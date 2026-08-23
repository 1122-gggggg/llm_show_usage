import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_usage_monitor.model import QuotaWindow
from llm_usage_monitor.providers.grok import GrokProvider
from llm_usage_monitor.quota import QuotaClient, QuotaResult


def _billing(ts: str, percent: float | str, end: str, tier: str) -> str:
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


def _turn(ts: str, session_id: str | None = None) -> str:
    record = {"type": "turn_started", "ts": ts}
    if session_id is not None:
        record["session_id"] = session_id
    return json.dumps(record)


def _events(root: Path, project: str, session: str, lines: list[str]) -> Path:
    path = root / "sessions" / project / session / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _FixedQuota(QuotaClient):
    def __init__(self, result: QuotaResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    def grok(self, auth_path: Path) -> QuotaResult:
        self.paths.append(auth_path)
        return self.result


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


def test_grok_live_quota_replaces_missing_billing_note(tmp_path: Path) -> None:
    quota = _FixedQuota(
        QuotaResult(
            windows=[QuotaWindow(label="週配額", used_percent=25.0, resets_at=None)],
            plan="SuperGrok",
        )
    )

    snap = GrokProvider(tmp_path / "missing", quota=quota).snapshot()

    assert snap.quotas[0].used_percent == 25.0
    assert "尚無計費資料" not in snap.notes
    assert "無 token 明細,僅顯示配額" in snap.notes


def test_grok_transient_live_failure_keeps_missing_billing_note(tmp_path: Path) -> None:
    quota = _FixedQuota(
        QuotaResult(
            windows=[QuotaWindow(label="週配額", used_percent=25.0, resets_at=None)],
            note="Grok 配額讀取失敗",
        )
    )

    snap = GrokProvider(tmp_path / "missing", quota=quota).snapshot()

    assert "尚無計費資料" in snap.notes
    assert "無 token 明細,僅顯示配額" not in snap.notes
    assert "Grok 配額讀取失敗" in snap.notes


def test_grok_counts_unique_sessions_without_billing(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    timestamp = now.isoformat()
    _events(tmp_path, "project-a", "session-a", [_turn(timestamp, "shared")] * 2)
    _events(tmp_path, "project-b", "session-b", [_turn(timestamp, "shared")])
    _events(tmp_path, "project-c", "session-c", [_turn(timestamp)] * 2)

    snap = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: now,
    ).snapshot()

    assert snap.sessions_today == 2
    assert snap.last_event == now
    assert any("尚無計費資料" in note for note in snap.notes)


def test_grok_resets_sessions_on_local_date_change(tmp_path: Path) -> None:
    day_one = datetime(2026, 8, 22, 4, tzinfo=UTC)
    day_two = datetime(2026, 8, 23, 4, tzinfo=UTC)
    current = [day_one]
    path = _events(tmp_path, "project", "session", [_turn(day_one.isoformat(), "same")])
    provider = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: current[0],
    )

    assert provider.snapshot().sessions_today == 1
    current[0] = day_two
    assert provider.snapshot().sessions_today == 0

    path.write_text(
        path.read_text(encoding="utf-8") + _turn(day_two.isoformat(), "same") + "\n",
        encoding="utf-8",
    )
    assert provider.snapshot().sessions_today == 1


def test_grok_counts_session_consumed_after_midnight(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    after_midnight = datetime(2026, 8, 24, 0, 0, 1, tzinfo=local_tz)
    before_midnight = after_midnight - timedelta(seconds=2)
    times = iter((before_midnight, after_midnight, after_midnight))
    _events(
        tmp_path,
        "project",
        "session",
        [_turn(after_midnight.astimezone(UTC).isoformat(), "after-midnight")],
    )
    provider = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: next(times, after_midnight),
    )

    first = provider.snapshot()

    assert first.sessions_today == 1
    assert provider.snapshot().sessions_today == 1


def test_grok_replays_sessions_when_local_date_moves_backward(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    monday = datetime(2026, 8, 24, 12, tzinfo=local_tz)
    tuesday = monday + timedelta(days=1)
    current = [monday]
    _events(
        tmp_path,
        "project",
        "session",
        [_turn(monday.astimezone(UTC).isoformat(), "monday")],
    )
    provider = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: current[0],
    )

    assert provider.snapshot().sessions_today == 1
    current[0] = tuesday
    assert provider.snapshot().sessions_today == 0
    current[0] = monday

    assert provider.snapshot().sessions_today == 1


def test_grok_skips_malformed_records_individually(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    timestamp = now.isoformat()
    logs = tmp_path / "logs"
    logs.mkdir()
    malformed_billing = json.dumps(
        {
            "ts": timestamp,
            "msg": "billing: fetched credits config",
            "ctx": {"config": {"currentPeriod": []}},
        }
    )
    invalid_percent = json.dumps(
        {
            "ts": "2099-01-01T00:00:00Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "Wrong",
                "config": {
                    "creditUsagePercent": "not-a-number",
                    "currentPeriod": {},
                },
            },
        }
    )
    (logs / "unified.jsonl").write_text(
        "\n".join(
            [
                "[]",
                json.dumps({"ts": 123, "msg": "billing: fetched credits config"}),
                malformed_billing,
                _billing(timestamp, "77.5", "2026-09-01T00:00:00Z", "SuperGrok"),
                invalid_percent,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _events(
        tmp_path,
        "project",
        "session",
        [
            "null",
            json.dumps({"type": "turn_started", "ts": 123}),
            _turn(timestamp, "ok"),
        ],
    )

    snap = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: now,
    ).snapshot()

    assert snap.plan == "SuperGrok"
    assert snap.quotas[0].used_percent == 77.5
    assert snap.sessions_today == 1


@pytest.mark.parametrize("bad_percent", [-0.1, 100.1])
def test_grok_ignores_out_of_range_percent_and_keeps_last_valid(
    tmp_path: Path,
    bad_percent: float,
) -> None:
    now = datetime.now(UTC)
    later = now + timedelta(seconds=1)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text(
        "\n".join(
            [
                _billing(
                    now.isoformat(),
                    42.0,
                    (now + timedelta(days=7)).isoformat(),
                    "SuperGrok",
                ),
                _billing(
                    later.isoformat(),
                    bad_percent,
                    (later + timedelta(days=7)).isoformat(),
                    "WrongPlan",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = GrokProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: now,
    ).snapshot()

    assert snap.quotas[0].used_percent == 42.0
    assert snap.plan == "SuperGrok"


def test_grok_uses_grok_home_and_custom_root_for_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_root = tmp_path / "from-env"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("GROK_HOME", str(env_root))

    env_quota = _FixedQuota(QuotaResult())
    env_provider = GrokProvider(quota=env_quota)
    env_provider.snapshot()
    assert env_provider.root == env_root
    assert env_quota.paths == [env_root / "auth.json"]

    explicit_quota = _FixedQuota(QuotaResult())
    explicit_provider = GrokProvider(explicit_root, quota=explicit_quota)
    explicit_provider.snapshot()
    assert explicit_provider.root == explicit_root
    assert explicit_quota.paths == [explicit_root / "auth.json"]


def test_grok_transient_live_failure_keeps_local_quota(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text(
        _billing(
            "2026-08-23T00:00:00Z",
            42.0,
            "2026-08-30T00:00:00Z",
            "SuperGrok",
        )
        + "\n",
        encoding="utf-8",
    )
    quota = _FixedQuota(QuotaResult(note="Grok 配額讀取失敗"))

    snap = GrokProvider(tmp_path, quota=quota).snapshot()

    assert snap.quotas[0].used_percent == 42.0
    assert "Grok 配額讀取失敗" in snap.notes
