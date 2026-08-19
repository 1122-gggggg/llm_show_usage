from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from llm_usage_monitor.model import ProviderSnapshot
from llm_usage_monitor.quota import LOGIN_NOTE, parse_antigravity_cli

Runner = Callable[..., subprocess.CompletedProcess[str]]


class AntigravityProvider:
    name = "Antigravity"
    key = "antigravity"

    def __init__(self, runner: Runner = subprocess.run, ttl: float = 10.0) -> None:
        self._runner = runner
        self._ttl = ttl
        self._cached: tuple[float, ProviderSnapshot] | None = None
        self._binary = shutil.which("agy") or shutil.which("antigravity")

    def snapshot(self) -> ProviderSnapshot:
        now = time.monotonic()
        if self._cached and now - self._cached[0] < self._ttl:
            return self._cached[1]
        snap = self._fetch()
        if (
            not snap.quotas
            and snap.notes == ["Antigravity 配額讀取失敗"]
            and self._cached
            and self._cached[1].quotas
        ):
            previous = self._cached[1]
            snap = ProviderSnapshot(
                name=self.name,
                plan=previous.plan,
                quotas=previous.quotas,
                notes=snap.notes,
            )
        self._cached = (now, snap)
        return snap

    def _fetch(self) -> ProviderSnapshot:
        if not self._binary:
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["找不到 Antigravity CLI"],
            )
        binary_name = Path(self._binary).name.lower()
        if binary_name.startswith("agy"):
            command = [self._binary, "-p", "/usage"]
        else:
            command = [self._binary, "--print", "/usage"]
        try:
            result = self._runner(
                command,
                cwd=Path.home(),
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["Antigravity 配額讀取失敗"],
            )
        if result.returncode != 0:
            return ProviderSnapshot(name=self.name, plan=None, notes=[LOGIN_NOTE])
        windows, plan = parse_antigravity_cli(result.stdout)
        if not windows:
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["Antigravity 配額讀取失敗"],
            )
        return ProviderSnapshot(name=self.name, plan=plan, quotas=windows)
