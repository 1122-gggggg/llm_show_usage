import json
import os
import shutil
import subprocess
from pathlib import Path

from llm_usage_monitor.model import ProviderSnapshot
from llm_usage_monitor.quota import QuotaClient, apply_live


def _copilot_token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
        entry = data.get("github-copilot") or {}
        token = entry.get("access") or entry.get("token")
        if token:
            return str(token)
    except (OSError, json.JSONDecodeError):
        pass
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


class CopilotProvider:
    name = "Copilot"
    key = "copilot"

    def __init__(self, quota: QuotaClient | None = None) -> None:
        self._quota = quota or QuotaClient()
        self._token: str | None = None

    def snapshot(self) -> ProviderSnapshot:
        if not self._token:
            self._token = _copilot_token()
        snap = ProviderSnapshot(name=self.name, plan=None)
        return apply_live(snap, self._quota.copilot(self._token), replace=True)
