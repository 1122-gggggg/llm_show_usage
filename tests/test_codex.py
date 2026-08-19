import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_usage_monitor.providers.codex import CodexProvider
from llm_usage_monitor.quota import QuotaClient


def _event(ts: datetime, last: dict, rate_limits: dict | None = None) -> str:
    rec = {
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last},
        },
    }
    if rate_limits is not None:
        rec["payload"]["rate_limits"] = rate_limits
    return json.dumps(rec)


def test_codex_sums_deltas_and_maps_quotas(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-test.jsonl"
    first = {
        "input_tokens": 10,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": 2,
        "output_tokens": 3,
        "reasoning_output_tokens": 1,
    }
    second = {
        "input_tokens": 20,
        "cached_input_tokens": 4,
        "cache_write_input_tokens": 5,
        "output_tokens": 6,
        "reasoning_output_tokens": 2,
    }
    rate_limits = {
        "primary": {"used_percent": 83.0, "window_minutes": 10080, "resets_at": int(now.timestamp()) + 3600},
        "secondary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": int(now.timestamp()) + 600},
        "credits": {"unlimited": False, "balance": "100.0"},
        "plan_type": "pro",
    }
    path.write_text(
        "\n".join(
            [
                _event(now, first),
                _event(now, second, rate_limits),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = CodexProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 30
    assert snap.today.cached_input == 5
    assert snap.today.cache_write == 7
    assert snap.today.output == 9
    assert snap.today.reasoning == 3
    assert snap.plan == "pro"
    labels = {q.label: q for q in snap.quotas}
    assert labels["5h"].used_percent == 12.5
    assert labels["週"].used_percent == 83.0
    assert labels["週"].detail is not None and "credits 100.0" in labels["週"].detail
    assert snap.sessions_today == 1


def test_codex_resets_today_across_date_change(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-test.jsonl"
    path.write_text(
        _event(now, {"input_tokens": 10, "output_tokens": 1}) + "\n",
        encoding="utf-8",
    )
    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())
    snap = provider.snapshot()
    assert snap.today.input == 10
    assert snap.sessions_today == 1
    week_input = snap.week.input

    provider._day = now.astimezone().date() - timedelta(days=1)
    rolled = provider.snapshot()
    assert rolled.today.input == 0
    assert rolled.sessions_today == 0
    assert rolled.week.input == week_input
