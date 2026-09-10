"""Oh My Pi (``omp usage --json``) quota source.

This module binds the dashboard to the quotas Oh My Pi itself reports for
the models currently configured there. One ``omp usage --json`` subprocess
per refresh cycle is shared by all OMP family providers, so five dashboard
rows cost a single ``omp`` invocation.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow
from llm_usage_monitor.process import trusted_which

_FAMILY_DISPLAY = {
    "anthropic": "Claude",
    "openai-codex": "Codex",
    "google-antigravity": "Antigravity",
    "xai-oauth": "Grok",
    "opencode-go": "GO",
}

KNOWN_FAMILIES = tuple(_FAMILY_DISPLAY)

# Snapshots are always emitted for every known family, so this carrier is
# guaranteed to exist for surfacing unexpected omp provider ids.
_UNKNOWN_CARRIER = "anthropic"

_SHORT_WINDOW = {
    "5h": "5h",
    "1w": "週",
    "7d": "週",
    "30d": "30天",
    "weekly": "週",
    "monthly": "月",
}

_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_DEFAULT_TTL = 10.0
_DEFAULT_TIMEOUT = 30.0

NOT_INSTALLED_NOTE = "未安裝 omp，安裝 Oh My Pi 後即可顯示各模型額度"
EMPTY_NOTE = "omp usage 無配額資料，請先在 omp 登入各帳號"
FAILED_NOTE = "omp usage 讀取失敗"


@dataclass
class _AccountWindow:
    label: str
    account_tag: str
    account_order: int
    used_percent: float | None
    resets_at: datetime | None = None


@dataclass
class _FamilyData:
    windows: list[_AccountWindow] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _used_percent(amount: object) -> float | None:
    if not isinstance(amount, dict):
        return None
    fraction = amount.get("usedFraction")
    if isinstance(fraction, bool):
        fraction = None
    if isinstance(fraction, (int, float)) and math.isfinite(fraction):
        candidate = float(fraction) * 100.0
        if 0.0 <= candidate <= 100.0:
            return candidate
    used = amount.get("used")
    if isinstance(used, bool):
        return None
    if (
        isinstance(used, (int, float))
        and math.isfinite(used)
        and 0.0 <= float(used) <= 100.0
    ):
        return float(used)
    return None


def _parse_ms_ts(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(millis) or millis < 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _short_window(*candidates: object) -> str | None:
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        key = candidate.strip().lower()
        if key in _SHORT_WINDOW:
            return _SHORT_WINDOW[key]
        tail = key.rsplit(":", 1)[-1].strip()
        if tail in _SHORT_WINDOW:
            return _SHORT_WINDOW[tail]
    return None


def _antigravity_member(label: str | None, shared: bool) -> str:
    if shared:
        return "Claude/GPT"
    inner = ""
    if label and "(" in label and label.endswith(")"):
        inner = label.rsplit("(", 1)[-1][:-1].strip().lower()
    return {"google": "Gemini", "anthropic": "Claude", "openai": "GPT"}.get(
        inner, inner or "其他"
    )


def _window_label(family: str, limit: dict) -> str:
    raw_label = _clean(limit.get("label"))
    window = limit.get("window")
    window = window if isinstance(window, dict) else {}
    short = _short_window(window.get("id"), window.get("label"), limit.get("id"))
    if family == "google-antigravity":
        scope = limit.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        member = _antigravity_member(raw_label, scope.get("shared") is True)
        return f"{member} {short}" if short else member
    if family == "xai-oauth":
        blob = f"{_clean(limit.get('id')) or ''} {raw_label or ''}".lower()
        prefix = "Build" if "build" in blob else "點數"
        return f"{prefix} {short}" if short else prefix
    if short is not None:
        return short
    fallback = raw_label or window.get("label") or limit.get("id")
    text = _clean(fallback) or "配額"
    return text if len(text) <= 24 else f"{text[:23].rstrip()}…"


def _account_tag(report: dict, index: int) -> str:
    """Short human tag for one omp report (email local part, max 12 chars)."""
    metadata = report.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    email = _clean(metadata.get("email"))
    if email:
        local = email.split("@")[0].strip()
        kept = "".join(char for char in local if char.isalnum() or char in "._-").strip(
            "._-"
        )
        if kept:
            return kept[:12]
    account_id = _clean(metadata.get("accountId"))
    if account_id:
        return account_id[:8]
    return f"帳號{index + 1}"


def parse_omp_payload(data: object) -> dict[str, ProviderSnapshot]:
    """Parse ``omp usage --json`` into one snapshot per known family.

    Every omp account keeps its own windows so multiple logins under one
    family (e.g. two Antigravity accounts) are all visible at once without
    switching CLIs. All quota values are stored as *used* percent; the TUI
    renders them as remaining (剩餘) percent.
    """

    def _error(note: str) -> dict[str, ProviderSnapshot]:
        return {
            family: ProviderSnapshot(
                name=f"OMP {_FAMILY_DISPLAY[family]}", plan=None, notes=[note]
            )
            for family in KNOWN_FAMILIES
        }

    if not isinstance(data, dict):
        return _error(FAILED_NOTE)
    reports = data.get("reports")
    if not isinstance(reports, list) or not reports:
        return _error(EMPTY_NOTE)

    grouped: dict[str, _FamilyData] = {}
    unknown: set[str] = set()
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            continue
        family = report.get("provider")
        if not isinstance(family, str) or family not in _FAMILY_DISPLAY:
            if isinstance(family, str) and family.strip():
                unknown.add(family.strip())
            continue
        entry = grouped.setdefault(family, _FamilyData())
        tag = _account_tag(report, index)
        if tag not in entry.accounts:
            entry.accounts.append(tag)
        order = entry.accounts.index(tag)
        metadata = report.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        plan = _clean(metadata.get("planType"))
        if plan and plan not in entry.plans:
            entry.plans.append(plan)
        limits = report.get("limits")
        if not isinstance(limits, list):
            continue
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            used = _used_percent(limit.get("amount"))
            if used is None:
                continue
            label = _window_label(family, limit)
            window = limit.get("window")
            window = window if isinstance(window, dict) else {}
            resets_at = _parse_ms_ts(window.get("resetsAt"))
            existing = next(
                (
                    item
                    for item in entry.windows
                    if item.account_tag == tag and item.label == label
                ),
                None,
            )
            if existing is None:
                entry.windows.append(
                    _AccountWindow(
                        label=label,
                        account_tag=tag,
                        account_order=order,
                        used_percent=used,
                        resets_at=resets_at,
                    )
                )
            elif used > existing.used_percent:
                existing.used_percent = used
                existing.resets_at = resets_at
            status = limit.get("status")
            if status == "exhausted" or used >= 100.0:
                note = f"{tag}·{label} 已耗盡"
                if note not in entry.notes:
                    entry.notes.append(note)

    snapshots: dict[str, ProviderSnapshot] = {}
    for family in KNOWN_FAMILIES:
        name = f"OMP {_FAMILY_DISPLAY[family]}"
        entry = grouped.get(family)
        if entry is None or not entry.windows:
            snapshots[family] = ProviderSnapshot(
                name=name, plan=None, notes=[EMPTY_NOTE]
            )
            continue
        multi = len(entry.accounts) > 1
        windows = [
            QuotaWindow(
                label=f"{item.account_tag}·{item.label}" if multi else item.label,
                used_percent=item.used_percent,
                resets_at=item.resets_at,
            )
            for item in sorted(entry.windows, key=lambda w: (w.account_order, w.label))
        ]
        plan = entry.plans[0] if entry.plans else None
        if multi:
            plan = (
                f"{plan}·共{len(entry.accounts)}帳號"
                if plan
                else (f"共{len(entry.accounts)}帳號")
            )
        snapshots[family] = ProviderSnapshot(
            name=name,
            plan=plan,
            quotas=windows,
            notes=list(entry.notes),
        )
    if unknown and _UNKNOWN_CARRIER in snapshots:
        carrier = snapshots[_UNKNOWN_CARRIER]
        carrier.notes.append(
            f"omp 另有未收錄來源（{', '.join(sorted(unknown))}），請更新 llm-usage"
        )
    return snapshots


class OhmypiStore:
    """Fetch ``omp usage --json`` at most once per TTL across providers."""

    def __init__(
        self,
        *,
        runner: Any | None = None,
        ttl: float = _DEFAULT_TTL,
        timeout: float = _DEFAULT_TIMEOUT,
        binary: str | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._ttl = max(0.0, ttl)
        self._timeout = max(0.1, timeout)
        self._binary = binary
        self._lock = threading.Lock()
        self._cached_at = float("-inf")
        self._cached: dict[str, ProviderSnapshot] | None = None

    def snapshots(self) -> dict[str, ProviderSnapshot]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < self._ttl:
                return self._cached
            payload = self._fetch()
            self._cached = build_snapshots(payload)
            self._cached_at = time.monotonic()
            return self._cached

    def snapshot_for(self, family: str) -> ProviderSnapshot:
        snapshots = self.snapshots()
        snapshot = snapshots.get(family)
        if snapshot is not None:
            return snapshot
        return ProviderSnapshot(
            name=f"OMP {_FAMILY_DISPLAY.get(family, family)}",
            plan=None,
            notes=[EMPTY_NOTE],
        )

    def _fetch(self) -> dict | None:
        binary = self._binary or trusted_which("omp")
        if binary is None:
            return None
        try:
            completed = self._runner(
                [binary, "usage", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"__error__": FAILED_NOTE}
        if completed.returncode != 0:
            return {"__error__": FAILED_NOTE}
        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", "replace")) > _MAX_OUTPUT_BYTES:
            return {"__error__": FAILED_NOTE}
        try:
            data = json.loads(stdout)
        except (ValueError, RecursionError):
            return {"__error__": FAILED_NOTE}
        return data if isinstance(data, dict) else {"__error__": FAILED_NOTE}


def build_snapshots(payload: dict | None) -> dict[str, ProviderSnapshot]:
    """Wrap :func:`parse_omp_payload` with install/failure notes."""
    if payload is None:
        note = NOT_INSTALLED_NOTE
    elif "__error__" in payload:
        note = FAILED_NOTE
    else:
        return parse_omp_payload(payload)
    return {
        family: ProviderSnapshot(
            name=f"OMP {_FAMILY_DISPLAY[family]}", plan=None, notes=[note]
        )
        for family in KNOWN_FAMILIES
    }
