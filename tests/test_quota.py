from datetime import UTC, datetime
from pathlib import Path

from llm_usage_monitor.quota import (
    QuotaClient,
    parse_antigravity_cli,
    parse_claude,
    parse_codex,
    parse_grok,
    parse_opencode,
)


def test_parse_claude_five_hour_and_week() -> None:
    windows, plan = parse_claude(
        {
            "five_hour": {
                "utilization": 42.4,
                "resets_at": "2026-08-18T20:00:00Z",
            },
            "seven_day": {
                "utilization": 10.0,
                "resets_at": "2026-08-21T00:00:00Z",
            },
        }
    )
    labels = {w.label: w for w in windows}
    assert labels["5h"].used_percent == 42.4
    assert labels["5h"].resets_at == datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
    assert labels["週"].used_percent == 10.0
    assert plan is None


def test_parse_codex_classifies_window_by_duration() -> None:
    windows, plan = parse_codex(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 97,
                    "limit_window_seconds": 604800,
                    "reset_at": 1787196665,
                },
                "secondary_window": {
                    "used_percent": 12.5,
                    "limit_window_seconds": 18000,
                    "reset_at": 1786800000,
                },
            },
        }
    )
    labels = {w.label: w for w in windows}
    assert plan == "pro"
    assert labels["週"].used_percent == 97
    assert labels["5h"].used_percent == 12.5


def test_parse_grok_weekly_percent() -> None:
    windows, plan = parse_grok(
        {
            "config": {
                "creditUsagePercent": 100.0,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-08-21T08:54:37.653971+00:00",
                },
            },
            "subscriptionTier": "SuperGrok",
        }
    )
    assert plan == "SuperGrok"
    assert windows[0].label == "週配額"
    assert windows[0].used_percent == 100.0


def test_parse_opencode_go_three_windows() -> None:
    windows, plan = parse_opencode(
        {
            "usage": {
                "rolling": {
                    "status": "ok",
                    "percent": 95,
                    "resetsAt": "2026-08-18T18:43:44.061Z",
                },
                "weekly": {
                    "status": "ok",
                    "percent": 38,
                    "resetsAt": "2026-08-24T00:00:00.061Z",
                },
                "monthly": {
                    "status": "ok",
                    "percent": 29,
                    "resetsAt": "2026-09-15T04:31:34.061Z",
                },
            }
        }
    )
    labels = {w.label: w for w in windows}
    assert plan == "GO"
    assert labels["5h"].used_percent == 95
    assert labels["週"].used_percent == 38
    assert labels["月"].used_percent == 29


def test_client_caches_and_surfaces_login_note(tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        calls["n"] += 1
        return 401, {"error": "revoked"}

    creds = tmp_path / ".credentials.json"
    creds.write_text(
        '{"claudeAiOauth":{"accessToken":"x","refreshToken":"y","expiresAt":9999999999999}}',
        encoding="utf-8",
    )
    client = QuotaClient(get=fake_get, ttl=30)
    first = client.claude(creds)
    second = client.claude(creds)
    assert first.note and "login" in first.note.lower()
    assert first.windows == []
    assert second.windows == []
    assert calls["n"] == 1


def test_parse_antigravity_cli_remaining() -> None:
    windows, plan = parse_antigravity_cli(
        "Gemini Models\tWeekly Limit Remaining\t21%\t2026-08-20T17:52:30Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t74%\t2026-08-18T18:35:23Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t88%\t2026-08-21T16:16:42Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-08-18T21:52:02Z"
    )
    labels = {window.label: window for window in windows}
    assert plan == "Google AI Pro"
    assert labels["Gemini 週"].used_percent == 79
    assert labels["Gemini 5h"].used_percent == 26
    assert labels["Claude/GPT 週"].used_percent == 12
    assert labels["Claude/GPT 5h"].used_percent == 0
    assert labels["Gemini 週"].resets_at is not None


def test_quota_client_caches_for_ten_seconds(tmp_path: Path) -> None:
    calls = {"count": 0}

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        calls["count"] += 1
        return 200, {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 50,
                    "limit_window_seconds": 604800,
                    "reset_at": 1787196665,
                }
            },
        }
    client = QuotaClient(get=fake_get, ttl=10)

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"x","account_id":"a"}}',
        encoding="utf-8",
    )
    try:
        client.codex(auth)
        client.codex(auth)
    finally:
        auth.unlink(missing_ok=True)
    assert calls["count"] == 1


def test_quota_client_keeps_last_good_value_on_transient_failure(tmp_path: Path) -> None:
    responses = [
        (
            200,
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 50,
                        "limit_window_seconds": 604800,
                        "reset_at": 1787196665,
                    }
                },
            },
        ),
        (0, "network error"),
    ]

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        return responses.pop(0)

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"x","account_id":"a"}}',
        encoding="utf-8",
    )
    client = QuotaClient(get=fake_get, ttl=0)
    first = client.codex(auth)
    second = client.codex(auth)
    assert first.windows[0].used_percent == 50
    assert second.windows[0].used_percent == 50
    assert second.note == "Codex 配額讀取失敗"
