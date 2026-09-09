import json
import math
import os
import sqlite3
import time
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.model import ProviderSnapshot, TokenTotals, clone_snapshot
from llm_usage_monitor.quota import QuotaClient, QuotaResult, apply_live

FileSignature = tuple[int, int, int, int, int]
DatabaseSignature = tuple[FileSignature | None, FileSignature | None]
ParsedMessage = tuple[datetime, TokenTotals, str, float]
_MAX_TOKEN_COUNT = (1 << 53) - 1


def _opencode_data_dir() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    base = (
        Path(configured.strip()).expanduser()
        if configured is not None and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return base / "opencode"


def _sqlite_ro_uri(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name == "nt":
        resolved = _strip_windows_extended_prefix(resolved)
        if resolved.startswith("\\\\"):
            # SQLite rejects a file://server authority by default. Express a
            # UNC share as an authority-free path: file:////server/share/...
            encoded = urllib.parse.quote(resolved.replace("\\", "/"), safe="/:")
            return f"file://{encoded}?mode=ro"
    return f"{Path(resolved).as_uri()}?mode=ro"


def _strip_windows_extended_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return f"\\\\{path[8:]}"
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _nonnegative_int(value: object) -> int | None:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= _MAX_TOKEN_COUNT else None


def _nonnegative_float(value: object) -> float | None:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _parse_message(raw: object) -> ParsedMessage | None:
    try:
        data = json.loads(raw)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("role") != "assistant":
        return None

    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        return None
    cache = tokens.get("cache")
    if cache is None:
        cache = {}
    if not isinstance(cache, dict):
        return None
    counts = [
        _nonnegative_int(value)
        for value in (
            tokens.get("input"),
            tokens.get("output"),
            tokens.get("reasoning"),
            cache.get("read"),
            cache.get("write"),
        )
    ]
    if any(value is None for value in counts):
        return None
    input_tokens, output_tokens, reasoning, cached_input, cache_write = counts
    assert input_tokens is not None
    assert output_tokens is not None
    assert reasoning is not None
    assert cached_input is not None
    assert cache_write is not None
    if output_tokens > _MAX_TOKEN_COUNT - reasoning:
        return None

    raw_time = data.get("time")
    if not isinstance(raw_time, dict):
        return None
    created = raw_time.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    if isinstance(created, float) and not math.isfinite(created):
        return None
    try:
        ts = datetime.fromtimestamp(created / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None

    model = data.get("modelID")
    if not isinstance(model, str) or not model:
        model = "unknown"
    cost = _nonnegative_float(data.get("cost"))
    if cost is None:
        return None
    delta = TokenTotals(
        input=input_tokens,
        output=output_tokens + reasoning,
        cached_input=cached_input,
        cache_write=cache_write,
        reasoning=reasoning,
    )
    return ts, delta, model, cost


class OpenCodeProvider:
    name = "OpenCode"
    key = "opencode"
    _unchanged_recheck_seconds = 60.0
    _batch_size = 128

    def __init__(
        self,
        db_path: Path | None = None,
        quota: QuotaClient | None = None,
        *,
        auth_path: Path | None = None,
    ) -> None:
        data_dir = _opencode_data_dir()
        self.db_path = db_path or data_dir / "opencode.db"
        self._quota = quota or QuotaClient()
        # A copied/custom database does not prove which account produced it.
        # Fail closed unless the caller explicitly pairs it with an auth profile.
        self._auth = (
            auth_path
            if auth_path is not None
            else None
            if db_path is not None
            else data_dir / "auth.json"
        )
        self._cached: ProviderSnapshot | None = None
        self._sig: DatabaseSignature | None = None
        self._cached_period: tuple[str, int, int, int | None] | None = None
        self._last_scan_at: float | None = None

    def snapshot(self) -> ProviderSnapshot:
        now = datetime.now(UTC)
        period = self._period(now)
        sig_before = self._database_signature()
        checked_at = time.monotonic()
        if (
            sig_before is not None
            and self._sig == sig_before
            and self._cached is not None
            and self._cached_period == period
            and self._last_scan_at is not None
            and checked_at - self._last_scan_at < self._unchanged_recheck_seconds
        ):
            return self._with_live(self._cached)

        if not self.db_path.exists():
            return self._fail(f"找不到資料庫: {self.db_path}")

        today = TokenTotals()
        week = TokenTotals()
        by_model: dict[str, TokenTotals] = {}
        sessions_today: set[str] = set()
        last_event: datetime | None = None
        cost_today = 0.0
        local_now = now.astimezone()
        local_today = local_now.date()
        local_week = local_now.isocalendar()[:2]

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(_sqlite_ro_uri(self.db_path), uri=True)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=1000")
            cursor = conn.execute("SELECT session_id, data FROM message")
            while rows := cursor.fetchmany(self._batch_size):
                for session_id, raw in rows:
                    parsed = _parse_message(raw)
                    if parsed is None:
                        continue
                    ts, delta, model, cost = parsed
                    try:
                        local_ts = ts.astimezone()
                    except (OSError, OverflowError, ValueError):
                        continue
                    if local_ts.date() == local_today:
                        today.add(delta)
                        bucket = by_model.setdefault(model, TokenTotals())
                        bucket.add(delta)
                        if isinstance(session_id, str) and session_id:
                            sessions_today.add(session_id)
                        cost_today += cost
                    if local_ts.isocalendar()[:2] == local_week:
                        week.add(delta)
                    if last_event is None or ts > last_event:
                        last_event = ts
        except (sqlite3.Error, OSError):
            return self._fail()
        finally:
            if conn is not None:
                conn.close()

        snap = ProviderSnapshot(
            name=self.name,
            plan=None,
            today=today,
            week=week,
            sessions_today=len(sessions_today),
            last_event=last_event,
            notes=[],
            by_model_today=by_model,
            cost_today=cost_today,
        )
        sig_after = self._database_signature()
        # A writer committed while rows were being read. Return this coherent
        # snapshot, but force one more scan so it is not cached as the newer DB.
        self._sig = (
            sig_after if sig_before is not None and sig_before == sig_after else None
        )
        self._cached_period = period
        self._last_scan_at = time.monotonic()
        self._cached = snap
        return self._with_live(snap)

    @staticmethod
    def _period(now: datetime) -> tuple[str, int, int, int | None]:
        local = now.astimezone()
        iso = local.isocalendar()
        offset = local.utcoffset()
        return (
            local.date().isoformat(),
            iso.year,
            iso.week,
            None if offset is None else int(offset.total_seconds()),
        )

    def _database_signature(self) -> DatabaseSignature | None:
        try:
            return self._file_signature(self.db_path), self._file_signature(
                Path(f"{self.db_path}-wal")
            )
        except OSError:
            # A stat failure must never be treated as proof that the DB is unchanged.
            return None

    @staticmethod
    def _file_signature(path: Path) -> FileSignature | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        snap = clone_snapshot(snap)
        result = (
            self._quota.opencode(self._auth)
            if self._auth is not None
            else QuotaResult()
        )
        snap = apply_live(snap, result, replace=True)
        if not snap.quotas and not any(
            "login" in n or "配額讀取失敗" in n for n in snap.notes
        ):
            snap.notes.append("BYOK,無配額資料")
        return snap

    def _fail(self, note: str = "資料庫讀取失敗") -> ProviderSnapshot:
        self._sig = None
        if self._cached is not None:
            snap = clone_snapshot(self._cached)
            current_period = self._period(datetime.now(UTC))
            cached_period = self._cached_period
            if cached_period is None or cached_period[0] != current_period[0]:
                snap.today = TokenTotals()
                snap.by_model_today = {}
                snap.sessions_today = 0
                snap.cost_today = 0.0
            if cached_period is None or cached_period[1:3] != current_period[1:3]:
                snap.week = TokenTotals()
            stale_note = f"{note}（顯示上次成功資料）"
            notes = [
                existing
                for existing in snap.notes
                if "顯示上次成功資料" not in existing
            ]
            snap = replace(snap, notes=[*notes, stale_note])
            return self._with_live(snap)
        snap = ProviderSnapshot(
            name=self.name,
            plan=None,
            notes=[note],
        )
        return self._with_live(snap)
