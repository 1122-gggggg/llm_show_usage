import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_usage_monitor.providers.claude import ClaudeProvider
from llm_usage_monitor.quota import QuotaClient, QuotaResult


class RecordingQuota(QuotaClient):
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def claude(self, path: Path) -> QuotaResult:
        self.paths.append(path)
        return QuotaResult()


def _line(
    ts: datetime, msg_id: str, model: str, input_tokens: int, typ: str = "assistant"
) -> str:
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


def test_claude_snapshot_does_not_expose_internal_totals(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(_line(now, "msg", "model", 10) + "\n", encoding="utf-8")
    provider = ClaudeProvider(tmp_path, quota=QuotaClient.disabled())

    first = provider.snapshot()
    first.today.input = 777
    first.by_model_today["model"].input = 888
    second = provider.snapshot()

    assert second.today.input == 10
    assert second.by_model_today["model"].input == 10


def test_claude_missing_dir(tmp_path: Path) -> None:
    snap = ClaudeProvider(tmp_path / "missing", quota=QuotaClient.disabled()).snapshot()
    assert snap.today.input == 0
    assert any("找不到日誌目錄" in n for n in snap.notes)


def test_claude_resets_today_across_date_change(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(_line(now, "msg-now", "claude-opus-5", 10) + "\n", encoding="utf-8")
    provider = ClaudeProvider(tmp_path, quota=QuotaClient.disabled())
    snap = provider.snapshot()
    assert snap.today.input == 10
    assert snap.sessions_today == 1
    week_input = snap.week.input

    provider._day = now.astimezone().date() - timedelta(days=1)
    rolled = provider.snapshot()
    assert rolled.today.input == 0
    assert rolled.sessions_today == 0
    assert rolled.by_model_today == {}
    assert rolled.week.input == week_input


def test_claude_counts_event_consumed_after_midnight(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    after_midnight = datetime(2026, 8, 24, 0, 0, 1, tzinfo=local_tz)
    before_midnight = after_midnight - timedelta(seconds=2)
    times = iter((before_midnight, after_midnight, after_midnight))
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(
        _line(
            after_midnight.astimezone(UTC),
            "after-midnight",
            "claude-opus-5",
            10,
        )
        + "\n",
        encoding="utf-8",
    )
    provider = ClaudeProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: next(times, after_midnight),
    )

    first = provider.snapshot()

    assert first.today.input == 10
    assert first.week.input == 10
    assert first.sessions_today == 1
    assert provider.snapshot().today.input == 10


def test_claude_replays_usage_when_local_date_moves_backward(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    monday = datetime(2026, 8, 24, 12, tzinfo=local_tz)
    tuesday = monday + timedelta(days=1)
    current = [monday]
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(
        _line(monday.astimezone(UTC), "monday", "model", 10) + "\n",
        encoding="utf-8",
    )
    provider = ClaudeProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: current[0],
    )

    assert provider.snapshot().today.input == 10
    current[0] = tuesday
    assert provider.snapshot().today.input == 0
    current[0] = monday
    replayed = provider.snapshot()

    assert replayed.today.input == 10
    assert replayed.sessions_today == 1


def test_claude_skips_invalid_records_and_keeps_later_valid_lines(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    huge_json_number = "9" * 5000
    bad_tokens = json.loads(_line(now, "retry-tokens", "claude-opus-5", 99))
    bad_tokens["message"]["usage"]["input_tokens"] = "not-a-number"
    negative_tokens = json.loads(_line(now, "retry-negative", "claude-opus-5", 99))
    negative_tokens["message"]["usage"]["input_tokens"] = -1
    bad_timestamp = json.loads(_line(now, "retry-timestamp", "claude-opus-5", 99))
    bad_timestamp["timestamp"] = "not-a-timestamp"
    overflowing_timestamp = json.loads(
        _line(now, "retry-overflow", "claude-opus-5", 99)
    )
    overflowing_timestamp["timestamp"] = "0001-01-01T00:00:00+14:00"
    bad_model = json.loads(_line(now, "retry-model", "claude-opus-5", 99))
    bad_model["message"]["model"] = []
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(
        "\n".join(
            [
                "[]",
                huge_json_number,
                json.dumps({"type": "assistant", "message": []}),
                json.dumps({"type": "assistant", "message": {"usage": []}}),
                json.dumps(bad_tokens),
                _line(now, "retry-tokens", "claude-opus-5", 7),
                json.dumps(negative_tokens),
                _line(now, "retry-negative", "claude-opus-5", 19),
                json.dumps(bad_timestamp),
                _line(now, "retry-timestamp", "claude-opus-5", 11),
                json.dumps(overflowing_timestamp),
                _line(now, "retry-overflow", "claude-opus-5", 17),
                json.dumps(bad_model),
                _line(now, "retry-model", "claude-opus-5", 13),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = ClaudeProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 67
    assert snap.today.output == 10
    assert snap.today.cached_input == 15
    assert snap.today.cache_write == 20
    assert snap.by_model_today["claude-opus-5"].input == 67
    assert snap.sessions_today == 1


def test_claude_seen_ids_only_tracks_the_current_iso_week(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    log = tmp_path / "proj" / "sess.jsonl"
    log.parent.mkdir()
    log.write_text(
        "\n".join(
            [
                _line(old, "old-week", "claude-opus-5", 20),
                _line(now, "current-week", "claude-opus-5", 10),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    provider = ClaudeProvider(tmp_path, quota=QuotaClient.disabled())

    provider.snapshot()

    assert provider._seen_ids == {"current-week"}

    provider._iso_week = (-1, -1)
    provider.snapshot()

    assert provider._seen_ids == set()


def test_claude_uses_config_dir_for_logs_and_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "profile"
    projects = config_dir / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    quota = RecordingQuota()

    provider = ClaudeProvider(quota=quota)
    provider.snapshot()

    assert provider.root == projects
    assert quota.paths == [config_dir / ".credentials.json"]


def test_claude_custom_root_uses_credentials_from_the_same_profile(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-profile"))
    projects = tmp_path / "selected-profile" / "projects"
    projects.mkdir(parents=True)
    quota = RecordingQuota()

    provider = ClaudeProvider(projects, quota=quota)
    provider.snapshot()

    assert provider.root == projects
    assert quota.paths == [projects.parent / ".credentials.json"]
