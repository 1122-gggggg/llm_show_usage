import json
import math
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from llm_usage_monitor.aggregate import (
    LocalPeriod,
    local_period,
    parse_iso,
    period_requires_replay,
)
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow, clone_snapshot
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (RecursionError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 100.0 else None


def _local_date(value: datetime) -> date | None:
    try:
        return value.astimezone().date()
    except (OSError, OverflowError, ValueError):
        return None


class GrokProvider:
    name = "Grok"
    key = "grok"

    def __init__(
        self,
        root: Path | None = None,
        quota: QuotaClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if root is None:
            configured_root = os.environ.get("GROK_HOME", "").strip()
            self.root = (
                Path(configured_root).expanduser()
                if configured_root
                else Path.home() / ".grok"
            )
        else:
            self.root = root
        self._quota = quota or QuotaClient()
        self._auth = self.root / "auth.json"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reader = IncrementalJsonlReader()
        self._session_reader = IncrementalJsonlReader()
        self._session_files = CachedGlob("*/*/events.jsonl")
        self._best_ts: datetime | None = None
        self._plan: str | None = None
        self._quota_window: QuotaWindow | None = None
        self._session_date: date | None = None
        self._sessions_today: set[str] = set()
        self._session_ids_by_path: dict[Path, str] = {}
        self._last_event: datetime | None = None
        self._period: LocalPeriod | None = None

    def _unified_path(self) -> Path:
        return self.root / "logs" / "unified.jsonl"

    def snapshot(self) -> ProviderSnapshot:
        return self._snapshot(replayed=False)

    def _snapshot(self, *, replayed: bool) -> ProviderSnapshot:
        now = self._clock()
        snapshot_period = local_period(now)
        if self._period is not None and period_requires_replay(
            self._period, snapshot_period
        ):
            self._reset_incremental_state()
        self._period = snapshot_period
        today = self._roll_sessions(now)

        unified = self._unified_path()
        found_billing = self._quota_window is not None
        for line in self._reader.iter_new(unified):
            rec = _json_object(line)
            if rec is None:
                continue
            if rec.get("msg") != "billing: fetched credits config":
                continue
            ts = _timestamp(rec.get("ts"))
            if ts is None:
                continue
            if self._best_ts is not None and ts < self._best_ts:
                continue
            ctx = rec.get("ctx")
            if not isinstance(ctx, dict):
                continue
            config = ctx.get("config")
            if not isinstance(config, dict):
                continue
            raw_period = config.get("currentPeriod")
            if raw_period is not None and not isinstance(raw_period, dict):
                continue
            billing_period = raw_period or {}
            raw_period_type = billing_period.get("type")
            period_type = raw_period_type if isinstance(raw_period_type, str) else ""
            label = "週配額" if period_type.endswith("WEEKLY") else "配額"
            raw_percent = config.get("creditUsagePercent")
            used_percent = _percent(raw_percent)
            if raw_percent is not None and used_percent is None:
                continue
            self._quota_window = QuotaWindow(
                label=label,
                used_percent=used_percent,
                resets_at=_timestamp(billing_period.get("end")),
            )
            raw_plan = ctx.get("subscriptionTier")
            if isinstance(raw_plan, str) and raw_plan:
                self._plan = raw_plan
            self._best_ts = ts
            found_billing = True
            if self._last_event is None or ts > self._last_event:
                self._last_event = ts

        sessions_dir = self.root / "sessions"
        if sessions_dir.exists():
            try:
                session_paths = self._session_files.list(sessions_dir)
            except OSError:
                session_paths = []
            active_paths = set(session_paths)
            self._session_reader.prune(active_paths)
            self._session_ids_by_path = {
                path: session_id
                for path, session_id in self._session_ids_by_path.items()
                if path in active_paths
            }
            for path in session_paths:
                for line in self._session_reader.iter_new(path):
                    rec = _json_object(line)
                    if rec is None:
                        continue
                    if rec.get("type") != "turn_started":
                        continue
                    ts = _timestamp(rec.get("ts") or rec.get("timestamp"))
                    if ts is None:
                        continue
                    today = self._roll_sessions(self._clock())
                    raw_session_id = rec.get("session_id")
                    path_key = f"path:{path}"
                    if isinstance(raw_session_id, str) and raw_session_id:
                        session_key = f"id:{raw_session_id}"
                        self._session_ids_by_path[path] = session_key
                    else:
                        session_key = self._session_ids_by_path.get(path, path_key)
                    if _local_date(ts) == today:
                        if session_key != path_key:
                            self._sessions_today.discard(path_key)
                        self._sessions_today.add(session_key)
                    if self._last_event is None or ts > self._last_event:
                        self._last_event = ts

        finished = self._clock()
        finished_period = local_period(finished)
        if not replayed and period_requires_replay(snapshot_period, finished_period):
            self._reset_incremental_state()
            self._period = finished_period
            return self._snapshot(replayed=True)
        self._period = finished_period
        self._roll_sessions(finished)
        notes = ["無 token 明細,僅顯示配額"] if found_billing else ["尚無計費資料"]
        snap = ProviderSnapshot(
            name=self.name,
            plan=self._plan,
            quotas=[self._quota_window] if self._quota_window else [],
            sessions_today=len(self._sessions_today),
            last_event=self._last_event,
            notes=notes,
        )
        return self._with_live(snap)

    def _reset_incremental_state(self) -> None:
        self._reader = IncrementalJsonlReader()
        self._session_reader = IncrementalJsonlReader()
        self._best_ts = None
        self._plan = None
        self._quota_window = None
        self._session_date = None
        self._sessions_today = set()
        self._session_ids_by_path = {}
        self._last_event = None

    def _roll_sessions(self, now: datetime) -> date | None:
        today = _local_date(now)
        if today != self._session_date:
            self._sessions_today.clear()
            self._session_date = today
        return today

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        snap = clone_snapshot(snap)
        result = self._quota.grok(self._auth)
        replace = result.note != "Grok 配額讀取失敗"
        snap = apply_live(snap, result, replace=replace)
        if result.windows and result.note is None and "尚無計費資料" in snap.notes:
            snap.notes.remove("尚無計費資料")
            snap.notes.append("無 token 明細,僅顯示配額")
        return snap
