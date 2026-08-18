import json
from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.providers.grok import GrokProvider
from llm_usage_monitor.quota import QuotaClient


def _billing(ts: str, percent: float, end: str, tier: str) -> str:
    return json.dumps(
        {
            "ts": ts,
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": tier,
                "config": {
                    "creditUsagePercent": percent,
                    "currentPeriod": {
                        "type": "USAGE_PERIOD_TYPE_WEEKLY",
                        "end": end,
                    },
                },
            },
        }
    )


def test_grok_takes_latest_billing(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    newer = now.isoformat().replace("+00:00", "Z")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text(
        "\n".join(
            [
                _billing("2020-01-01T00:00:00Z", 10.0, "2020-01-08T00:00:00Z", "Free"),
                _billing(newer, 77.0, "2026-08-21T08:54:37.653971+00:00", "SuperGrok"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = GrokProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()
    assert snap.plan == "SuperGrok"
    assert snap.quotas[0].label == "週配額"
    assert snap.quotas[0].used_percent == 77.0
    assert snap.quotas[0].resets_at is not None
    assert snap.today.input == 0
    assert snap.today.output == 0
    assert snap.notes


def test_grok_missing_billing(tmp_path: Path) -> None:
    snap = GrokProvider(tmp_path / "missing", quota=QuotaClient.disabled()).snapshot()
    assert any("尚無計費資料" in n for n in snap.notes)
