import os
from pathlib import Path

from llm_usage_monitor.process import trusted_which


def _make_executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True)
    path = directory / name
    path.write_bytes(b"test")
    path.chmod(0o755)
    return path


def test_trusted_which_skips_current_and_relative_path_entries(
    monkeypatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "cwd"
    trusted = tmp_path / "trusted"
    relative = tmp_path / "relative"
    suffix = ".EXE" if os.name == "nt" else ""
    command = f"safe-tool{suffix}"
    _make_executable(cwd, command)
    _make_executable(relative, command)
    expected = _make_executable(trusted, command)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(("", str(cwd), "../relative", str(trusted))),
    )
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".EXE")

    assert trusted_which("safe-tool") == str(expected.resolve())


def test_trusted_which_rejects_relative_explicit_path(tmp_path: Path) -> None:
    assert trusted_which(str(Path("relative") / "tool")) is None
