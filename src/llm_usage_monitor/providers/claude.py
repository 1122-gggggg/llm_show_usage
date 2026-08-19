import json
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.aggregate import add_to_buckets, parse_iso
from llm_usage_monitor.model import ProviderSnapshot, TokenTotals
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader


class ClaudeProvider:
    name = "Claude"
    key = "claude"

    def __init__(self, root: Path | None = None, quota: QuotaClient | None = None) -> None:
        self.root = root or Path.home() / ".claude" / "projects"
        self._quota = quota or QuotaClient()
        self._creds = Path.home() / ".claude" / ".credentials.json"
        self._reader = IncrementalJsonlReader()
        self._files = CachedGlob("**/*.jsonl")
        self._seen_ids: set[str] = set()
        self._today = TokenTotals()
        self._week = TokenTotals()
        self._by_model_today: dict[str, TokenTotals] = {}
        self._session_stems_today: set[str] = set()
        self._last_event: datetime | None = None
        self._day = None
        self._iso_week = None

    def snapshot(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        if not self.root.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[f"找不到日誌目錄: {self.root}"],
            )
            return self._with_live(snap)
        self._roll(now)

        for path in self._files.list(self.root):
            for line in self._reader.read_new(path):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                message = rec.get("message") or {}
                usage = message.get("usage")
                if not usage:
                    continue
                msg_id = message.get("id")
                if not msg_id or msg_id in self._seen_ids:
                    continue
                self._seen_ids.add(msg_id)

                raw_ts = rec.get("timestamp")
                if not raw_ts:
                    continue
                try:
                    ts = parse_iso(raw_ts)
                except ValueError:
                    continue

                delta = TokenTotals(
                    input=int(usage.get("input_tokens") or 0),
                    output=int(usage.get("output_tokens") or 0),
                    cached_input=int(usage.get("cache_read_input_tokens") or 0),
                    cache_write=int(usage.get("cache_creation_input_tokens") or 0),
                )
                add_to_buckets(self._today, self._week, ts, delta, now)
                if ts.astimezone().date() == now.astimezone().date():
                    model = message.get("model") or "unknown"
                    bucket = self._by_model_today.setdefault(model, TokenTotals())
                    bucket.add(delta)
                    self._session_stems_today.add(path.stem)
                if self._last_event is None or ts > self._last_event:
                    self._last_event = ts

        quotas = []
        snap = ProviderSnapshot(
            name=self.name,
            plan=None,
            quotas=quotas,
            today=self._today,
            week=self._week,
            sessions_today=len(self._session_stems_today),
            last_event=self._last_event,
            by_model_today=self._by_model_today,
        )
        return self._with_live(snap)


    def _roll(self, now: datetime) -> None:
        local = now.astimezone()
        day = local.date()
        week = local.isocalendar()[:2]
        if self._day != day:
            self._today = TokenTotals()
            self._by_model_today = {}
            self._session_stems_today = set()
            self._day = day
        if self._iso_week != week:
            self._week = TokenTotals()
            self._iso_week = week

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        return apply_live(snap, self._quota.claude(self._creds), replace=True)

