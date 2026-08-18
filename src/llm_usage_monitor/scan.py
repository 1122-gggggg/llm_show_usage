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
