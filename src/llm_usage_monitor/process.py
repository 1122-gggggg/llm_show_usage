from __future__ import annotations

import os
from pathlib import Path


def trusted_which(command: str) -> str | None:
    """Resolve a command without implicitly searching the current directory."""
    requested = Path(command)
    if requested.parent != Path("."):
        if not requested.is_absolute():
            return None
        return _usable_file(requested)

    try:
        current = Path.cwd().resolve(strict=False)
    except (OSError, RuntimeError):
        current = None

    names = _candidate_names(requested.name)
    for raw_directory in os.get_exec_path():
        raw_directory = raw_directory.strip().strip('"')
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        if not directory.is_absolute():
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if current is not None and resolved_directory == current:
            continue
        for name in names:
            resolved = _usable_file(resolved_directory / name)
            if resolved is not None:
                return resolved
    return None


def _candidate_names(command: str) -> tuple[str, ...]:
    if os.name != "nt":
        return (command,)
    suffixes = tuple(
        suffix.strip()
        for suffix in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        if suffix.strip()
    )
    if any(command.lower().endswith(suffix.lower()) for suffix in suffixes):
        return (command,)
    return tuple(f"{command}{suffix}" for suffix in suffixes)


def _usable_file(path: Path) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return None
    except (OSError, RuntimeError):
        return None
    return str(resolved)
