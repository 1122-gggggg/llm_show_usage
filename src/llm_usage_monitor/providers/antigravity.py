from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.model import ProviderSnapshot, clone_snapshot
from llm_usage_monitor.process import trusted_which
from llm_usage_monitor.quota import LOGIN_NOTE, parse_antigravity_cli

Runner = Callable[..., subprocess.CompletedProcess[str]]

_MIN_SAFE_VERSION = (1, 1, 11)
_VERSION_RECHECK_SECONDS = 60.0
_TRANSIENT_NOTE = "Antigravity 配額讀取失敗"
_VERSION_RE = re.compile(
    r"(?:agy(?:\.exe)?\s+version\s+)?v?(\d+)\.(\d+)\.(\d+)",
    re.IGNORECASE,
)


def _agy_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.groups())
    except ValueError:
        return None


def _current_quota_snapshot(snapshot: ProviderSnapshot) -> ProviderSnapshot:
    current = clone_snapshot(snapshot)
    now = datetime.now(UTC)
    windows = []
    for window in current.quotas:
        reset = window.resets_at
        if reset is None:
            windows.append(window)
            continue
        try:
            aware = (
                reset if reset.utcoffset() is not None else reset.replace(tzinfo=UTC)
            )
            if aware > now:
                windows.append(window)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
    current.quotas = windows
    return current


class AntigravityProvider:
    name = "Antigravity"
    key = "antigravity"

    def __init__(
        self,
        runner: Runner = subprocess.run,
        ttl: float = 10.0,
        max_stale: float = 300.0,
    ) -> None:
        self._runner = runner
        self._ttl = ttl
        self._max_stale = max(0.0, max_stale)
        self._cached: tuple[float, ProviderSnapshot] | None = None
        self._last_good: tuple[float, ProviderSnapshot] | None = None
        self._binary = trusted_which("agy")
        self._version_verified: tuple[object, ...] | None = None
        self._version_checked_at = float("-inf")
        self._lock = threading.Lock()

    def snapshot(self) -> ProviderSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> ProviderSnapshot:
        now = time.monotonic()
        if self._cached and now - self._cached[0] < self._ttl:
            cached = self._cached[1]
            current_cached = (
                _current_quota_snapshot(cached)
                if cached.notes == [_TRANSIENT_NOTE]
                else clone_snapshot(cached)
            )
            stale_expired = (
                cached.notes == [_TRANSIENT_NOTE]
                and bool(cached.quotas)
                and (
                    not current_cached.quotas
                    or self._last_good is None
                    or now - self._last_good[0] > self._max_stale
                )
            )
            if not stale_expired:
                return current_cached
        snap = self._fetch()
        finished_at = time.monotonic()
        if snap.quotas:
            self._last_good = (finished_at, clone_snapshot(snap))
        if (
            not snap.quotas
            and snap.notes == [_TRANSIENT_NOTE]
            and self._last_good
            and finished_at - self._last_good[0] <= self._max_stale
        ):
            previous = _current_quota_snapshot(self._last_good[1])
            snap = ProviderSnapshot(
                name=self.name,
                plan=previous.plan,
                quotas=list(previous.quotas),
                notes=snap.notes,
            )
        self._cached = (finished_at, clone_snapshot(snap))
        return clone_snapshot(snap)

    def _fetch(self) -> ProviderSnapshot:
        if not self._binary:
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["找不到 Antigravity CLI"],
            )
        version_error = self._version_error()
        if version_error is not None:
            return ProviderSnapshot(name=self.name, plan=None, notes=[version_error])
        command = [self._binary, "-p", "/usage"]
        try:
            result = self._run(command, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[_TRANSIENT_NOTE],
            )
        if result.returncode != 0:
            output = f"{result.stdout}\n{result.stderr}".lower()
            auth_failed = any(
                marker in output
                for marker in (
                    "login",
                    "not logged",
                    "sign in",
                    "unauthorized",
                    "authentication",
                )
            )
            note = LOGIN_NOTE if auth_failed else _TRANSIENT_NOTE
            return ProviderSnapshot(name=self.name, plan=None, notes=[note])
        windows, plan = parse_antigravity_cli(result.stdout)
        if not windows:
            return ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[_TRANSIENT_NOTE],
            )
        return ProviderSnapshot(name=self.name, plan=plan, quotas=windows)

    def _version_error(self) -> str | None:
        assert self._binary is not None
        binary_identity = self._binary_identity()
        now = time.monotonic()
        if (
            self._version_verified == binary_identity
            and now - self._version_checked_at < _VERSION_RECHECK_SECONDS
        ):
            return None
        try:
            result = self._run([self._binary, "--version"], timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return "無法確認 agy 版本，未執行 /usage"
        if result.returncode != 0:
            return "無法確認 agy 版本，未執行 /usage"
        output = "\n".join(
            part
            for part in (result.stdout, result.stderr)
            if isinstance(part, str) and part.strip()
        )
        version = _agy_version(output)
        if version is None:
            return "無法確認 agy 版本，未執行 /usage"
        if version < _MIN_SAFE_VERSION:
            return "agy 版本過舊（需 >= 1.1.11），未執行 /usage"
        self._version_verified = binary_identity
        self._version_checked_at = time.monotonic()
        return None

    def _binary_identity(self) -> tuple[object, ...]:
        assert self._binary is not None
        path = Path(self._binary)
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except (OSError, RuntimeError):
            return (str(path), None)
        return (
            str(resolved),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def _run(
        self, command: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            command,
            cwd=Path.home(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
