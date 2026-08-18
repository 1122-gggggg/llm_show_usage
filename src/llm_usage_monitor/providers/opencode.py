import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.aggregate import add_to_buckets
from llm_usage_monitor.model import ProviderSnapshot, TokenTotals
from llm_usage_monitor.quota import QuotaClient, apply_live


class OpenCodeProvider:
    name = "OpenCode"

    def __init__(self, db_path: Path | None = None, quota: QuotaClient | None = None) -> None:
        self.db_path = db_path or (
            Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        )
        self._quota = quota or QuotaClient()
        self._auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        self._cached: ProviderSnapshot | None = None
        self._sig: tuple[int, int] | None = None

    def snapshot(self) -> ProviderSnapshot:
        if not self.db_path.exists():
            snap = ProviderSnapshot(
                name=self.name,
                plan=None,
                notes=[f"找不到資料庫: {self.db_path}"],
            )
            self._cached = snap
            return self._with_live(snap)

        try:
            conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
            conn.execute("PRAGMA busy_timeout=1000")
        except sqlite3.OperationalError:
            return self._fail()

        try:
            row = conn.execute("SELECT COUNT(*), MAX(time_updated) FROM message").fetchone()
            count = int(row[0] or 0)
            max_updated = int(row[1] or 0)
            sig = (count, max_updated)
            if self._sig == sig and self._cached is not None:
                return self._with_live(self._cached)
            rows = conn.execute("SELECT session_id, data FROM message").fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return self._fail()
        finally:
            conn.close()

        now = datetime.now(UTC)
        today = TokenTotals()
        week = TokenTotals()
        by_model: dict[str, TokenTotals] = {}
        sessions_today: set[str] = set()
        last_event: datetime | None = None
        cost_today = 0.0

        for session_id, raw in rows:
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if data.get("role") != "assistant" or not data.get("tokens"):
                continue
            tokens = data["tokens"]
            cache = tokens.get("cache") or {}
            reasoning = int(tokens.get("reasoning") or 0)
            delta = TokenTotals(
                input=int(tokens.get("input") or 0),
                output=int(tokens.get("output") or 0) + reasoning,
                cached_input=int(cache.get("read") or 0),
                cache_write=int(cache.get("write") or 0),
                reasoning=reasoning,
            )
            created = (data.get("time") or {}).get("created")
            if created is None:
                continue
            try:
                ts = datetime.fromtimestamp(created / 1000, tz=UTC)
            except (TypeError, ValueError, OSError):
                continue
            add_to_buckets(today, week, ts, delta, now)
            if ts.astimezone().date() == now.astimezone().date():
                model = data.get("modelID") or "unknown"
                bucket = by_model.setdefault(model, TokenTotals())
                bucket.add(delta)
                sessions_today.add(session_id)
                cost_today += float(data.get("cost") or 0)
            if last_event is None or ts > last_event:
                last_event = ts

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
        snap = self._with_live(snap)
        self._sig = sig
        self._cached = snap
        return snap

    def _with_live(self, snap: ProviderSnapshot) -> ProviderSnapshot:
        result = self._quota.opencode(self._auth)
        snap = apply_live(snap, result, replace=False)
        if not snap.quotas and not any("login" in n or "配額讀取失敗" in n for n in snap.notes):
            snap.notes.append("BYOK,無配額資料")
        return snap

    def _fail(self) -> ProviderSnapshot:
        if self._cached is not None:
            return self._with_live(self._cached)
        snap = ProviderSnapshot(
            name=self.name,
            plan=None,
            notes=["資料庫讀取失敗"],
        )
        self._cached = snap
        return self._with_live(snap)
