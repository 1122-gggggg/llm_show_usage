from types import SimpleNamespace

from llm_usage_monitor.providers.antigravity import AntigravityProvider

USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t21%\t2026-08-20T17:52:30Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t74%\t2026-08-18T18:35:23Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t88%\t2026-08-21T16:16:42Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-08-18T21:52:02Z"
)


def test_antigravity_uses_read_only_usage_command_and_caches(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=USAGE, stderr="")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.shutil.which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=10)
    first = provider.snapshot()
    second = provider.snapshot()

    assert first.plan == "Google AI Pro"
    assert len(first.quotas) == 4
    assert second is first
    assert calls[0][0] == ["C:/agy.exe", "-p", "/usage"]
    assert len(calls) == 1


def test_antigravity_failed_usage_requests_login(monkeypatch) -> None:
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.shutil.which",
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
        result = responses.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(
        "llm_usage_monitor.providers.antigravity.shutil.which",
        lambda name: "C:/agy.exe" if name == "agy" else None,
    )
    provider = AntigravityProvider(runner=runner, ttl=0)
    first = provider.snapshot()
    second = provider.snapshot()
    assert len(first.quotas) == 4
    assert len(second.quotas) == 4
    assert second.notes == ["Antigravity 配額讀取失敗"]
