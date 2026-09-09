from types import SimpleNamespace

import llm_usage_monitor.providers.copilot as copilot_module
from llm_usage_monitor.providers.copilot import CopilotProvider
from llm_usage_monitor.quota import LOGIN_NOTE, QuotaResult


class FakeQuota:
    def __init__(self, results: list[QuotaResult]) -> None:
        self._results = iter(results)
        self.tokens: list[str | None] = []

    def copilot(self, token: str | None) -> QuotaResult:
        self.tokens.append(token)
        return next(self._results)


def test_copilot_token_prefers_gh_token(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    assert copilot_module._copilot_token() == "gh-token"


def test_copilot_does_not_send_enterprise_host_env_token_to_github_com(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GH_HOST", "git.corp.example")
    monkeypatch.setenv("GH_TOKEN", "enterprise-token")
    monkeypatch.setenv("GITHUB_TOKEN", "second-enterprise-token")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(copilot_module, "trusted_which", lambda _name: None)

    assert copilot_module._copilot_token() is None


def test_copilot_re_resolves_token_after_login_note(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "expired-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    quota = FakeQuota(
        [
            QuotaResult(note=LOGIN_NOTE),
            QuotaResult(plan="pro"),
            QuotaResult(plan="pro"),
        ]
    )
    now = [0.0]
    provider = CopilotProvider(quota=quota, clock=lambda: now[0])

    assert provider.snapshot().notes == [LOGIN_NOTE]
    monkeypatch.setenv("GH_TOKEN", "replacement-token")
    now[0] = 10.0
    assert provider.snapshot().plan == "pro"
    monkeypatch.setenv("GH_TOKEN", "unused-token")
    assert provider.snapshot().plan == "pro"

    assert quota.tokens == ["expired-token", "replacement-token", "replacement-token"]


def test_copilot_throttles_missing_token_resolution(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(copilot_module, "trusted_which", lambda _name: "/fake/gh")
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(copilot_module.subprocess, "run", fake_run)
    quota = FakeQuota([QuotaResult(note=LOGIN_NOTE) for _ in range(20)])
    provider = CopilotProvider(quota=quota, clock=lambda: 0.0)

    for _ in range(20):
        provider.snapshot()

    assert calls == 1


def test_copilot_keeps_successful_gh_token_between_snapshots(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(copilot_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(copilot_module, "trusted_which", lambda name: f"/fake/{name}")
    subprocess_calls: list[list[str]] = []
    subprocess_kwargs: list[dict] = []

    def fake_run(command, **kwargs):
        subprocess_calls.append(command)
        subprocess_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="cli-token\n")

    monkeypatch.setattr(copilot_module.subprocess, "run", fake_run)
    quota = FakeQuota([QuotaResult(plan="pro"), QuotaResult(plan="pro")])
    provider = CopilotProvider(quota=quota)

    provider.snapshot()
    provider.snapshot()

    assert quota.tokens == ["cli-token", "cli-token"]
    assert subprocess_calls == [
        ["/fake/gh", "auth", "token", "--hostname", "github.com"]
    ]
    assert subprocess_kwargs[0]["cwd"] == tmp_path
    assert subprocess_kwargs[0]["stdin"] is copilot_module.subprocess.DEVNULL


def test_copilot_invalid_local_auth_falls_back_to_gh(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    auth = tmp_path / "opencode" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(copilot_module, "trusted_which", lambda _name: "/fake/gh")
    monkeypatch.setattr(
        copilot_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=" fallback-token \n"
        ),
    )

    assert copilot_module._copilot_token() == "fallback-token"


def test_copilot_re_resolves_after_account_has_no_subscription(monkeypatch) -> None:
    tokens = iter(("old-account-token", "new-account-token"))
    monkeypatch.setattr(copilot_module, "_copilot_token", lambda: next(tokens))
    quota = FakeQuota(
        [
            QuotaResult(note="此帳號沒有 Copilot 訂閱"),
            QuotaResult(plan="pro"),
        ]
    )
    provider = CopilotProvider(quota=quota, clock=lambda: 0.0)

    assert provider.snapshot().notes == ["此帳號沒有 Copilot 訂閱"]
    assert provider.snapshot().plan == "pro"
    assert quota.tokens == ["old-account-token", "new-account-token"]


def test_copilot_strips_host_tokens_from_gh_subprocess(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GH_HOST", "git.corp.example")
    monkeypatch.setenv("GH_TOKEN", "enterprise-token")
    monkeypatch.setenv("GITHUB_TOKEN", "second-enterprise-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "third-enterprise-token")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "fourth-enterprise-token")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(copilot_module, "trusted_which", lambda _name: "/fake/gh")
    seen_env: dict[str, str] = {}

    def fake_run(*_args, **kwargs):
        seen_env.update(kwargs["env"])
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(copilot_module.subprocess, "run", fake_run)

    assert copilot_module._copilot_token() is None
    assert not {
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }.intersection(seen_env)
