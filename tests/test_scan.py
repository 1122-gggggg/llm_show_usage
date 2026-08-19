from pathlib import Path

from llm_usage_monitor.scan import IncrementalJsonlReader


def test_reader_skips_reopen_when_size_unchanged(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text("one\n", encoding="utf-8")
    reader = IncrementalJsonlReader()
    assert reader.read_new(path) == ["one"]

    opened = {"n": 0}
    original = Path.open

    def wrapped(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb":
            opened["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", wrapped)
    assert reader.read_new(path) == []
    assert opened["n"] == 0

    path.write_text("one\ntwo\n", encoding="utf-8")
    assert reader.read_new(path) == ["two"]
    assert opened["n"] == 1
