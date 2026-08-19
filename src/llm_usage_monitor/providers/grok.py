import json
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.aggregate import parse_iso
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader


class GrokProvider:
    name = "Grok"
    key = "grok"

    def __init__(self, root: Path | None = None, quota: QuotaClient | None = None) -> None:
        if root is None:
            self.root = Path.home() / ".grok"
        else:
            self.root = root
        self._quota = quota or QuotaClient()
        self._auth = Path.home() / ".grok" / "auth.json"
        self._reader = IncrementalJsonlReader()
        self._session_reader = IncrementalJsonlReader()
        self._session_files = CachedGlob("*/*/events.jsonl")
        self._best_ts: datetime | None = None
        self._plan: str | None = None
        self._quota_window = None
        self._sessions_today = 0
        self._last_event: datetime | None = None

    def _unified_path(self) -> Path:
        return self.root / "logs" / "unified.jsonl"

    def snapshot(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        notes = ["無 token 明細,僅顯示配額"]
        unified = self._unified_path()
        if not unified.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["尚無計費資料"],
            )
            return self._with_live(snap)

        found_billing = self._quota_window is not None
        for line in self._reader.read_new(unified):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("msg") != "billing: fetched credits config":
                continue
            raw_ts = rec.get("ts")
            if not raw_ts:
                continue
            try:
                ts = parse_iso(raw_ts)
            except ValueError:
                continue
            if self._best_ts is not None and ts < self._best_ts:
                continue
            ctx = rec.get("ctx") or {}
            config = ctx.get("config") or {}
            period = config.get("currentPeriod") or {}
            period_type = str(period.get("type") or "")
            label = "週配額" if period_type.endswith("WEEKLY") else "配額"
            resets_at = None
            end = period.get("end")
            if end:
                try:
                    resets_at = parse_iso(end)
                except ValueError:
                    resets_at = None
            self._quota_window = QuotaWindow(
                label=label,
                used_percent=config.get("creditUsagePercent"),
                resets_at=resets_at,
            )
            self._plan = ctx.get("subscriptionTier")
            self._best_ts = ts
            found_billing = True
            if self._last_event is None or ts > self._last_event:
                self._last_event = ts

        if not found_billing:
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=["尚無計費資料"],
            )
            return self._with_live(snap)

        sessions_dir = self.root / "sessions"
        if sessions_dir.exists():
            for path in self._session_files.list(sessions_dir):
                for line in self._session_reader.read_new(path):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "turn_started":
                        continue
                    raw_ts = rec.get("ts") or rec.get("timestamp")
                    if not raw_ts:
                        continue
                    try:
                        ts = parse_iso(raw_ts)
                    except ValueError:
                        continue
                    if ts.astimezone().date() == now.astimezone().date():
                        self._sessions_today += 1
                    if self._last_event is None or ts > self._last_event:
                        self._last_event = ts

        snap = ProviderSnapshot(
            name=self.name,
            plan=self._plan,
            quotas=[self._quota_window] if self._quota_window else [],
            sessions_today=self._sessions_today,
            last_event=self._last_event,
            notes=notes,
        )
        return self._with_live(snap)

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        return apply_live(snap, self._quota.grok(self._auth), replace=True)
