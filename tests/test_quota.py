import http.client
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import llm_usage_monitor.quota as quota_module
from llm_usage_monitor.quota import (
    QuotaClient,
    QuotaResult,
    _SafeRedirectHandler,
    http_get,
    parse_antigravity_cli,
    parse_claude,
    parse_codex,
    parse_copilot,
    parse_grok,
    parse_opencode,
)


def test_quota_result_public_schema_remains_stable() -> None:
    assert asdict(QuotaResult(note="note")) == {
        "windows": [],
        "plan": None,
        "note": "note",
    }


def _copilot_body(used_percent: float) -> dict:
    return {
        "copilot_plan": "pro",
        "quota_snapshots": {
            "premium_interactions": {
                "percent_remaining": 100.0 - used_percent,
            }
        },
    }


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
                    "reset_at": int(datetime.now(UTC).timestamp()) + 3600,
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
    assert plan is None
    assert labels["Gemini 週"].used_percent == 79
    assert labels["Gemini 5h"].used_percent == 26
    assert labels["Claude/GPT 週"].used_percent == 12
    assert labels["Claude/GPT 5h"].used_percent == 0
    assert labels["Gemini 週"].resets_at is not None


def test_parse_antigravity_cli_accepts_empty_reset_column() -> None:
    windows, plan = parse_antigravity_cli(
        "Gemini Models\tWeekly Limit Remaining\t21%\t\n"
    )

    assert plan is None
    assert len(windows) == 1
    assert windows[0].used_percent == 79
    assert windows[0].resets_at is None


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


def test_quota_client_keeps_last_good_value_on_transient_failure(
    tmp_path: Path,
) -> None:
    responses = [
        (
            200,
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 50,
                        "limit_window_seconds": 604800,
                        "reset_at": int(datetime.now(UTC).timestamp()) + 3600,
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


def test_redirect_strips_sensitive_headers_only_when_origin_changes() -> None:
    handler = _SafeRedirectHandler()
    request = urllib.request.Request(
        "https://api.example.test/start",
        headers={
            "Authorization": "Bearer DUMMY_SECRET",
            "chatgpt-account-id": "account-a",
            "Accept": "application/json",
        },
    )

    same_origin = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://api.example.test/next",
    )
    assert same_origin is not None
    assert same_origin.get_header("Authorization") == "Bearer DUMMY_SECRET"
    assert same_origin.get_header("Chatgpt-account-id") == "account-a"

    cross_origin = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://other.example.test/next",
    )
    assert cross_origin is not None
    assert cross_origin.get_header("Authorization") is None
    assert cross_origin.get_header("Chatgpt-account-id") is None
    assert cross_origin.get_header("Accept") == "application/json"

    downgrade = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "http://api.example.test/next",
    )
    assert downgrade is not None
    assert downgrade.get_header("Authorization") is None


def test_cache_distinguishes_copilot_tokens() -> None:
    calls: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        token = headers["Authorization"].split()[-1]
        calls.append(token)
        return 200, _copilot_body(10.0 if token == "token-a" else 90.0)

    client = QuotaClient(get=fake_get, ttl=60)
    first = client.copilot("token-a")
    second = client.copilot("token-b")
    again = client.copilot("token-a")

    assert first.windows[0].used_percent == 10.0
    assert second.windows[0].used_percent == 90.0
    assert again.windows[0].used_percent == 10.0
    assert calls == ["token-a", "token-b"]


def test_cache_identity_uses_credential_content_not_windows_metadata(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"token-a","account_id":"acct-a"}}',
        encoding="utf-8",
    )
    original = auth.stat()
    calls: list[str] = []

    def fake_get(_url: str, headers: dict, timeout: float = 3.0):
        calls.append(headers["Authorization"])
        used = 10.0 if headers["Authorization"].endswith("token-a") else 90.0
        return 200, {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 300,
                }
            }
        }

    client = QuotaClient(get=fake_get, ttl=60)
    assert client.codex(auth).windows[0].used_percent == 10.0
    auth.write_text(
        '{"tokens":{"access_token":"token-b","account_id":"acct-b"}}',
        encoding="utf-8",
    )
    os.utime(auth, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert client.codex(auth).windows[0].used_percent == 90.0
    assert calls == ["Bearer token-a", "Bearer token-b"]


def test_credential_rotation_during_fetch_discards_old_identity_result(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"token-a","account_id":"acct-a"}}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_get(_url: str, headers: dict, timeout: float = 3.0):
        token = headers["Authorization"].split()[-1]
        calls.append(token)
        if token == "token-a":
            auth.write_text(
                '{"tokens":{"access_token":"token-b","account_id":"acct-b"}}',
                encoding="utf-8",
            )
        return 200, {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10.0 if token == "token-a" else 90.0,
                    "limit_window_seconds": 300,
                }
            }
        }

    result = QuotaClient(get=fake_get, ttl=60).codex(auth)

    assert result.windows[0].used_percent == 90.0
    assert calls == ["token-a", "token-b"]


def test_quota_cache_and_locks_are_bounded_across_token_rotation() -> None:
    client = QuotaClient(
        get=lambda *_args, **_kwargs: (200, _copilot_body(25.0)),
        ttl=60,
    )

    for index in range(100):
        assert client.copilot(f"token-{index}").windows

    assert len(client._cache) == quota_module._MAX_CACHE_ENTRIES
    assert len(client._key_locks) == 1


def test_cache_distinguishes_credential_paths(tmp_path: Path) -> None:
    first_auth = tmp_path / "first.json"
    second_auth = tmp_path / "second.json"
    first_auth.write_text(
        '{"tokens":{"access_token":"token-a","account_id":"account-a"}}',
        encoding="utf-8",
    )
    second_auth.write_text(
        '{"tokens":{"access_token":"token-b","account_id":"account-b"}}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        token = headers["Authorization"].split()[-1]
        calls.append(token)
        return 200, {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10.0 if token == "token-a" else 90.0,
                    "limit_window_seconds": 604800,
                    "reset_at": 1787196665,
                }
            },
        }

    client = QuotaClient(get=fake_get, ttl=60)
    first = client.codex(first_auth)
    second = client.codex(second_auth)

    assert first.windows[0].used_percent == 10.0
    assert second.windows[0].used_percent == 90.0
    assert calls == ["token-a", "token-b"]


def test_same_key_concurrent_miss_is_single_flight() -> None:
    calls = 0
    calls_guard = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        nonlocal calls
        with calls_guard:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return 200, _copilot_body(25.0)

    client = QuotaClient(get=fake_get, ttl=60)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(client.copilot, "token-a") for _ in range(8)]
        assert started.wait(timeout=1)
        time.sleep(0.05)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert calls == 1
    assert all(result.windows[0].used_percent == 25.0 for result in results)


def test_ttl_starts_when_fetch_finishes(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        quota_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    calls = 0

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        nonlocal calls
        calls += 1
        clock[0] += 20.0
        return 200, _copilot_body(25.0)

    client = QuotaClient(get=fake_get, ttl=10)
    client.copilot("token-a")
    client.copilot("token-a")

    assert calls == 1


def test_copilot_permanent_failure_does_not_reuse_stale_quota() -> None:
    responses = [
        (200, _copilot_body(25.0)),
        (403, {"message": "no subscription"}),
    ]
    client = QuotaClient(get=lambda *_a, **_k: responses.pop(0), ttl=0)

    first = client.copilot("token-a")
    second = client.copilot("token-a")

    assert first.windows
    assert second.windows == []
    assert second.note == "此帳號沒有 Copilot 訂閱"


def test_transient_fallback_expires_after_max_stale(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        quota_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    responses = [
        (200, _copilot_body(25.0)),
        (503, {"message": "down"}),
        (503, {"message": "still down"}),
    ]
    client = QuotaClient(
        get=lambda *_a, **_k: responses.pop(0),
        ttl=10,
        max_stale=15,
    )

    assert client.copilot("token-a").windows
    clock[0] = 11.0
    assert client.copilot("token-a").windows
    clock[0] = 16.0
    expired = client.copilot("token-a")

    assert expired.windows == []
    assert expired.note == "Copilot 配額讀取失敗"


def test_cached_results_are_defensive_copies() -> None:
    calls = 0

    def fake_get(url: str, headers: dict, timeout: float = 3.0):
        nonlocal calls
        calls += 1
        return 200, _copilot_body(25.0)

    client = QuotaClient(get=fake_get, ttl=60)
    first = client.copilot("token-a")
    first.windows[0].used_percent = 99.0
    first.windows.clear()
    second = client.copilot("token-a")

    assert calls == 1
    assert second.windows[0].used_percent == 25.0


def test_empty_and_malformed_parser_payloads_do_not_create_quota() -> None:
    assert parse_claude({"five_hour": {"utilization": "bad"}}) == ([], None)
    assert parse_grok({}) == ([], None)
    assert parse_grok({"config": {"productUsage": ["bad", {}]}}) == ([], None)
    assert parse_copilot({}) == ([], None)
    assert parse_copilot({"quota_snapshots": []}) == ([], None)
    assert parse_opencode({"usage": {"rolling": {"percent": float("nan")}}}) == (
        [],
        None,
    )
    assert parse_codex(
        {"rate_limit": {"primary_window": {"limit_window_seconds": "bad"}}}
    ) == ([], None)

    windows, plan = parse_codex(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 50,
                    "limit_window_seconds": "not-a-number",
                    "reset_at": 1787196665,
                }
            },
        }
    )
    assert plan == "pro"
    assert windows[0].label == "配額"

    assert parse_codex({"plan_type": ["pro"]}) == ([], None)
    assert parse_grok({"subscriptionTier": {"name": "pro"}}) == ([], None)
    assert parse_copilot({"copilot_plan": ["pro"]}) == ([], None)
    assert parse_copilot(
        {"quota_snapshots": {"premium_interactions": {"percent_remaining": True}}}
    ) == ([], None)
    assert parse_claude({"five_hour": {"utilization": -1}}) == ([], None)
    assert parse_codex({"rate_limit": {"primary_window": {"used_percent": 101}}}) == (
        [],
        None,
    )
    assert parse_grok({"config": {"creditUsagePercent": 101}}) == ([], None)
    assert parse_opencode({"usage": {"rolling": {"percent": -1}}}) == ([], None)
    assert parse_copilot(
        {"quota_snapshots": {"premium_interactions": {"percent_remaining": 101}}}
    ) == ([], None)
    assert parse_antigravity_cli("Gemini Models\tWeekly Limit Remaining\t101%\t") == (
        [],
        None,
    )


def test_malformed_auth_json_and_fields_fail_closed(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"n":' + "9" * 5000 + "}", encoding="utf-8")
    calls = 0

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return 200, {}

    client = QuotaClient(get=fake_get, ttl=0)
    assert client.codex(auth).note == quota_module.LOGIN_NOTE

    auth.write_text(
        '{"tokens":{"access_token":"token","account_id":["bad"]}}',
        encoding="utf-8",
    )
    assert client.codex(auth).note == quota_module.LOGIN_NOTE
    assert calls == 0


def test_transient_fallback_drops_windows_that_already_reset(tmp_path: Path) -> None:
    now = int(datetime.now(UTC).timestamp())
    responses = [
        (
            200,
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 99,
                        "limit_window_seconds": 300,
                        "reset_at": now - 1,
                    },
                    "secondary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 604800,
                        "reset_at": now + 3600,
                    },
                }
            },
        ),
        (503, {}),
    ]
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"token","account_id":"account"}}',
        encoding="utf-8",
    )
    client = QuotaClient(get=lambda *_args, **_kwargs: responses.pop(0), ttl=0)

    assert len(client.codex(auth).windows) == 2
    stale = client.codex(auth)

    assert [(window.label, window.used_percent) for window in stale.windows] == [
        ("週", 10.0)
    ]


def test_cached_transient_fallback_rechecks_reset_time(
    tmp_path: Path, monkeypatch
) -> None:
    class FrozenDatetime(datetime):
        current = datetime(2026, 8, 24, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.current.timestamp(), tz)

    clock = [0.0]
    monkeypatch.setattr(quota_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        quota_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    reset = int((FrozenDatetime.current + timedelta(seconds=1)).timestamp())
    responses = [
        (
            200,
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 25,
                        "limit_window_seconds": 300,
                        "reset_at": reset,
                    }
                }
            },
        ),
        (503, {}),
        (503, {}),
    ]
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"token","account_id":"account"}}',
        encoding="utf-8",
    )
    client = QuotaClient(get=lambda *_args, **_kwargs: responses.pop(0), ttl=10)

    assert client.codex(auth).windows
    clock[0] = 11
    assert client.codex(auth).windows
    FrozenDatetime.current += timedelta(seconds=2)
    clock[0] = 12

    assert client.codex(auth).windows == []


def test_http_error_body_timeout_is_closed_and_normalized(monkeypatch) -> None:
    class BrokenBody:
        def __init__(self) -> None:
            self.closed = False

        def read(self, _size: int = -1) -> bytes:
            raise TimeoutError("body stalled")

        def close(self) -> None:
            self.closed = True

    body = BrokenBody()
    error = urllib.error.HTTPError(
        "https://example.invalid",
        500,
        "boom",
        {},
        body,
    )

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(quota_module._HTTP_OPENER, "open", raise_error)
    status, response = http_get("https://example.invalid", {})

    assert status == 500
    assert response == "boom"
    assert body.closed


def test_http_get_rejects_oversized_response(monkeypatch) -> None:
    class OversizedResponse:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def read(self, size: int) -> bytes:
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.closed = True

    oversized = OversizedResponse()
    monkeypatch.setattr(
        quota_module._HTTP_OPENER,
        "open",
        lambda *_args, **_kwargs: oversized,
    )

    status, response = http_get("https://example.invalid", {})

    assert status == 0
    assert response == "ValueError"
    assert oversized.closed


def test_http_get_does_not_echo_secret_from_invalid_request(monkeypatch) -> None:
    secret = "DUMMY_SECRET"

    def raise_invalid(*_args, **_kwargs):
        raise http.client.InvalidURL(f"invalid Authorization Bearer {secret}")

    monkeypatch.setattr(quota_module._HTTP_OPENER, "open", raise_invalid)
    status, response = http_get(
        "https://example.invalid",
        {"Authorization": f"Bearer {secret}"},
    )

    assert status == 0
    assert response == "InvalidURL"
    assert secret not in response
