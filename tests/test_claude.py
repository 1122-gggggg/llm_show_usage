import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_usage_monitor.providers.claude import ClaudeProvider
from llm_usage_monitor.quota import QuotaClient


def _line(ts: datetime, msg_id: str, model: str, input_tokens: int, typ: str = "assistant") -> str:
    rec = {
        "type": typ,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            },
        },
    }
    return json.dumps(rec)


def test_claude_buckets_dedup_and_models(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    mid = now - timedelta(days=1)
    if mid.astimezone().isocalendar()[:2] != now.astimezone().isocalendar()[:2]:
        mid = now + timedelta(days=1)
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(
        "\n".join(
            [
                _line(now, "msg-now", "claude-opus-5", 10),
                _line(old, "msg-old", "claude-sonnet-4", 20),
                _line(mid, "msg-mid", "claude-haiku", 5),
                _line(now, "user-1", "claude-opus-5", 99, typ="user"),
                _line(now, "msg-now", "claude-opus-5", 10),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = ClaudeProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 10
    same_week = old.astimezone().isocalendar()[:2] == now.astimezone().isocalendar()[:2]
    assert snap.week.input == 15 + (20 if same_week else 0)
    assert snap.sessions_today == 1
    assert snap.by_model_today["claude-opus-5"].input == 10
    assert "claude-sonnet-4" not in snap.by_model_today


def test_claude_missing_dir(tmp_path: Path) -> None:
    snap = ClaudeProvider(tmp_path / "missing", quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 0
    assert any("找不到日誌目錄" in n for n in snap.notes)
