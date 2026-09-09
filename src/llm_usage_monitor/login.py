from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.process import trusted_which

_SAFE_AGY_VERSION = (1, 1, 11)
_MAX_SESSION_BYTES = 1024 * 1024
_AGY_VERSION_RE = re.compile(
    r"(?:agy(?:\.exe)?\s+version\s+)?v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE
)


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
    login_env: tuple[tuple[str, str], ...] = ()

    def cli_path(self) -> Path | None:
        resolved = self._cli_command()
        return resolved[0] if resolved is not None else None

    def _cli_command(self) -> tuple[Path, str] | None:
        for name in self.binaries:
            candidate = Path(name)
            alias = candidate.name.lower()
            found = trusted_which(name)
            if found:
                return Path(found), alias
        return None

    def has_session(self) -> bool:
        if any(
            _clean_token(os.environ.get(key)) is not None
            and _env_credential_matches_host(self.key, key)
            for key in self.env_keys
        ):
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
            if self.key == "grok":
                if _grok_has_credential(path):
                    return True
                continue
            if self.key == "copilot" and path.suffix.lower() in {".yml", ".yaml"}:
                if _github_hosts_has_credential(path):
                    return True
                continue
            if self.json_key and path.suffix.lower() == ".json":
                if _json_entry_has_credential(path, self.json_key):
                    return True
                continue
            return True
        return self._probe_session() if self.probe_args else False

    def _probe_session(self) -> bool:
        resolved = self._cli_command()
        if resolved is None:
            return False
        cli, alias = resolved
        args = list(self.probe_args)
        if self.key == "antigravity" and not alias.startswith("agy"):
            return False
        try:
            result = subprocess.run(
                [str(cli), *args],
                cwd=self.login_cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())


def _json_entry_has_credential(path: Path, key: str) -> bool:
    data = _read_json_object(path)
    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return False
    return any(
        _clean_token(entry.get(name)) is not None for name in ("key", "access", "token")
    )


def _grok_has_credential(path: Path) -> bool:
    data = _read_json_object(path)
    entry = next(iter(data.values()), None) if data is not None else None
    return isinstance(entry, dict) and _clean_token(entry.get("key")) is not None


def _read_json_object(path: Path) -> dict | None:
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_SESSION_BYTES + 1)
        if len(raw) > _MAX_SESSION_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, UnicodeError, RecursionError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _github_hosts_has_credential(path: Path) -> bool:
    """Recognize only a GitHub.com token in gh's bounded hosts.yml file."""
    try:
        with path.open("rb") as file:
            raw = file.read(_MAX_SESSION_BYTES + 1)
        if len(raw) > _MAX_SESSION_BYTES:
            return False
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return False

    in_github_com = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            host = stripped.split(":", 1)[0].strip("'\"").lower().rstrip(".")
            in_github_com = host == "github.com"
            continue
        if not in_github_com:
            continue
        key, separator, value = stripped.partition(":")
        if separator and key.strip() == "oauth_token":
            token = value.split("#", 1)[0].strip().strip("'\"")
            return bool(token and token.lower() not in {"null", "~"})
    return False


def _clean_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _env_credential_matches_host(source: str, env_key: str) -> bool:
    if source != "copilot" or env_key not in {"GH_TOKEN", "GITHUB_TOKEN"}:
        return True
    host = os.environ.get("GH_HOST", "").strip().lower().rstrip(".")
    return not host or host == "github.com"


def _agy_version(text: str) -> tuple[int, int, int] | None:
    match = _AGY_VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.groups())
    except ValueError:
        return None


def _claude_expired(path: Path) -> bool:
    try:
        data = _read_json_object(path)
        if data is None:
            return True
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return True
        if _clean_token(oauth.get("accessToken")) is None:
            return True
        exp = oauth.get("expiresAt")
        if exp is None:
            return False
        expires_at = datetime.fromtimestamp(int(exp) / 1000, tz=UTC)
        return expires_at <= datetime.now(UTC)
    except (
        OSError,
        OverflowError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ):
        return True


def _codex_expired(path: Path) -> bool:
    try:
        data = _read_json_object(path)
        if data is None:
            return True
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return True
        token = _clean_token(tokens.get("access_token"))
        if token is None:
            return True
        parts = token.split(".")
        if len(parts) != 3:
            return False
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(payload, dict):
            return True
        exp = payload.get("exp")
        if exp is None:
            return True
        return datetime.fromtimestamp(int(exp), tz=UTC) <= datetime.now(UTC)
    except (
        OSError,
        OverflowError,
        ValueError,
        TypeError,
        UnicodeDecodeError,
        RecursionError,
    ):
        return True


def _env_path(key: str) -> Path | None:
    value = os.environ.get(key)
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _gh_config_dir(home: Path) -> Path:
    configured = _env_path("GH_CONFIG_DIR")
    if configured is not None:
        return configured
    xdg_config = _env_path("XDG_CONFIG_HOME")
    if xdg_config is not None:
        return xdg_config / "gh"
    if os.name == "nt":
        appdata = _env_path("APPDATA")
        if appdata is not None:
            return appdata / "GitHub CLI"
    return home / ".config" / "gh"


def default_sources(
    *,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    grok_dir: Path | None = None,
) -> list[SourceSpec]:
    home = Path.home()
    claude_profile = (
        claude_dir.parent
        if claude_dir is not None
        else (_env_path("CLAUDE_CONFIG_DIR") or home / ".claude")
    )
    codex_profile = (
        codex_dir.parent
        if codex_dir is not None
        else (_env_path("CODEX_HOME") or home / ".codex")
    )
    grok_profile = (
        grok_dir if grok_dir is not None else (_env_path("GROK_HOME") or home / ".grok")
    )
    xdg_data = _env_path("XDG_DATA_HOME")
    opencode_data = (
        xdg_data / "opencode" if xdg_data else home / ".local" / "share" / "opencode"
    )
    opencode_auth = opencode_data / "auth.json"
    gh_hosts = _gh_config_dir(home) / "hosts.yml"
    return [
        SourceSpec(
            key="claude",
            display="Claude",
            binaries=("claude",),
            login_args=("auth", "login"),
            session_paths=(claude_profile / ".credentials.json",),
            install_hint="安裝 Claude Code：https://code.claude.com",
            login_env=(
                (("CLAUDE_CONFIG_DIR", str(claude_profile)),)
                if claude_dir is not None
                else ()
            ),
        ),
        SourceSpec(
            key="codex",
            display="Codex",
            binaries=("codex",),
            login_args=("login",),
            session_paths=(codex_profile / "auth.json",),
            install_hint="安裝 Codex CLI 後執行 codex login",
            login_env=(
                (("CODEX_HOME", str(codex_profile)),) if codex_dir is not None else ()
            ),
        ),
        SourceSpec(
            key="grok",
            display="Grok",
            binaries=("grok",),
            login_args=("login", "--oauth"),
            session_paths=(grok_profile / "auth.json",),
            install_hint="安裝 Grok CLI 後執行 grok login --oauth",
            login_env=(
                (("GROK_HOME", str(grok_profile)),) if grok_dir is not None else ()
            ),
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
                gh_hosts,
            ),
            env_keys=("GITHUB_TOKEN", "GH_TOKEN"),
            install_hint="安裝 GitHub CLI 後執行 gh auth login",
            json_key="github-copilot",
        ),
        SourceSpec(
            key="antigravity",
            display="Antigravity",
            binaries=("agy",),
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


def missing_sources(
    wanted: set[str], sources: list[SourceSpec] | None = None
) -> list[SourceSpec]:
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
    resolved = spec._cli_command()
    if resolved is None:
        return False, spec.install_hint or f"找不到 {spec.display} CLI"
    cli, alias = resolved
    args = list(spec.login_args)
    if spec.key == "copilot" and alias.startswith("opencode"):
        args = ["providers", "login", "-p", "github-copilot"]
    if spec.key == "antigravity" and not alias.startswith("agy"):
        return False, "只支援 agy >= 1.1.11，未執行 /usage"
    if spec.key == "antigravity" and alias.startswith("agy"):
        try:
            version_result = runner(
                [str(cli), "--version"],
                cwd=spec.login_cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "無法確認 agy 版本，未執行 /usage"
        output = "\n".join(
            part
            for part in (
                getattr(version_result, "stdout", ""),
                getattr(version_result, "stderr", ""),
            )
            if isinstance(part, str) and part.strip()
        )
        version = _agy_version(output)
        if getattr(version_result, "returncode", 1) != 0 or version is None:
            return False, "無法確認 agy 版本，未執行 /usage"
        if version < _SAFE_AGY_VERSION:
            return False, "agy 版本過舊（需 >= 1.1.11），未執行 /usage"
        args = ["-p", "/usage"]
    kwargs = {"check": False}
    if spec.key == "antigravity":
        kwargs["stdin"] = subprocess.DEVNULL
    if spec.login_cwd is not None:
        kwargs["cwd"] = spec.login_cwd
    if spec.login_env:
        env = os.environ.copy()
        env.update(spec.login_env)
        kwargs["env"] = env
    try:
        completed = runner([str(cli), *args], **kwargs)
    except OSError as exc:
        return False, f"{exc.__class__.__name__}: 無法啟動 {spec.display} CLI"
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
