import os
from pathlib import Path

from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader


def test_reader_skips_reopen_when_size_unchanged(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\n")
    reader = IncrementalJsonlReader()
    assert list(reader.read_new(path)) == ["one"]

    opened = {"n": 0}
    original = Path.open

    def wrapped(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb":
            opened["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", wrapped)
    assert list(reader.read_new(path)) == []
    assert opened["n"] == 0

    path.write_bytes(b"one\ntwo\n")
    assert list(reader.read_new(path)) == ["two"]
    assert opened["n"] == 1


def test_reader_normalizes_crlf(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\r\ntwo\r\n")

    assert list(IncrementalJsonlReader().read_new(path)) == ["one", "two"]


def test_reader_public_api_returns_list_and_replaces_invalid_utf8(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\xfftwo\n")
    reader = IncrementalJsonlReader()

    result = reader.read_new(path)

    assert isinstance(result, list)
    assert result == ["one\ufffdtwo"]
    assert reader._max_line_bytes >= 64 * 1024 * 1024


def test_reader_does_not_expose_zero_inode_as_cross_file_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.jsonl"
    reader = IncrementalJsonlReader()
    reader._identities[path] = (1, 0)

    assert reader.identity(path) is None


def test_reader_preserves_unicode_line_separator(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes('{"text":"one\u2028two"}\n'.encode())

    assert list(IncrementalJsonlReader().read_new(path)) == ['{"text":"one\u2028two"}']


def test_reader_detects_same_size_file_replacement(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\n")
    reader = IncrementalJsonlReader()
    assert list(reader.read_new(path)) == ["one"]

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"two\n")
    replacement.replace(path)

    assert list(reader.read_new(path)) == ["two"]


def test_reader_detects_same_inode_rewrite_and_regrow(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\n")
    reader = IncrementalJsonlReader(verify_interval=0)
    assert list(reader.read_new(path)) == ["one"]
    first_generation = reader.generation(path)

    path.write_bytes(b"two\n")
    assert list(reader.read_new(path)) == ["two"]
    assert reader.generation(path) > first_generation

    path.write_bytes(b"new\nsecond\n")
    assert list(reader.read_new(path)) == ["new", "second"]


def test_reader_bounds_and_discards_overlong_incomplete_line(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"x" * 32)
    reader = IncrementalJsonlReader(max_line_bytes=8, batch_bytes=4)

    assert list(reader.read_new(path)) == []
    assert reader._offsets[path] == 32
    assert path not in reader._partials

    original = Path.open
    opened = 0

    def wrapped(self, *args, **kwargs):
        nonlocal opened
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            opened += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", wrapped)
    assert reader.read_new(path) == []
    assert opened == 0

    with path.open("ab") as file:
        file.write(b"\nvalid\n")
    assert list(reader.read_new(path)) == ["valid"]


def test_reader_does_not_reread_32_mib_incomplete_line(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "large.jsonl"
    size = 32 * 1024 * 1024
    path.write_bytes(b"x" * size)
    reader = IncrementalJsonlReader()

    assert reader.read_new(path) == []
    assert reader._offsets[path] == size
    assert len(reader._partials[path]) == size
    assert len(reader._partials[path]) <= reader._max_line_bytes

    original = Path.open
    opened = 0

    def wrapped(self, *args, **kwargs):
        nonlocal opened
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            opened += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", wrapped)

    assert reader.read_new(path) == []
    assert opened == 0


def test_reader_accepts_line_at_limit_and_discards_line_over_limit(
    tmp_path: Path,
) -> None:
    at_limit = tmp_path / "at-limit.jsonl"
    at_limit.write_bytes(b"12345678\nok\n")
    reader = IncrementalJsonlReader(max_line_bytes=8, batch_bytes=4)

    assert reader.read_new(at_limit) == ["12345678", "ok"]

    over_limit = tmp_path / "over-limit.jsonl"
    over_limit.write_bytes(b"123456789\nok\n")
    reader = IncrementalJsonlReader(max_line_bytes=8, batch_bytes=4)

    assert reader.read_new(over_limit) == ["ok"]


def test_reader_closes_file_before_consumer_processes_line(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    path.write_text("old\n", encoding="utf-8")
    replacement.write_text("new\n", encoding="utf-8")
    reader = IncrementalJsonlReader()

    assert reader.read_new(path) == ["old"]
    replacement.replace(path)
    assert list(reader.read_new(path)) == ["new"]


def test_reader_uses_fixed_horizon_for_live_writer(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\ntwo\n")
    reader = IncrementalJsonlReader(batch_bytes=4)
    iterator = reader.iter_new(path)

    assert next(iterator) == "one"
    with path.open("ab") as file:
        file.write(b"later\n")

    assert list(iterator) == ["two"]
    assert reader.read_new(path) == ["later"]


def test_reader_streaming_iterator_close_releases_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"one\ntwo\n")
    reader = IncrementalJsonlReader()
    original = Path.open
    handles = []

    def wrapped(self, *args, **kwargs):
        handle = original(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", wrapped)
    iterator = reader.iter_new(path)

    assert next(iterator) == "one"
    assert handles and not handles[-1].closed

    iterator.close()

    assert handles[-1].closed


def test_reader_bounded_guard_detects_middle_rewrite_and_grow(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    middle = "a" * 4096
    path.write_text(f"head\n{middle}\ntail\n", encoding="utf-8")
    reader = IncrementalJsonlReader()
    assert reader.read_new(path) == ["head", middle, "tail"]
    first_generation = reader.generation(path)

    changed = f"head\n{'a' * 2048}b{'a' * 2047}\ntail\nnew\n"
    path.write_text(changed, encoding="utf-8")

    assert reader.read_new(path) == [
        "head",
        f"{'a' * 2048}b{'a' * 2047}",
        "tail",
        "new",
    ]
    assert reader.generation(path) > first_generation


def test_reader_periodic_verification_reads_only_bounded_guard(
    tmp_path: Path, monkeypatch
) -> None:
    now = [0.0]
    path = tmp_path / "a.jsonl"
    path.write_bytes(b"x" * (4 * 1024 * 1024))
    reader = IncrementalJsonlReader(clock=lambda: now[0])
    assert reader.read_new(path) == []

    original = Path.open
    opened = 0
    read_bytes = 0

    class CountingReader:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def read(self, size=-1):
            nonlocal read_bytes
            data = self._handle.read(size)
            read_bytes += len(data)
            return data

    def wrapped(self, *args, **kwargs):
        nonlocal opened
        handle = original(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            opened += 1
            return CountingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", wrapped)
    now[0] = 60.0

    assert reader.read_new(path) == []
    assert opened == 1
    assert read_bytes <= 9 * 64


def test_reader_prunes_deleted_file_state(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text("one\n", encoding="utf-8")
    reader = IncrementalJsonlReader()
    assert list(reader.read_new(path)) == ["one"]

    reader.prune(set())

    assert path not in reader._offsets
    assert path not in reader._identities
    assert reader.generation(path) == 0


def test_cached_glob_detects_new_nested_sibling(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    month = root / "2026" / "08"
    first = month / "22" / "rollout-first.jsonl"
    first.parent.mkdir(parents=True)
    first.write_text("", encoding="utf-8")
    files = CachedGlob("**/rollout-*.jsonl")

    assert files.list(root) == [first]

    second = month / "23" / "rollout-second.jsonl"
    second.parent.mkdir()
    second.write_text("", encoding="utf-8")
    cached_mtime = month.stat().st_mtime_ns
    os.utime(month, ns=(cached_mtime + 1_000_000_000,) * 2)

    assert set(files.list(root)) == {first, second}


def test_cached_glob_periodically_rescans_unwatched_empty_tree(tmp_path: Path) -> None:
    now = [0.0]
    root = tmp_path / "sessions"
    day = root / "2026" / "08" / "23"
    day.mkdir(parents=True)
    files = CachedGlob(
        "**/rollout-*.jsonl",
        rescan_interval=60,
        clock=lambda: now[0],
    )
    assert files.list(root) == []

    path = day / "rollout-new.jsonl"
    path.write_text("", encoding="utf-8")
    assert files.list(root) == []

    now[0] = 60
    assert files.list(root) == [path]
