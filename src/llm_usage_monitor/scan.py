from pathlib import Path


class IncrementalJsonlReader:
    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}

    def read_new(self, path: Path) -> list[str]:
        try:
            size = path.stat().st_size
        except OSError:
            return []

        offset = self._offsets.get(path, 0)
        if size == offset:
            return []
        if size < offset:
            offset = 0

        try:
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return []

        if not data:
            self._offsets[path] = offset
            return []

        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return []

        complete = data[: last_nl + 1]
        self._offsets[path] = offset + len(complete)
        text = complete.decode("utf-8", errors="replace")
        return [line for line in text.split("\n") if line]


class CachedGlob:
    def __init__(self, pattern: str) -> None:
        self._pattern = pattern
        self._paths: list[Path] | None = None
        self._mtimes: dict[Path, int] = {}

    def list(self, root: Path) -> list[Path]:
        if self._paths is not None and not self._changed():
            return self._paths
        paths = list(root.glob(self._pattern))
        dirs = {root}
        try:
            for child in root.iterdir():
                if child.is_dir():
                    dirs.add(child)
        except OSError:
            pass
        for path in paths:
            dirs.add(path.parent)
        mtimes: dict[Path, int] = {}
        for directory in dirs:
            try:
                mtimes[directory] = directory.stat().st_mtime_ns
            except OSError:
                pass
        self._paths = paths
        self._mtimes = mtimes
        return paths

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
