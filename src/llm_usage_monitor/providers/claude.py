import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_usage_monitor.aggregate import (
    LocalPeriod,
    add_to_buckets,
    local_period,
    parse_iso,
    period_requires_replay,
)
from llm_usage_monitor.model import ProviderSnapshot, TokenTotals, clone_snapshot
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader

_MAX_TOKEN_COUNT = 2**63 - 1


def _token_total(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        count = int(value)
    except (OverflowError, ValueError):
        return None
    return count if 0 <= count <= _MAX_TOKEN_COUNT and count == value else None


def _parse_record(line: str) -> tuple[str, datetime, TokenTotals, str] | None:
    try:
        rec = json.loads(line)
    except (RecursionError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None

    message = rec.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None

    msg_id = message.get("id")
    if not isinstance(msg_id, str) or not msg_id:
        return None
    raw_ts = rec.get("timestamp")
    if not isinstance(raw_ts, str) or not raw_ts:
        return None
    try:
        ts = parse_iso(raw_ts)
    except (OverflowError, ValueError):
        return None

    model = message.get("model")
    if model is None or model == "":
        model = "unknown"
    elif not isinstance(model, str):
        return None

    counts = [
        _token_total(usage, key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ]
    if any(count is None for count in counts):
        return None
    input_tokens, output_tokens, cached_input, cache_write = counts
    assert input_tokens is not None
    assert output_tokens is not None
    assert cached_input is not None
    assert cache_write is not None
    delta = TokenTotals(
        input=input_tokens,
        output=output_tokens,
        cached_input=cached_input,
        cache_write=cache_write,
    )
    return msg_id, ts, delta, model


class ClaudeProvider:
    name = "Claude"
    key = "claude"

    def __init__(
        self,
        root: Path | None = None,
        quota: QuotaClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        config_dir = (
            Path(configured).expanduser() if configured else Path.home() / ".claude"
        )
        if root is None:
            self.root = config_dir / "projects"
            self._creds = config_dir / ".credentials.json"
        else:
            self.root = root
            self._creds = root.parent / ".credentials.json"
        self._quota = quota or QuotaClient()
        self._clock = clock or (lambda: datetime.now(UTC))
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
        self._period: LocalPeriod | None = None

    def snapshot(self) -> ProviderSnapshot:
        return self._snapshot(replayed=False)

    def _snapshot(self, *, replayed: bool) -> ProviderSnapshot:
        now = self._clock()
        period = local_period(now)
        if self._period is not None and period_requires_replay(self._period, period):
            self._reset_incremental_state()
        self._period = period
        self._roll(now)
        if not self.root.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[f"找不到日誌目錄: {self.root}"],
            )
            return self._with_live(snap)
        paths = self._files.list(self.root)
        self._reader.prune(set(paths))
        for path in paths:
            for line in self._reader.iter_new(path):
                parsed = _parse_record(line)
                if parsed is None:
                    continue
                msg_id, ts, delta, model = parsed
                now = self._clock()
                self._roll(now)
                try:
                    local_ts = ts.astimezone()
                    local_now = now.astimezone()
                except (OSError, OverflowError, ValueError):
                    continue
                current_week = local_now.isocalendar()[:2]
                track_id = local_ts.isocalendar()[:2] == current_week
                if track_id and msg_id in self._seen_ids:
                    continue
                add_to_buckets(self._today, self._week, ts, delta, now)
                if local_ts.date() == local_now.date():
                    bucket = self._by_model_today.setdefault(model, TokenTotals())
                    bucket.add(delta)
                    self._session_stems_today.add(path.stem)
                if self._last_event is None or ts > self._last_event:
                    self._last_event = ts
                if track_id:
                    self._seen_ids.add(msg_id)

        finished = self._clock()
        finished_period = local_period(finished)
        if not replayed and period_requires_replay(period, finished_period):
            self._reset_incremental_state()
            self._period = finished_period
            return self._snapshot(replayed=True)
        self._period = finished_period
        self._roll(finished)
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

    def _reset_incremental_state(self) -> None:
        self._reader = IncrementalJsonlReader()
        self._seen_ids = set()
        self._today = TokenTotals()
        self._week = TokenTotals()
        self._by_model_today = {}
        self._session_stems_today = set()
        self._last_event = None
        self._day = None
        self._iso_week = None

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
            self._seen_ids.clear()
            self._iso_week = week

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        return apply_live(
            clone_snapshot(snap), self._quota.claude(self._creds), replace=True
        )
