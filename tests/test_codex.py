import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_usage_monitor.providers.codex import CodexProvider
from llm_usage_monitor.quota import QuotaClient, QuotaResult


def _event(
    ts: datetime,
    last: dict,
    rate_limits: dict | None = None,
    total: dict | None = None,
) -> str:
    rec = {
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last},
        },
    }
    if total is not None:
        rec["payload"]["info"]["total_token_usage"] = total
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
        "primary": {
            "used_percent": 83.0,
            "window_minutes": 10080,
            "resets_at": int(now.timestamp()) + 3600,
        },
        "secondary": {
            "used_percent": 12.5,
            "window_minutes": 300,
            "resets_at": int(now.timestamp()) + 600,
        },
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
    assert snap.today.input == 25
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


def test_codex_counts_event_consumed_after_midnight(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    after_midnight = datetime(2026, 8, 24, 0, 0, 1, tzinfo=local_tz)
    before_midnight = after_midnight - timedelta(seconds=2)
    times = iter((before_midnight, after_midnight, after_midnight))
    path = tmp_path / "rollout-test.jsonl"
    path.write_text(
        _event(after_midnight.astimezone(UTC), {"input_tokens": 10}) + "\n",
        encoding="utf-8",
    )
    provider = CodexProvider(
        tmp_path,
        quota=QuotaClient.disabled(),
        clock=lambda: next(times, after_midnight),
    )

    first = provider.snapshot()

    assert first.today.input == 10
    assert first.week.input == 10
    assert first.sessions_today == 1
    assert provider.snapshot().today.input == 10


def test_codex_replays_usage_when_local_date_moves_backward(tmp_path: Path) -> None:
    local_tz = datetime.now().astimezone().tzinfo
    monday = datetime(2026, 8, 24, 12, tzinfo=local_tz)
    tuesday = monday + timedelta(days=1)
    current = [monday]
    path = tmp_path / "rollout-test.jsonl"
    path.write_text(
        _event(monday.astimezone(UTC), {"input_tokens": 10}) + "\n",
        encoding="utf-8",
    )
    provider = CodexProvider(
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


def test_codex_uses_matching_profile_for_sessions_and_auth(
    tmp_path: Path, monkeypatch
) -> None:
    env_home = tmp_path / "env-profile"
    (env_home / "sessions").mkdir(parents=True)
    explicit_sessions = tmp_path / "explicit-profile" / "sessions"
    explicit_sessions.mkdir(parents=True)
    seen_auth: list[Path] = []
    quota = QuotaClient.disabled()
    monkeypatch.setattr(
        quota,
        "codex",
        lambda path: seen_auth.append(path) or QuotaResult(),
    )
    monkeypatch.setenv("CODEX_HOME", str(env_home))

    env_provider = CodexProvider(quota=quota)
    env_provider.snapshot()
    explicit_provider = CodexProvider(explicit_sessions, quota=quota)
    explicit_provider.snapshot()

    assert env_provider.root == env_home / "sessions"
    assert explicit_provider.root == explicit_sessions
    assert seen_auth == [env_home / "auth.json", explicit_sessions.parent / "auth.json"]


def test_codex_diffs_cumulative_usage_and_ignores_rate_only_replay(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-test.jsonl"
    first = {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
        "reasoning_output_tokens": 1,
    }
    latest_total = {
        "input_tokens": 25,
        "cached_input_tokens": 5,
        "output_tokens": 7,
        "reasoning_output_tokens": 2,
    }
    rate_limits = {
        "primary": {
            "used_percent": 50,
            "window_minutes": 300,
            "resets_at": int(now.timestamp()) + 3600,
        }
    }
    path.write_text(
        "\n".join(
            [
                _event(now, first, total=first),
                _event(now, first, rate_limits=rate_limits, total=first),
                _event(now, {"input_tokens": 999}, total=latest_total),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = CodexProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 20
    assert snap.today.cached_input == 5
    assert snap.today.output == 7
    assert snap.today.reasoning == 2
    assert snap.today.total == 32
    assert snap.quotas[0].used_percent == 50


def test_codex_uses_last_usage_for_first_cumulative_value_and_counter_reset(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-resumed.jsonl"
    path.write_text(
        "\n".join(
            [
                _event(
                    now,
                    {"input_tokens": 10},
                    total={"input_tokens": 1_000},
                ),
                _event(
                    now,
                    {"input_tokens": 2},
                    total={"input_tokens": 2},
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = CodexProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 12


def test_codex_rebaselines_cumulative_usage_after_atomic_replacement(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-current.jsonl"
    path.write_text(
        _event(now, {"input_tokens": 10}, total={"input_tokens": 50}) + "\n",
        encoding="utf-8",
    )
    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())
    assert provider.snapshot().today.input == 10

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(
        _event(now, {"input_tokens": 7}, total={"input_tokens": 100}) + "\n",
        encoding="utf-8",
    )
    replacement.replace(path)

    assert provider.snapshot().today.input == 17


def test_codex_does_not_recount_byte_identical_atomic_replacement(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-current.jsonl"
    content = _event(now, {"input_tokens": 10}, total={"input_tokens": 50}) + "\n"
    path.write_text(content, encoding="utf-8")
    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())
    assert provider.snapshot().today.input == 10

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(content, encoding="utf-8")
    replacement.replace(path)

    assert provider.snapshot().today.input == 10

    rate_limits = {
        "primary": {
            "used_percent": 25,
            "window_minutes": 300,
        }
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            _event(
                now + timedelta(seconds=1),
                {"input_tokens": 10},
                rate_limits=rate_limits,
                total={"input_tokens": 50},
            )
            + "\n"
        )

    rate_only = provider.snapshot()
    assert rate_only.today.input == 10
    assert rate_only.quotas[0].used_percent == 25


def test_codex_deduplicates_hardlink_aliases(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-one.jsonl"
    alias = tmp_path / "rollout-two.jsonl"
    path.write_text(_event(now, {"input_tokens": 10}) + "\n", encoding="utf-8")
    try:
        os.link(path, alias)
    except OSError:
        return

    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())
    snap = provider.snapshot()

    assert snap.today.input == 10
    assert snap.sessions_today == 1

    selected = next(iter(provider._reader._offsets))
    selected.unlink()
    refreshed = provider.snapshot()
    assert refreshed.today.input == 10
    assert refreshed.sessions_today == 1


def test_codex_snapshot_does_not_expose_internal_totals(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-test.jsonl"
    path.write_text(_event(now, {"input_tokens": 10}) + "\n", encoding="utf-8")
    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())

    first = provider.snapshot()
    first.today.input = 777
    first.week.input = 888
    second = provider.snapshot()

    assert second.today.input == 10
    assert second.week.input == 10


def test_codex_counts_identical_events_from_independent_rollouts(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    content = _event(now, {"input_tokens": 10}) + "\n"
    (tmp_path / "rollout-one.jsonl").write_text(content, encoding="utf-8")
    (tmp_path / "rollout-two.jsonl").write_text(content, encoding="utf-8")

    snap = CodexProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 20
    assert snap.sessions_today == 2


def test_codex_keeps_independent_rollouts_separate_without_inode_identity(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime.now(UTC)
    content = _event(now, {"input_tokens": 10}) + "\n"
    (tmp_path / "rollout-one.jsonl").write_text(content, encoding="utf-8")
    (tmp_path / "rollout-two.jsonl").write_text(content, encoding="utf-8")
    provider = CodexProvider(tmp_path, quota=QuotaClient.disabled())
    monkeypatch.setattr(provider._reader, "identity", lambda _path: None)

    snap = provider.snapshot()

    assert snap.today.input == 20
    assert snap.sessions_today == 2


def test_codex_skips_malformed_records_without_losing_later_events(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "rollout-test.jsonl"
    malformed_info = {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": "invalid"},
    }
    malformed_limits = {
        "primary": "invalid",
        "credits": [],
        "plan_type": [],
    }
    path.write_text(
        "\n".join(
            [
                _event(now, {"input_tokens": 10}),
                "[]",
                json.dumps(malformed_info),
                _event(now, {"input_tokens": "not-a-number"}),
                _event(
                    now,
                    {"input_tokens": 1, "cached_input_tokens": 2},
                ),
                _event(
                    now,
                    {"input_tokens": 99},
                    total={"input_tokens": "not-a-number"},
                ),
                _event(
                    now,
                    {"input_tokens": 99},
                    total={"input_tokens": 1, "cached_input_tokens": 2},
                ),
                _event(now, {"input_tokens": 5}, rate_limits=malformed_limits),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = CodexProvider(tmp_path, quota=QuotaClient.disabled()).snapshot()

    assert snap.today.input == 15
    assert snap.plan is None
    assert snap.quotas == []


def test_codex_transient_live_failure_keeps_local_rate_window(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    rate_limits = {
        "primary": {
            "used_percent": 42,
            "window_minutes": 300,
            "resets_at": int(now.timestamp()) + 3600,
        }
    }
    (tmp_path / "rollout-test.jsonl").write_text(
        _event(now, {"input_tokens": 1}, rate_limits=rate_limits) + "\n",
        encoding="utf-8",
    )

    class TransientQuota:
        def codex(self, _auth_path: Path) -> QuotaResult:
            return QuotaResult(note="Codex 配額讀取失敗")

    snap = CodexProvider(tmp_path, quota=TransientQuota()).snapshot()

    assert [(window.label, window.used_percent) for window in snap.quotas] == [
        ("5h", 42.0)
    ]
    assert snap.notes == ["Codex 配額讀取失敗"]
