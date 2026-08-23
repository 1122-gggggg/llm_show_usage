import hashlib
import json
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.aggregate import (
    LocalPeriod,
    add_to_buckets,
    local_period,
    parse_iso,
    period_requires_replay,
)
from llm_usage_monitor.model import (
    ProviderSnapshot,
    QuotaWindow,
    TokenTotals,
    clone_snapshot,
)
from llm_usage_monitor.quota import QuotaClient, apply_live
from llm_usage_monitor.scan import CachedGlob, IncrementalJsonlReader

_USAGE_FIELDS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cached_input_tokens", "cached_input"),
    ("cache_write_input_tokens", "cache_write"),
    ("reasoning_output_tokens", "reasoning"),
)
_MAX_TOKEN_COUNT = 2**63 - 1


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= _MAX_TOKEN_COUNT else None


def _token_usage(value: object) -> TokenTotals | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, int] = {}
    found = False
    for source, target in _USAGE_FIELDS:
        raw = value.get(source)
        if raw is None:
            parsed[target] = 0
            continue
        amount = _nonnegative_int(raw)
        if amount is None:
            return None
        parsed[target] = amount
        found = True
    if not found or parsed["cached_input"] > parsed["input"]:
        return None
    # Codex reports cached_input_tokens as a subset of input_tokens. Store only
    # the non-cached portion in input so TokenTotals.total remains additive.
    parsed["input"] -= parsed["cached_input"]
    return TokenTotals(**parsed)


def _usage_delta(current: TokenTotals, previous: TokenTotals) -> TokenTotals | None:
    values: dict[str, int] = {}
    for _source, field in _USAGE_FIELDS:
        amount = getattr(current, field) - getattr(previous, field)
        if amount < 0:
            return None
        values[field] = amount
    return TokenTotals(**values)


def _percent(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 100.0:
        return None
    return parsed


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
    key = "codex"

    def __init__(
        self,
        root: Path | None = None,
        quota: QuotaClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if root is None:
            configured_home = os.environ.get("CODEX_HOME", "").strip()
            profile_home = (
                Path(configured_home).expanduser()
                if configured_home
                else Path.home() / ".codex"
            )
            self.root = profile_home / "sessions"
        else:
            self.root = root
            profile_home = root.parent
        self._quota = quota or QuotaClient()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._auth = profile_home / "auth.json"
        self._reader = IncrementalJsonlReader()
        self._files = CachedGlob("**/rollout-*.jsonl")
        self._today = TokenTotals()
        self._week = TokenTotals()
        self._session_files_today: set[int] = set()
        self._last_event: datetime | None = None
        self._rate_limits: dict | None = None
        self._rate_limits_ts: datetime | None = None
        self._total_usage_by_file: dict[Path, TokenTotals] = {}
        self._file_generations: dict[Path, int] = {}
        self._seen_events: set[tuple[int, bytes]] = set()
        self._logical_by_identity: dict[tuple[int, int], int] = {}
        self._logical_by_path: dict[Path, int] = {}
        self._next_logical_file = 0
        self._plan: str | None = None
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
        notes: list[str] = []
        if not self.root.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[f"找不到日誌目錄: {self.root}"],
            )
            return self._with_live(snap)
        paths = self._files.list(self.root)
        active_paths = set(paths)
        self._reader.prune(active_paths)
        self._total_usage_by_file = {
            path: total
            for path, total in self._total_usage_by_file.items()
            if path in active_paths
        }
        self._file_generations = {
            path: generation
            for path, generation in self._file_generations.items()
            if path in active_paths
        }
        for path in paths:
            for line in self._reader.iter_new(path):
                try:
                    rec = json.loads(line)
                except (RecursionError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != "event_msg":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count":
                    continue
                raw_ts = rec.get("timestamp")
                if not raw_ts:
                    continue
                try:
                    ts = parse_iso(raw_ts)
                    now = self._clock()
                    self._roll(now)
                    local_ts = ts.astimezone()
                    local_now = now.astimezone()
                except (OSError, OverflowError, TypeError, ValueError):
                    continue
                current_week = local_now.isocalendar()[:2]
                event_id = hashlib.blake2b(
                    line.encode("utf-8"), digest_size=16
                ).digest()
                logical_file = self._logical_file(path)

                info = payload.get("info")
                if not isinstance(info, dict):
                    info = {}
                last_usage = _token_usage(info.get("last_token_usage"))
                total_raw = info.get("total_token_usage")
                total_usage = _token_usage(total_raw)
                self._sync_generation(path)

                if local_ts.isocalendar()[:2] == current_week:
                    seen_key = (logical_file, event_id)
                    if seen_key in self._seen_events:
                        # A replacement generation can replay byte-identical
                        # events. They must not be counted again, but their
                        # cumulative value is still the baseline for later
                        # rate-only events in the replacement file.
                        if total_usage is not None:
                            self._total_usage_by_file[path] = total_usage
                        if local_ts.date() == local_now.date():
                            self._session_files_today.add(logical_file)
                        continue
                    self._seen_events.add(seen_key)

                # Only a missing/null cumulative field denotes the legacy schema.
                # A present but malformed counter must not replay last_token_usage.
                delta = (
                    TokenTotals()
                    if total_raw is not None and total_usage is None
                    else self._delta_for(path, total_usage, last_usage)
                )
                add_to_buckets(self._today, self._week, ts, delta, now)
                if local_ts.date() == local_now.date():
                    self._session_files_today.add(logical_file)
                if self._last_event is None or ts > self._last_event:
                    self._last_event = ts

                rate_limits = payload.get("rate_limits")
                if (
                    isinstance(rate_limits, dict)
                    and rate_limits
                    and (self._rate_limits_ts is None or ts > self._rate_limits_ts)
                ):
                    self._rate_limits = dict(rate_limits)
                    self._rate_limits_ts = ts
                    plan = rate_limits.get("plan_type")
                    self._plan = plan if isinstance(plan, str) and plan else None

        finished = self._clock()
        finished_period = local_period(finished)
        if not replayed and period_requires_replay(period, finished_period):
            self._reset_incremental_state()
            self._period = finished_period
            return self._snapshot(replayed=True)
        self._period = finished_period
        self._roll(finished)
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

    def _reset_incremental_state(self) -> None:
        self._reader = IncrementalJsonlReader()
        self._today = TokenTotals()
        self._week = TokenTotals()
        self._session_files_today = set()
        self._last_event = None
        self._rate_limits = None
        self._rate_limits_ts = None
        self._total_usage_by_file = {}
        self._file_generations = {}
        self._seen_events = set()
        self._logical_by_identity = {}
        self._logical_by_path = {}
        self._next_logical_file = 0
        self._plan = None
        self._day = None
        self._iso_week = None

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        result = self._quota.codex(self._auth)
        replace = result.note != "Codex 配額讀取失敗"
        return apply_live(clone_snapshot(snap), result, replace=replace)

    def _sync_generation(self, path: Path) -> None:
        generation = self._reader.generation(path)
        if self._file_generations.get(path) == generation:
            return
        self._file_generations[path] = generation
        self._total_usage_by_file.pop(path, None)

    def _logical_file(self, path: Path) -> int:
        identity = self._reader.identity(path)
        if identity is not None and identity in self._logical_by_identity:
            logical = self._logical_by_identity[identity]
        elif path in self._logical_by_path:
            logical = self._logical_by_path[path]
        else:
            self._next_logical_file += 1
            logical = self._next_logical_file
        self._logical_by_path[path] = logical
        if identity is not None:
            self._logical_by_identity[identity] = logical
        return logical

    def _delta_for(
        self,
        path: Path,
        total: TokenTotals | None,
        last: TokenTotals | None,
    ) -> TokenTotals:
        if total is None:
            # Older rollout files only contain last_token_usage. Keep their
            # established additive behaviour when no cumulative counter exists.
            return last or TokenTotals()

        previous = self._total_usage_by_file.get(path)
        self._total_usage_by_file[path] = total
        if previous is None:
            # A rollout can resume from an earlier cumulative total. The first
            # event's last usage is the amount attributable to this event.
            return last or TokenTotals()

        delta = _usage_delta(total, previous)
        if delta is not None:
            return delta
        # A decreasing cumulative value indicates a reset or format change.
        # Re-baseline above and use the event-local value without going negative.
        return last or TokenTotals()

    def _roll(self, now: datetime) -> None:
        local = now.astimezone()
        day = local.date()
        week = local.isocalendar()[:2]
        if self._day != day:
            self._today = TokenTotals()
            self._session_files_today = set()
            self._day = day
        if self._iso_week != week:
            self._week = TokenTotals()
            self._seen_events.clear()
            self._logical_by_identity.clear()
            self._logical_by_path.clear()
            self._iso_week = week

    def _quotas(self) -> list[QuotaWindow]:
        rl = self._rate_limits
        if not rl:
            return []
        windows: list[QuotaWindow] = []
        for key in ("primary", "secondary"):
            slot = rl.get(key)
            if not isinstance(slot, dict) or not slot:
                continue
            minutes = _nonnegative_int(slot.get("window_minutes"))
            resets_raw = slot.get("resets_at")
            resets_at = None
            if resets_raw is not None:
                try:
                    resets_at = datetime.fromtimestamp(int(resets_raw), tz=UTC)
                except (TypeError, ValueError, OSError, OverflowError):
                    resets_at = None
            windows.append(
                QuotaWindow(
                    label=_window_label(minutes),
                    used_percent=_percent(slot.get("used_percent")),
                    resets_at=resets_at,
                )
            )
        credits = rl.get("credits")
        if (
            isinstance(credits, dict)
            and credits.get("unlimited") is False
            and credits.get("balance") is not None
            and windows
        ):
            widest = max(windows, key=lambda window: _label_minutes(window.label))
            balance = credits["balance"]
            if isinstance(balance, (str, int, float)) and not isinstance(balance, bool):
                extra = f"credits {balance}"
                widest.detail = (
                    extra if not widest.detail else f"{widest.detail} {extra}"
                )
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
