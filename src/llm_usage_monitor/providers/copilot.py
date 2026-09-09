import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from llm_usage_monitor.model import ProviderSnapshot
from llm_usage_monitor.process import trusted_which
from llm_usage_monitor.quota import LOGIN_NOTE, QuotaClient, apply_live


def _copilot_token() -> str | None:
    gh_host = os.environ.get("GH_HOST", "").strip().lower().rstrip(".")
    if not gh_host or gh_host == "github.com":
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            value = os.environ.get(var)
            if value and value.strip():
                return value.strip()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = (
        Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    )
    auth = base / "opencode" / "auth.json"
    try:
        with auth.open("rb") as file:
            raw = file.read(1024 * 1024 + 1)
        data = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        if isinstance(data, dict):
            entry = data.get("github-copilot")
            if isinstance(entry, dict):
                token = entry.get("access") or entry.get("token")
                if isinstance(token, str) and token.strip():
                    return token.strip()
    except (OSError, UnicodeError, RecursionError, ValueError):
        pass
    gh = trusted_which("gh")
    if not gh:
        return None
    try:
        env = os.environ.copy()
        for name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST",
        ):
            env.pop(name, None)
        result = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            cwd=Path.home(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


class CopilotProvider:
    name = "Copilot"
    key = "copilot"

    def __init__(
        self,
        quota: QuotaClient | None = None,
        *,
        token_retry_ttl: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._quota = quota or QuotaClient()
        self._token: str | None = None
        self._token_retry_ttl = max(0.0, token_retry_ttl)
        self._clock = clock
        self._token_checked_at = float("-inf")

    def snapshot(self) -> ProviderSnapshot:
        now = self._clock()
        if not self._token and now - self._token_checked_at >= self._token_retry_ttl:
            self._token = _copilot_token()
            self._token_checked_at = now
        had_token = bool(self._token)
        snap = ProviderSnapshot(name=self.name, plan=None)
        result = self._quota.copilot(self._token)
        if result.note in (LOGIN_NOTE, "此帳號沒有 Copilot 訂閱"):
            self._token = None
            if had_token:
                # A previously usable credential may have been revoked or the
                # active gh account may have changed. Re-resolve on the next
                # snapshot without defeating missing-token negative caching.
                self._token_checked_at = float("-inf")
        return apply_live(snap, result, replace=True)
