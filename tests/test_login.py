import base64
import json
from pathlib import Path
from types import SimpleNamespace

from llm_usage_monitor.__main__ import main, parse_args
from llm_usage_monitor.login import (
    SourceSpec,
    default_sources,
    ensure_sessions,
    login,
    missing_sources,
    parse_selection,
)
from llm_usage_monitor.providers import build_providers
from llm_usage_monitor.quota import parse_copilot


def _spec(
    tmp_path: Path,
    *,
    key: str,
    display: str,
    connected: bool,
    json_key: str | None = None,
) -> SourceSpec:
    cli = tmp_path / f"{key}.exe"
    cli.write_text("", encoding="utf-8")
    session = tmp_path / f"{key}.json"
    if connected:
        if key == "codex":
            payload = {"tokens": {"access_token": "opaque-token"}}
        elif json_key:
            payload = {json_key: {"access": "token"}}
        else:
            payload = {"token": "present"}
        session.write_text(json.dumps(payload), encoding="utf-8")
    return SourceSpec(
        key=key,
        display=display,
        binaries=(str(cli),),
        login_args=("login",),
        session_paths=(session,),
        json_key=json_key,
    )


def test_default_refresh_is_ten_seconds_and_has_no_kimi() -> None:
    args = parse_args([])
    assert args.interval == 10
    assert "antigravity" in args.providers
    assert "kimi" not in args.providers


def test_registered_sources_include_antigravity_and_exclude_kimi() -> None:
    source_keys = {source.key for source in default_sources()}
    provider_names = {provider.name for provider in build_providers()}
    assert "antigravity" in source_keys
    assert "Antigravity" in provider_names
    assert "kimi" not in source_keys
    assert "Kimi" not in provider_names


def test_filters_providers_by_stable_key(monkeypatch) -> None:
    captured: list[str] = []

    def fake_run_once(providers, interval=10.0) -> None:
        captured.extend(p.key for p in providers)

    monkeypatch.setattr("llm_usage_monitor.__main__.run_once", fake_run_once)
    assert main(["--once", "--providers", "claude,copilot"]) == 0
    assert captured == ["claude", "copilot"]
    names = {provider.key: provider.name for provider in build_providers()}
    assert names == {
        "claude": "Claude",
        "codex": "Codex",
        "grok": "Grok",
        "opencode": "OpenCode",
        "copilot": "Copilot",
        "antigravity": "Antigravity",
    }


def test_provider_specific_json_key_does_not_false_positive(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"opencode":{"key":"x"}}', encoding="utf-8")
    copilot = SourceSpec(
        key="copilot",
        display="Copilot",
        binaries=("gh",),
        login_args=("auth", "login"),
        session_paths=(auth,),
        json_key="github-copilot",
    )
    assert copilot.has_session() is False


def test_missing_sources_omits_connected_sources(tmp_path: Path) -> None:
    connected = _spec(tmp_path, key="codex", display="Codex", connected=True)
    missing = _spec(tmp_path, key="grok", display="Grok", connected=False)
    assert missing_sources({"codex", "grok"}, [connected, missing]) == [missing]


def test_copilot_refresh_token_alone_is_not_connected(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"github-copilot":{"refresh":"refresh-only"}}',
        encoding="utf-8",
    )
    copilot = SourceSpec(
        key="copilot",
        display="Copilot",
        binaries=("gh",),
        login_args=("auth", "login"),
        session_paths=(auth,),
        json_key="github-copilot",
    )
    assert copilot.has_session() is False


def test_expired_codex_jwt_is_not_connected(tmp_path: Path) -> None:
    payload = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": f"x.{payload}.x"}}),
        encoding="utf-8",
    )
    codex = SourceSpec(
        key="codex",
        display="Codex",
        binaries=("codex",),
        login_args=("login",),
        session_paths=(auth,),
    )
    assert codex.has_session() is False


def test_parse_selection_supports_multiple_and_all() -> None:
    assert parse_selection("1,3", 3) == {0, 2}
    assert parse_selection("a", 3) == {0, 1, 2}
    assert parse_selection("", 3) == set()
    assert parse_selection("9,nope", 3) == set()


def test_startup_menu_auto_keeps_connected_and_logs_selected_missing(tmp_path: Path) -> None:
    connected = _spec(tmp_path, key="codex", display="Codex", connected=True)
    missing = _spec(tmp_path, key="antigravity", display="Antigravity", connected=False)
    printed: list[str] = []
    logged: list[str] = []

    def login_fn(spec: SourceSpec):
        logged.append(spec.key)
        return True, f"{spec.display} 登入完成"

    notes = ensure_sessions(
        {"codex", "antigravity"},
        select=lambda _prompt: "1",
        printer=printed.append,
        sources=[connected, missing],
        login_fn=login_fn,
    )

    output = "\n".join(printed)
    assert "✓ Codex" in output
    assert "○ Antigravity" in output
    assert logged == ["antigravity"]
    assert any("登入完成" in note for note in notes)


def test_login_without_cli_returns_install_hint() -> None:
    spec = SourceSpec(
        key="x",
        display="X",
        binaries=("definitely-not-installed-xyz",),
        login_args=("login",),
        session_paths=(),
        install_hint="請安裝 X",
    )
    ok, msg = login(spec)
    assert ok is False
    assert "請安裝 X" in msg


def test_login_runs_official_args(tmp_path: Path) -> None:
    fake = tmp_path / "codex.exe"
    fake.write_text("", encoding="utf-8")
    spec = SourceSpec(
        key="codex",
        display="Codex",
        binaries=(str(fake),),
        login_args=("login",),
        session_paths=(),
    )
    seen: list[list[str]] = []

    def runner(cmd, check=False):
        seen.append(cmd)
        return SimpleNamespace(returncode=0)

    ok, msg = login(spec, runner=runner)
    assert ok is True
    assert seen[0][1:] == ["login"]
    assert "登入完成" in msg


def test_antigravity_agy_login_uses_read_only_usage_flow(tmp_path: Path) -> None:
    fake = tmp_path / "agy.exe"
    fake.write_text("", encoding="utf-8")
    spec = SourceSpec(
        key="antigravity",
        display="Antigravity",
        binaries=(str(fake),),
        login_args=("auth", "login"),
        session_paths=(),
        login_cwd=tmp_path,
    )
    seen: list[tuple[list[str], dict]] = []

    def runner(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    ok, _msg = login(spec, runner=runner)
    assert ok is True
    assert seen[0][0] == [str(fake), "-p", "/usage"]
    assert seen[0][1]["cwd"] == tmp_path


def test_parse_copilot_remaining() -> None:
    windows, plan = parse_copilot(
        {
            "copilot_plan": "pro",
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 300,
                    "remaining": 120,
                    "percent_remaining": 40,
                }
            },
            "quota_reset_date": "2026-09-01",
        }
    )
    assert plan == "pro"
    assert windows[0].used_percent == 60
    assert windows[0].detail == "120/300"
    assert windows[0].resets_at is not None
    assert windows[0].resets_at.tzinfo is not None
