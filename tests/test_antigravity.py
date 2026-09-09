import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_usage_monitor.providers.antigravity import AntigravityProvider

USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t21%\t2099-08-20T17:52:30Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t74%\t2099-08-18T18:35:23Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t88%\t2099-08-21T16:16:42Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2099-08-18T21:52:02Z"
)
VERSION = "agy version 1.1.11"


def test_antigravity_uses_read_only_usage_command_and_caches(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        return SimpleNamespace(returncode=0, stdout=USAGE, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=10)
    first = provider.snapshot()
    first.quotas[0].used_percent = 99
    second = provider.snapshot()

    assert first.plan is None
    assert len(first.quotas) == 4
    assert second.quotas[0].used_percent != 99
    assert second is not first
    assert second.quotas is not first.quotas
    assert calls[0][0] == ["C:/agy.exe", "--version"]
    assert calls[1][0] == ["C:/agy.exe", "-p", "/usage"]
    assert calls[1][1]["encoding"] == "utf-8"
    assert calls[1][1]["errors"] == "replace"
    assert calls[1][1]["stdin"] == subprocess.DEVNULL
    assert len(calls) == 2


def test_antigravity_failed_usage_requests_login(monkeypatch) -> None:
    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    snap = AntigravityProvider(runner=runner).snapshot()
    assert snap.quotas == []
    assert any("login" in note for note in snap.notes)


def test_antigravity_keeps_last_good_quota_on_transient_failure(monkeypatch) -> None:
    responses = [
        SimpleNamespace(returncode=0, stdout=USAGE, stderr=""),
        OSError("temporary failure"),
    ]

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        result = responses.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=0)
    first = provider.snapshot()
    second = provider.snapshot()
    assert len(first.quotas) == 4
    assert len(second.quotas) == 4
    assert second.notes == ["Antigravity 配額讀取失敗"]


def test_antigravity_checks_safe_version_only_once(monkeypatch) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        return SimpleNamespace(returncode=0, stdout=USAGE, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=0)

    provider.snapshot()
    provider.snapshot()

    assert sum(command[-1] == "--version" for command in calls) == 1
    assert sum(command[-1] == "/usage" for command in calls) == 2


def test_antigravity_rechecks_version_when_binary_is_replaced(
    monkeypatch, tmp_path: Path
) -> None:
    binary = tmp_path / "agy.exe"
    binary.write_text("safe", encoding="utf-8")
    versions = iter(["agy version 1.1.11", "agy version 1.1.10"])
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=next(versions), stderr="")
        return SimpleNamespace(returncode=0, stdout=USAGE, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: str(binary) if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=0)
    assert provider.snapshot().quotas

    replacement = tmp_path / "replacement.exe"
    replacement.write_text("old", encoding="utf-8")
    replacement.replace(binary)
    blocked = provider.snapshot()

    assert blocked.quotas == []
    assert sum(command[-1] == "/usage" for command in calls) == 1


def test_antigravity_concurrent_cache_miss_is_single_flight(monkeypatch) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        return SimpleNamespace(returncode=0, stdout=USAGE, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _index: provider.snapshot(), range(8)))

    assert all(snapshot.quotas for snapshot in snapshots)
    assert sum(command[-1] == "--version" for command in calls) == 1
    assert sum(command[-1] == "/usage" for command in calls) == 1


def test_antigravity_keeps_last_good_quota_on_non_auth_exit(monkeypatch) -> None:
    responses = [
        SimpleNamespace(returncode=0, stdout=USAGE, stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="temporary backend error"),
    ]

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        return responses.pop(0)

    provider = AntigravityProvider(runner=runner, ttl=0)

    assert len(provider.snapshot().quotas) == 4
    stale = provider.snapshot()

    assert len(stale.quotas) == 4
    assert stale.notes == ["Antigravity 配額讀取失敗"]


@pytest.mark.parametrize("version", ["agy version 1.1.10", "unexpected output"])
def test_antigravity_refuses_old_or_unparseable_agy_without_running_usage(
    monkeypatch, version: str
) -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=version, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    snap = AntigravityProvider(runner=runner, ttl=0).snapshot()

    assert snap.quotas == []
    assert calls == [["C:/agy.exe", "--version"]]
    assert any("版本" in note and "/usage" in note for note in snap.notes)


def test_antigravity_stale_quota_expires(monkeypatch) -> None:
    responses = [
        SimpleNamespace(returncode=0, stdout=USAGE, stderr=""),
        OSError("temporary failure"),
        OSError("still failing"),
    ]
    now = [0.0]

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        result = responses.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.time.monotonic", lambda: now[0]
    )
    provider = AntigravityProvider(runner=runner, ttl=10, max_stale=15)

    assert len(provider.snapshot().quotas) == 4
    now[0] = 11
    assert len(provider.snapshot().quotas) == 4
    now[0] = 16
    expired = provider.snapshot()

    assert expired.quotas == []
    assert expired.notes == ["Antigravity 配額讀取失敗"]


def test_antigravity_does_not_reuse_windows_after_their_reset(monkeypatch) -> None:
    usage = "Gemini Models\tWeekly Limit Remaining\t21%\t2000-01-01T00:00:00Z"
    responses = [
        SimpleNamespace(returncode=0, stdout=usage, stderr=""),
        OSError("temporary failure"),
    ]

    def runner(command, **_kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=VERSION, stderr="")
        result = responses.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.trusted_which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=0)

    assert provider.snapshot().quotas
    stale = provider.snapshot()

    assert stale.quotas == []
    assert stale.notes == ["Antigravity 配額讀取失敗"]
