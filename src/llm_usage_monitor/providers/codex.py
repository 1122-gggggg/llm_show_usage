import json
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.aggregate import add_to_buckets, parse_iso
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow, TokenTotals
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import IncrementalJsonlReader


def _window_label(minutes: int | None) -> str:
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "週"
    if minutes is None:
        return "配額"
    return f"{minutes}min"


class CodexProvider:
    name = "Codex"

    def __init__(self, root: Path | None = None, quota: QuotaClient | None = None) -> None:
        self.root = root or Path.home() / ".codex" / "sessions"
        self._quota = quota or QuotaClient()
        self._auth = Path.home() / ".codex" / "auth.json"
        self._reader = IncrementalJsonlReader()
        self._today = TokenTotals()
        self._week = TokenTotals()
        self._session_files_today: set[Path] = set()
        self._last_event: datetime | None = None
        self._rate_limits: dict | None = None
        self._rate_limits_ts: datetime | None = None
        self._plan: str | None = None

    def snapshot(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        notes: list[str] = []
        if not self.root.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[f"找不到日誌目錄: {self.root}"],
            )
            return self._with_live(snap)

        for path in self.root.glob("**/rollout-*.jsonl"):
            for line in self._reader.read_new(path):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "event_msg":
                    continue
                payload = rec.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                raw_ts = rec.get("timestamp")
                if not raw_ts:
                    continue
                try:
                    ts = parse_iso(raw_ts)
                except ValueError:
                    continue

                info = (payload.get("info") or {}).get("last_token_usage") or {}
                delta = TokenTotals(
                    input=int(info.get("input_tokens") or 0),
                    output=int(info.get("output_tokens") or 0),
                    cached_input=int(info.get("cached_input_tokens") or 0),
                    cache_write=int(info.get("cache_write_input_tokens") or 0),
                    reasoning=int(info.get("reasoning_output_tokens") or 0),
                )
                add_to_buckets(self._today, self._week, ts, delta, now)
                if ts.astimezone().date() == now.astimezone().date():
                    self._session_files_today.add(path)
                if self._last_event is None or ts > self._last_event:
                    self._last_event = ts

                rate_limits = payload.get("rate_limits")
                if rate_limits and (self._rate_limits_ts is None or ts > self._rate_limits_ts):
                    self._rate_limits = rate_limits
                    self._rate_limits_ts = ts
                    self._plan = rate_limits.get("plan_type")

        snap = ProviderSnapshot(
            name=self.name,
            plan=self._plan,
            quotas=self._quotas(),
            today=self._today,
            week=self._week,
            sessions_today=len(self._session_files_today),
            last_event=self._last_event,
            notes=notes,
        )
        return self._with_live(snap)

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        return apply_live(snap, self._quota.codex(self._auth), replace=True)

    def _quotas(self) -> list[QuotaWindow]:
        rl = self._rate_limits
        if not rl:
            return []
        windows: list[QuotaWindow] = []
        for key in ("primary", "secondary"):
            slot = rl.get(key)
            if not slot:
                continue
            minutes = slot.get("window_minutes")
            resets_raw = slot.get("resets_at")
            resets_at = None
            if resets_raw is not None:
                try:
                    resets_at = datetime.fromtimestamp(int(resets_raw), tz=UTC)
                except (TypeError, ValueError, OSError):
                    resets_at = None
            windows.append(
                QuotaWindow(
                    label=_window_label(minutes),
                    used_percent=slot.get("used_percent"),
                    resets_at=resets_at,
                )
            )
        credits = rl.get("credits")
        if (
            credits
            and credits.get("unlimited") is False
            and credits.get("balance") is not None
            and windows
        ):
            widest = max(windows, key=lambda window: _label_minutes(window.label))
            extra = f"credits {credits['balance']}"
            widest.detail = extra if not widest.detail else f"{widest.detail} {extra}"
        return windows


def _label_minutes(label: str) -> int:
    if label == "週":
        return 10080
    if label == "5h":
        return 300
    if label.endswith("min"):
        try:
            return int(label[:-3])
        except ValueError:
            return 0
    return 0
