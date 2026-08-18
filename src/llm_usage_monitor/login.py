from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class SourceSpec:
    key: str
    display: str
    binaries: tuple[str, ...]
    login_args: tuple[str, ...]
    session_paths: tuple[Path, ...]
    env_keys: tuple[str, ...] = ()
    install_hint: str = ""
    json_key: str | None = None
    probe_args: tuple[str, ...] = ()
    login_cwd: Path | None = None

    def cli_path(self) -> Path | None:
        for name in self.binaries:
            candidate = Path(name)
            if candidate.exists():
                return candidate
            found = shutil.which(name)
            if found:
                return Path(found)
        return None

    def has_session(self) -> bool:
        if any(os.environ.get(key) for key in self.env_keys):
            return True
        for path in self.session_paths:
            try:
                if not path.exists() or path.stat().st_size <= 2:
                    continue
            except OSError:
                continue
            if self.key == "claude" and _claude_expired(path):
                continue
            if self.key == "codex" and _codex_expired(path):
                continue
            if self.json_key and path.suffix.lower() == ".json":
                if _json_entry_has_credential(path, self.json_key):
                    return True
                continue
            return True
        return self._probe_session() if self.probe_args else False

    def _probe_session(self) -> bool:
        cli = self.cli_path()
        if cli is None:
            return False
        args = list(self.probe_args)
        if self.key == "antigravity" and cli.name.lower().startswith("antigravity"):
            args = ["--print", "/usage"]
        try:
            result = subprocess.run(
                [str(cli), *args],
                cwd=self.login_cwd,
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())


def _json_entry_has_credential(path: Path, key: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return False
    return any(entry.get(name) for name in ("key", "access", "token"))


def _claude_expired(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        exp = oauth.get("expiresAt")
        if exp is None:
            return False
        expires_at = datetime.fromtimestamp(int(exp) / 1000, tz=UTC)
        return expires_at <= datetime.now(UTC)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True


def _codex_expired(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = ((data.get("tokens") or {}).get("access_token"))
        if not token:
            return True
        parts = token.split(".")
        if len(parts) != 3:
            return False
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        return bool(exp is not None and datetime.fromtimestamp(int(exp), tz=UTC) <= datetime.now(UTC))
    except (OSError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def default_sources() -> list[SourceSpec]:
    home = Path.home()
    opencode_auth = home / ".local" / "share" / "opencode" / "auth.json"
    return [
        SourceSpec(
            key="claude",
            display="Claude",
            binaries=("claude",),
            login_args=("auth", "login"),
            session_paths=(home / ".claude" / ".credentials.json",),
            install_hint="安裝 Claude Code：https://code.claude.com",
        ),
        SourceSpec(
            key="codex",
            display="Codex",
            binaries=("codex",),
            login_args=("login",),
            session_paths=(home / ".codex" / "auth.json",),
            install_hint="安裝 Codex CLI 後執行 codex login",
        ),
        SourceSpec(
            key="grok",
            display="Grok",
            binaries=("grok",),
            login_args=("login", "--oauth"),
            session_paths=(home / ".grok" / "auth.json",),
            install_hint="安裝 Grok CLI 後執行 grok login --oauth",
        ),
        SourceSpec(
            key="opencode",
            display="OpenCode GO",
            binaries=("opencode",),
            login_args=("providers", "login", "-p", "opencode"),
            session_paths=(opencode_auth,),
            install_hint="安裝 OpenCode 後執行 opencode providers login -p opencode",
            json_key="opencode",
        ),
        SourceSpec(
            key="copilot",
            display="GitHub Copilot",
            binaries=("gh", "opencode"),
            login_args=("auth", "login"),
            session_paths=(
                opencode_auth,
                home / ".config" / "gh" / "hosts.yml",
                home / "AppData" / "Roaming" / "GitHub CLI" / "hosts.yml",
            ),
            env_keys=("GITHUB_TOKEN", "GH_TOKEN"),
            install_hint="安裝 GitHub CLI 後執行 gh auth login",
            json_key="github-copilot",
        ),
        SourceSpec(
            key="antigravity",
            display="Antigravity",
            binaries=("agy", "antigravity"),
            login_args=("auth", "login"),
            session_paths=(
                home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
                home / ".config" / "antigravity-cli" / "antigravity-oauth-token",
            ),
            env_keys=("ANTIGRAVITY_ACCESS_TOKEN", "ANTIGRAVITY_REFRESH_TOKEN"),
            install_hint="安裝 Antigravity CLI（agy）後重新啟動",
            probe_args=("models",),
            login_cwd=home,
        ),
    ]


def missing_sources(wanted: set[str], sources: list[SourceSpec] | None = None) -> list[SourceSpec]:
    specs = sources or default_sources()
    return [spec for spec in specs if spec.key in wanted and not spec.has_session()]


def parse_selection(value: str, count: int) -> set[int]:
    raw = value.strip().lower()
    if raw in {"a", "all", "全部"}:
        return set(range(count))
    if not raw:
        return set()
    selected: set[int] = set()
    for part in raw.replace("，", ",").split(","):
        try:
            index = int(part.strip()) - 1
        except ValueError:
            continue
        if 0 <= index < count:
            selected.add(index)
    return selected


def login(spec: SourceSpec, *, runner=subprocess.run) -> tuple[bool, str]:
    cli = spec.cli_path()
    if cli is None:
        return False, spec.install_hint or f"找不到 {spec.display} CLI"
    args = list(spec.login_args)
    if spec.key == "copilot" and cli.name.lower().startswith("opencode"):
        args = ["providers", "login", "-p", "github-copilot"]
    if spec.key == "antigravity" and cli.name.lower().startswith("agy"):
        args = ["-p", "/usage"]
    kwargs = {"check": False}
    if spec.login_cwd is not None:
        kwargs["cwd"] = spec.login_cwd
    try:
        completed = runner([str(cli), *args], **kwargs)
    except OSError as exc:
        return False, str(exc)
    if getattr(completed, "returncode", 1) == 0:
        return True, f"{spec.display} 登入完成"
    return False, f"{spec.display} 登入失敗（exit {completed.returncode}）"


def ensure_sessions(
    wanted: set[str],
    *,
    select: Callable[[str], str],
    printer: Callable[[str], None],
    sources: list[SourceSpec] | None = None,
    select_all: bool = False,
    login_fn: Callable[[SourceSpec], tuple[bool, str]] = login,
) -> list[str]:
    specs = [spec for spec in (sources or default_sources()) if spec.key in wanted]
    missing: list[SourceSpec] = []
    printer("來源狀態")
    for spec in specs:
        if spec.has_session():
            printer(f"✓ {spec.display:<16} 已登入，自動抓取")
        else:
            missing.append(spec)
            printer(f"○ {spec.display:<16} 未登入")
    if not missing:
        return []

    printer("")
    for index, spec in enumerate(missing, 1):
        printer(f"[{index}] {spec.display}")
    if select_all:
        selected = set(range(len(missing)))
    else:
        choice = select("選擇要登入的來源（例如 1,3；a 全選；Enter 略過）：")
        selected = parse_selection(choice, len(missing))

    notes: list[str] = []
    for index, spec in enumerate(missing):
        if index not in selected:
            continue
        if spec.cli_path() is None:
            msg = spec.install_hint or f"找不到 {spec.display} CLI"
            printer(msg)
            notes.append(f"{spec.display}: {msg}")
            continue
        printer(f"正在啟動 {spec.display} 官方登入…")
        _ok, msg = login_fn(spec)
        printer(msg)
        notes.append(f"{spec.display}: {msg}")
    return notes
