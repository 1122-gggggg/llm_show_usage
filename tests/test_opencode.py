import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_usage_monitor.providers.opencode import OpenCodeProvider
from llm_usage_monitor.quota import QuotaClient


def _insert(conn: sqlite3.Connection, msg_id: str, session_id: str, data: dict, ts: datetime) -> None:
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
    snap = OpenCodeProvider(tmp_path / "nope.db", quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 0
    assert any("找不到資料庫" in n for n in snap.notes)
