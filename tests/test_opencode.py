import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from types import SimpleNamespace

import llm_usage_monitor.providers.opencode as opencode_module
from llm_usage_monitor.model import QuotaWindow
from llm_usage_monitor.providers.opencode import OpenCodeProvider
from llm_usage_monitor.quota import LOGIN_NOTE, QuotaClient, QuotaResult


def _insert(
    conn: sqlite3.Connection, msg_id: str, session_id: str, data: dict, ts: datetime
) -> None:
    ms = int(ts.timestamp() * 1000)
    conn.execute(
        "INSERT INTO message(id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
        (msg_id, session_id, ms, ms, json.dumps(data)),
    )


def test_opencode_models_week_cost_and_reasoning(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    mid = now - timedelta(days=1)
    if mid.astimezone().isocalendar()[:2] != now.astimezone().isocalendar()[:2]:
        mid = now + timedelta(days=1)
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    _insert(
        conn,
        "a1",
        "s1",
        {
            "role": "assistant",
            "modelID": "deepseek/deepseek-v4-pro-0813-free",
            "tokens": {
                "input": 10,
                "output": 8,
                "reasoning": 11,
                "cache": {"read": 1, "write": 2},
            },
            "cost": 0.12,
            "time": {"created": int(now.timestamp() * 1000)},
        },
        now,
    )
    _insert(
        conn,
        "a2",
        "s2",
        {
            "role": "assistant",
            "modelID": "kimi-k2.5",
            "tokens": {
                "input": 5,
                "output": 1,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "cost": 0.01,
            "time": {"created": int(now.timestamp() * 1000)},
        },
        now,
    )
    _insert(
        conn,
        "a3",
        "s3",
        {
            "role": "assistant",
            "modelID": "old-model",
            "tokens": {
                "input": 7,
                "output": 3,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "cost": 9.0,
            "time": {"created": int(old.timestamp() * 1000)},
        },
        old,
    )
    _insert(
        conn,
        "a4",
        "s4",
        {
            "role": "assistant",
            "modelID": "mid-model",
            "tokens": {
                "input": 4,
                "output": 0,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "cost": 0.0,
            "time": {"created": int(mid.timestamp() * 1000)},
        },
        mid,
    )
    _insert(
        conn,
        "u1",
        "s1",
        {
            "role": "user",
            "tokens": {"input": 999, "output": 999},
            "cost": 1.0,
            "time": {"created": int(now.timestamp() * 1000)},
        },
        now,
    )
    conn.commit()
    conn.close()

    snap = OpenCodeProvider(db, quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 15
    assert snap.today.output == 8 + 11 + 1
    assert snap.today.reasoning == 11
    same_week = old.astimezone().isocalendar()[:2] == now.astimezone().isocalendar()[:2]
    assert snap.week.input == 19 + (7 if same_week else 0)
    assert snap.cost_today == 0.13
    assert snap.by_model_today["deepseek/deepseek-v4-pro-0813-free"].output == 19
    assert snap.by_model_today["kimi-k2.5"].input == 5
    assert "old-model" not in snap.by_model_today
    assert snap.sessions_today == 2
    assert snap.notes == ["BYOK,無配額資料"]


def test_opencode_missing_db(tmp_path: Path) -> None:
    snap = OpenCodeProvider(
        tmp_path / "nope.db", quota=QuotaClient.disabled()
    ).snapshot()
    assert snap.today.input == 0
    assert any("找不到資料庫" in n for n in snap.notes)


class _FrozenDatetime(datetime):
    current = datetime.now(UTC)
    local_timezone = timezone(timedelta(hours=8))

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(cls.current.timestamp(), tz)

    def astimezone(self, tz=None):
        return super().astimezone(tz or self.local_timezone)


def _single_message_db(path: Path, ts: datetime) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    _insert(
        conn,
        "a1",
        "s1",
        {
            "role": "assistant",
            "modelID": "test-model",
            "tokens": {
                "input": 10,
                "output": 2,
                "reasoning": 1,
                "cache": {"read": 0, "write": 0},
            },
            "cost": 0.01,
            "time": {"created": int(ts.timestamp() * 1000)},
        },
        ts,
    )
    conn.commit()
    conn.close()


def test_opencode_cache_recomputes_at_local_day_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    local_tz = _FrozenDatetime.local_timezone
    before_midnight = datetime(2026, 8, 18, 23, 59, tzinfo=local_tz)
    after_midnight = before_midnight + timedelta(minutes=2)
    db = tmp_path / "opencode.db"
    _single_message_db(db, before_midnight)
    monkeypatch.setattr(
        "llm_usage_monitor.providers.opencode.datetime", _FrozenDatetime
    )
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())

    _FrozenDatetime.current = before_midnight.astimezone(UTC)
    assert provider.snapshot().today.input == 10
    _FrozenDatetime.current = after_midnight.astimezone(UTC)
    assert provider.snapshot().today.input == 0


def test_opencode_cache_recomputes_at_local_week_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    local_tz = _FrozenDatetime.local_timezone
    sunday = datetime(2026, 8, 23, 23, 59, tzinfo=local_tz)
    monday = sunday + timedelta(minutes=2)
    db = tmp_path / "opencode.db"
    _single_message_db(db, sunday)
    monkeypatch.setattr(
        "llm_usage_monitor.providers.opencode.datetime", _FrozenDatetime
    )
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())

    _FrozenDatetime.current = sunday.astimezone(UTC)
    assert provider.snapshot().week.input == 10
    _FrozenDatetime.current = monday.astimezone(UTC)
    assert provider.snapshot().week.input == 0


def test_opencode_unchanged_db_uses_file_signature_cache(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    _single_message_db(db, now)
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())
    first = provider.snapshot()

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("unchanged DB should not be reopened")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.opencode.sqlite3.connect", unexpected_connect
    )
    second = provider.snapshot()
    assert second.today.input == first.today.input == 10


def test_opencode_periodic_recheck_bounds_stale_file_signature(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    _single_message_db(db, now)
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())
    assert provider.snapshot().today.input == 10
    unchanged_signature = provider._sig

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT data FROM message WHERE id = 'a1'").fetchone()
    data = json.loads(row[0])
    data["tokens"]["input"] = 20
    conn.execute("UPDATE message SET data = ? WHERE id = 'a1'", (json.dumps(data),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(provider, "_database_signature", lambda: unchanged_signature)
    provider._last_scan_at -= provider._unchanged_recheck_seconds + 1
    assert provider.snapshot().today.input == 20


def test_opencode_transient_db_failure_keeps_last_good_with_warning(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    _single_message_db(db, now)
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())
    first = provider.snapshot()

    with monkeypatch.context() as patch:
        patch.setattr(
            provider,
            "_database_signature",
            lambda: ((0, 0, 0, 0, 0), None),
        )

        def failed_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("temporarily busy")

        patch.setattr(
            "llm_usage_monitor.providers.opencode.sqlite3.connect", failed_connect
        )
        stale = provider.snapshot()

    assert stale.today.input == first.today.input == 10
    assert stale is not first
    assert any(
        "資料庫讀取失敗" in note and "顯示上次成功資料" in note for note in stale.notes
    )
    assert not any("顯示上次成功資料" in note for note in first.notes)

    recovered = provider.snapshot()
    assert recovered.today.input == 10
    assert not any("顯示上次成功資料" in note for note in recovered.notes)


def test_opencode_failure_does_not_carry_usage_across_day_or_week(
    tmp_path: Path, monkeypatch
) -> None:
    local_tz = _FrozenDatetime.local_timezone
    sunday = datetime(2026, 8, 23, 23, 59, tzinfo=local_tz)
    monday = sunday + timedelta(minutes=2)
    db = tmp_path / "opencode.db"
    _single_message_db(db, sunday)
    monkeypatch.setattr(
        "llm_usage_monitor.providers.opencode.datetime", _FrozenDatetime
    )
    provider = OpenCodeProvider(db, quota=QuotaClient.disabled())

    _FrozenDatetime.current = sunday.astimezone(UTC)
    assert provider.snapshot().today.input == 10
    assert provider.snapshot().week.input == 10
    _FrozenDatetime.current = monday.astimezone(UTC)
    monkeypatch.setattr(
        provider,
        "_database_signature",
        lambda: ((0, 0, 0, 0, 0), None),
    )
    monkeypatch.setattr(
        opencode_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("temporarily busy")
        ),
    )

    stale = provider.snapshot()

    assert stale.today.input == 0
    assert stale.week.input == 0
    assert stale.sessions_today == 0
    assert stale.by_model_today == {}
    assert stale.cost_today == 0.0


def test_opencode_default_paths_follow_xdg_and_custom_auth_is_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    xdg_data = tmp_path / "xdg data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    default = OpenCodeProvider(quota=QuotaClient.disabled())
    assert default.db_path == xdg_data / "opencode" / "opencode.db"
    assert default._auth == xdg_data / "opencode" / "auth.json"

    copied_db = tmp_path / "copied.db"
    custom_auth = tmp_path / "profile" / "auth.json"
    conservative = OpenCodeProvider(copied_db, quota=QuotaClient.disabled())
    explicit = OpenCodeProvider(
        copied_db,
        quota=QuotaClient.disabled(),
        auth_path=custom_auth,
    )
    assert conservative._auth is None
    assert explicit._auth == custom_auth


def test_opencode_strips_windows_extended_path_prefixes() -> None:
    assert (
        opencode_module._strip_windows_extended_prefix(r"\\?\C:\data\open.db")
        == r"C:\data\open.db"
    )
    assert (
        opencode_module._strip_windows_extended_prefix(r"\\?\UNC\server\share\open.db")
        == r"\\server\share\open.db"
    )


def test_opencode_unc_uri_has_no_sqlite_authority(monkeypatch) -> None:
    monkeypatch.setattr(opencode_module.os, "name", "nt")
    path = SimpleNamespace(resolve=lambda: r"\\?\UNC\server\share\open code#.db")

    assert opencode_module._sqlite_ro_uri(path) == (
        "file:////server/share/open%20code%23.db?mode=ro"
    )


def test_opencode_uses_encoded_read_only_uri_and_query_only(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "open#code%.db"
    _single_message_db(db, now)
    real_connect = sqlite3.connect
    opened: list[tuple[object, dict]] = []
    statements: list[str] = []

    def tracking_connect(database, *args, **kwargs):
        opened.append((database, kwargs))
        conn = real_connect(database, *args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(opencode_module.sqlite3, "connect", tracking_connect)
    snap = OpenCodeProvider(db, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 10
    assert opened == [(f"{db.resolve().as_uri()}?mode=ro", {"uri": True})]
    assert "%23" in opened[0][0] and "%25" in opened[0][0]
    assert any(statement == "PRAGMA query_only=ON" for statement in statements)
    assert "%3F" in opencode_module._sqlite_ro_uri(tmp_path / "open?code.db")


def test_opencode_streams_large_result_in_bounded_batches(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    db.touch()
    raw = json.dumps(
        {
            "role": "assistant",
            "modelID": "batch-model",
            "tokens": {"input": 1, "output": 0, "reasoning": 0},
            "cost": 0,
            "time": {"created": int(now.timestamp() * 1000)},
        }
    )
    source = iter((f"session-{index}", raw) for index in range(1000))
    batch_lengths: list[int] = []

    class Cursor:
        def fetchmany(self, size: int):
            batch = list(islice(source, size))
            batch_lengths.append(len(batch))
            return batch

        def fetchall(self):
            raise AssertionError("OpenCode rows must not be fetched all at once")

    class Connection:
        closed = False

        def execute(self, statement: str):
            if statement.startswith("SELECT"):
                return Cursor()
            return self

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        opencode_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    snap = OpenCodeProvider(db, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 1000
    assert snap.sessions_today == 1000
    assert connection.closed
    assert len(batch_lengths) > 2
    assert max(batch_lengths) <= OpenCodeProvider._batch_size


def test_opencode_skips_malformed_rows_without_losing_valid_usage(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    _insert(
        conn,
        "valid",
        "session-valid",
        {
            "role": "assistant",
            "modelID": "valid-model",
            "tokens": {"input": 10, "output": 2, "reasoning": 1},
            "cost": 0.5,
            "time": {"created": int(now.timestamp() * 1000)},
        },
        now,
    )
    ms = int(now.timestamp() * 1000)
    malformed = [
        "{",
        "null",
        "[]",
        json.dumps({"role": "assistant", "tokens": "bad"}),
        json.dumps({"role": "assistant", "tokens": {"input": -1}}),
        json.dumps(
            {
                "role": "assistant",
                "tokens": {"input": 1},
                "time": [],
            }
        ),
        json.dumps(
            {
                "role": "assistant",
                "tokens": {"input": 1},
                "time": {"created": ms},
                "cost": "not-a-number",
            }
        ),
        json.dumps(
            {
                "role": "assistant",
                "tokens": {"input": 1},
                "time": {"created": 10**1000},
            }
        ),
        json.dumps(
            {
                "role": "assistant",
                "tokens": {"input": 10**4000},
                "time": {"created": ms},
            }
        ),
        b"\xff\xfe",
    ]
    conn.executemany(
        "INSERT INTO message(id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (f"bad-{index}", "bad-session", ms, ms, raw)
            for index, raw in enumerate(malformed)
        ],
    )
    conn.commit()
    conn.close()

    snap = OpenCodeProvider(db, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 10
    assert snap.today.output == 3
    assert snap.cost_today == 0.5
    assert snap.sessions_today == 1
    assert list(snap.by_model_today) == ["valid-model"]


def test_opencode_live_cache_keeps_transient_but_clears_permanent_failure(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "opencode.db"
    _single_message_db(db, now)
    responses = iter(
        (
            QuotaResult(
                windows=[
                    QuotaWindow(
                        label="5h",
                        used_percent=25.0,
                        resets_at=now + timedelta(hours=1),
                    )
                ],
                plan="OpenCode Go",
            ),
            QuotaResult(
                windows=[
                    QuotaWindow(
                        label="5h",
                        used_percent=25.0,
                        resets_at=now + timedelta(hours=1),
                    )
                ],
                plan="OpenCode Go",
                note="OpenCode 配額讀取失敗",
            ),
            QuotaResult(note=LOGIN_NOTE),
        )
    )

    class SequenceQuota:
        def opencode(self, _auth_path: Path) -> QuotaResult:
            return next(responses)

    provider = OpenCodeProvider(
        db,
        quota=SequenceQuota(),
        auth_path=tmp_path / "auth.json",
    )
    first = provider.snapshot()

    assert first.plan == "OpenCode Go"
    assert len(first.quotas) == 1
    assert provider._cached is not None
    assert provider._cached.plan is None
    assert provider._cached.quotas == []

    first.today.input = 999
    first.notes.append("caller mutation")
    second = provider.snapshot()

    assert second.today.input == 10
    assert second.plan == "OpenCode Go"
    assert len(second.quotas) == 1
    assert second.notes == ["OpenCode 配額讀取失敗"]

    third = provider.snapshot()

    assert third.today.input == 10
    assert third.plan is None
    assert third.quotas == []
    assert third.notes == [LOGIN_NOTE]
