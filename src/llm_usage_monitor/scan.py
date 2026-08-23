import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

FileIdentity = tuple[int, int]
FileSignature = tuple[int, int]
FileGuard = tuple[tuple[int, bytes], ...]

_GUARD_SAMPLE_BYTES = 64
_GUARD_SAMPLE_COUNT = 9


class IncrementalJsonlReader:
    def __init__(
        self,
        *,
        max_line_bytes: int = 64 * 1024 * 1024,
        batch_bytes: int = 1024 * 1024,
        verify_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_line_bytes = max(1, max_line_bytes)
        self._batch_bytes = max(1, batch_bytes)
        self._verify_interval = max(0.0, verify_interval)
        self._clock = clock
        self._offsets: dict[Path, int] = {}
        self._identities: dict[Path, FileIdentity] = {}
        self._signatures: dict[Path, FileSignature] = {}
        self._guards: dict[Path, FileGuard] = {}
        self._partials: dict[Path, bytearray] = {}
        self._discarding: set[Path] = set()
        self._generations: dict[Path, int] = {}
        self._last_verified: dict[Path, float] = {}

    def read_new(self, path: Path) -> list[str]:
        """Return complete JSONL records added since the previous call."""
        return list(self.iter_new(path))

    def iter_new(self, path: Path) -> Iterator[str]:
        """Yield complete JSONL records without materializing the backlog."""
        return self._iter_new(path)

    def _iter_new(self, path: Path) -> Iterator[str]:
        opened = False
        try:
            now = self._clock()
            path_stat = path.stat()
            known_identity = self._identities.get(path)
            path_identity = (path_stat.st_dev, path_stat.st_ino)
            path_signature = (path_stat.st_size, path_stat.st_mtime_ns)
            verification_due = (
                now - self._last_verified.get(path, float("-inf"))
                >= self._verify_interval
            )
            if (
                known_identity == path_identity
                and self._offsets.get(path, 0) == path_stat.st_size
                and self._signatures.get(path) == path_signature
                and not verification_due
            ):
                return

            with path.open("rb") as file:
                opened = True
                stat = os.fstat(file.fileno())
                # A read_new call observes one fixed horizon. Bytes appended by a
                # live writer are deliberately left for the next refresh.
                read_limit = stat.st_size
                identity = (stat.st_dev, stat.st_ino)
                signature = (stat.st_size, stat.st_mtime_ns)
                reset = self._prepare_file(path, file, identity, signature)
                offset = self._offsets[path]
                if offset >= read_limit:
                    self._signatures[path] = signature
                    if reset:
                        self._guards[path] = self._guard(file, offset)
                    return

                file.seek(offset)
                while offset < read_limit:
                    budget = min(self._batch_bytes, read_limit - offset)
                    if path in self._discarding:
                        data = file.readline(max(1, budget))
                        if not data:
                            break
                        self._consume(path, data)
                        offset += len(data)
                        if data.endswith(b"\n"):
                            self._discarding.discard(path)
                        continue

                    partial = self._partials.setdefault(path, bytearray())
                    remaining = self._max_line_bytes - len(partial)
                    amount = min(max(1, remaining + 1), max(1, budget))
                    data = file.readline(amount)
                    if not data:
                        break
                    self._consume(path, data)
                    offset += len(data)
                    partial.extend(data)

                    if not data.endswith(b"\n"):
                        if len(partial) > self._max_line_bytes:
                            self._partials.pop(path, None)
                            self._discarding.add(path)
                        continue

                    self._partials.pop(path, None)
                    partial.pop()  # newline
                    if partial and partial[-1] == 0x0D:
                        partial.pop()
                    if len(partial) > self._max_line_bytes or not partial:
                        continue
                    text = partial.decode("utf-8", errors="replace")
                    del partial
                    yield text

                latest = os.fstat(file.fileno())
                self._signatures[path] = (latest.st_size, latest.st_mtime_ns)
                self._guards[path] = self._guard(file, self._offsets[path])
        except OSError:
            return
        finally:
            if opened:
                self._record_closed_signature(path)

    def generation(self, path: Path) -> int:
        return self._generations.get(path, 0)

    def identity(self, path: Path) -> FileIdentity | None:
        identity = self._identities.get(path)
        # Some Windows/network filesystems report a zero inode for every file.
        # It remains usable as path-local scanner state, but not as a global
        # hardlink/logical-file identity.
        return identity if identity is not None and identity[1] != 0 else None

    def prune(self, active_paths: set[Path]) -> None:
        known = set(self._offsets) | set(self._identities)
        for path in known - active_paths:
            self._offsets.pop(path, None)
            self._identities.pop(path, None)
            self._signatures.pop(path, None)
            self._guards.pop(path, None)
            self._partials.pop(path, None)
            self._discarding.discard(path)
            self._generations.pop(path, None)
            self._last_verified.pop(path, None)

    def _prepare_file(
        self,
        path: Path,
        file,
        identity: FileIdentity,
        signature: FileSignature,
    ) -> bool:
        previous_identity = self._identities.get(path)
        offset = self._offsets.get(path, 0)
        if previous_identity != identity or signature[0] < offset:
            self._reset(path, identity)
            return True

        if offset and self._guards.get(path) != self._guard(file, offset):
            self._reset(path, identity)
            return True
        return False

    def _reset(self, path: Path, identity: FileIdentity) -> None:
        self._identities[path] = identity
        self._offsets[path] = 0
        self._signatures.pop(path, None)
        self._guards.pop(path, None)
        self._partials.pop(path, None)
        self._discarding.discard(path)
        self._generations[path] = self._generations.get(path, 0) + 1
        self._last_verified.pop(path, None)

    def _record_closed_signature(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        identity = (stat.st_dev, stat.st_ino)
        if self._identities.get(path) != identity:
            return
        self._last_verified[path] = self._clock()

    def _consume(self, path: Path, data: bytes) -> None:
        self._offsets[path] = self._offsets.get(path, 0) + len(data)

    @staticmethod
    def _guard(file, offset: int) -> FileGuard:
        if offset <= 0:
            return ()
        position = file.tell()
        try:
            width = min(_GUARD_SAMPLE_BYTES, offset)
            last_start = max(0, offset - width)
            if last_start == 0:
                starts = [0]
            else:
                starts = sorted(
                    {
                        (last_start * index) // (_GUARD_SAMPLE_COUNT - 1)
                        for index in range(_GUARD_SAMPLE_COUNT)
                    }
                )
            samples: list[tuple[int, bytes]] = []
            for start in starts:
                file.seek(start)
                samples.append((start, file.read(min(width, offset - start))))
            return tuple(samples)
        finally:
            file.seek(position)


class CachedGlob:
    def __init__(
        self,
        pattern: str,
        *,
        rescan_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pattern = pattern
        self._rescan_interval = rescan_interval
        self._clock = clock
        self._root: Path | None = None
        self._paths: list[Path] | None = None
        self._mtimes: dict[Path, int] = {}
        self._last_scan = float("-inf")

    def list(self, root: Path) -> list[Path]:
        now = self._clock()
        cache_fresh = now - self._last_scan < self._rescan_interval
        if (
            self._root == root
            and self._paths is not None
            and cache_fresh
            and not self._changed()
        ):
            return list(self._paths)
        paths = _dedupe_files(list(root.glob(self._pattern)))
        dirs = {root}
        try:
            for child in root.iterdir():
                if child.is_dir():
                    dirs.add(child)
        except OSError:
            pass
        for path in paths:
            for directory in path.parents:
                dirs.add(directory)
                if directory == root:
                    break
        mtimes: dict[Path, int] = {}
        for directory in dirs:
            try:
                mtimes[directory] = directory.stat().st_mtime_ns
            except OSError:
                pass
        self._paths = paths
        self._root = root
        self._mtimes = mtimes
        self._last_scan = now
        return list(paths)

    def _changed(self) -> bool:
        if not self._mtimes:
            return True
        for directory, mtime in self._mtimes.items():
            try:
                if directory.stat().st_mtime_ns != mtime:
                    return True
            except OSError:
                return True
        return False


def _dedupe_files(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            unique.append(path)
            continue
        inode = getattr(stat, "st_ino", 0)
        if not inode:
            unique.append(path)
            continue
        identity = (stat.st_dev, inode)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(path)
    return unique
