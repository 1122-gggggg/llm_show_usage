import base64
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm_usage_monitor.__main__ as main_module
import llm_usage_monitor.login as login_module
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


def _fake_cli(tmp_path: Path, name: str) -> Path:
    cli = tmp_path / name
    cli.write_text("", encoding="utf-8")
    if os.name != "nt":
        cli.chmod(0o755)
    return cli


def _spec(
    tmp_path: Path,
    *,
    key: str,
    display: str,
    connected: bool,
    json_key: str | None = None,
) -> SourceSpec:
    cli = _fake_cli(tmp_path, f"{key}.exe")
    session = tmp_path / f"{key}.json"
    if connected:
        if key == "claude":
            payload = {"claudeAiOauth": {"accessToken": "token"}}
        elif key == "codex":
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


def test_opencode_profile_paths_expand_together(tmp_path: Path) -> None:
    args = parse_args(
        [
            "--opencode-db",
            str(tmp_path / "profile" / "opencode.db"),
            "--opencode-auth",
            str(tmp_path / "profile" / "auth.json"),
        ]
    )

    assert args.opencode_db == tmp_path / "profile" / "opencode.db"
    assert args.opencode_auth == tmp_path / "profile" / "auth.json"


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "0.01", "1e-20", "5e-324", "86401", "1e308", "nan", "inf", "-inf"],
)
def test_refresh_interval_must_be_positive_and_finite(value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--interval", value])

    assert exc_info.value.code == 2


def test_refresh_interval_accepts_tenth_of_a_second() -> None:
    assert parse_args(["--interval", "0.1"]).interval == 0.1


@pytest.mark.parametrize("value", ["", ",,", "codxe", "claude,codxe"])
def test_provider_list_rejects_empty_or_unknown_names(value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--providers", value])

    assert exc_info.value.code == 2


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


def test_non_tty_output_defaults_to_one_snapshot(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("llm_usage_monitor.__main__._live_capable", lambda: False)
    monkeypatch.setattr(
        "llm_usage_monitor.__main__.run_once",
        lambda _providers, _interval: calls.append("once"),
    )
    monkeypatch.setattr(
        "llm_usage_monitor.__main__.run_live",
        lambda _providers, _interval: calls.append("live"),
    )

    assert main(["--providers", "claude"]) == 0
    assert calls == ["once"]


def test_force_color_cannot_turn_redirected_stdout_into_live_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module.sys,
        "stdout",
        SimpleNamespace(isatty=lambda: False),
    )
    monkeypatch.setattr(
        main_module,
        "Console",
        lambda: SimpleNamespace(is_terminal=True, is_dumb_terminal=False),
    )

    assert main_module._live_capable() is False


def test_unexpandable_user_path_is_an_argparse_error(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module.Path,
        "expanduser",
        lambda _self: (_ for _ in ()).throw(RuntimeError("missing home")),
    )

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--claude-dir", "~"])

    assert exc_info.value.code == 2


def test_yes_implies_login_in_non_interactive_mode(monkeypatch) -> None:
    selected: list[bool] = []
    monkeypatch.setattr("llm_usage_monitor.__main__._live_capable", lambda: False)
    monkeypatch.setattr(
        "llm_usage_monitor.__main__.ensure_sessions",
        lambda _wanted, **kwargs: selected.append(kwargs["select_all"]),
    )
    monkeypatch.setattr("llm_usage_monitor.__main__.run_once", lambda *_args: None)

    assert main(["--yes", "--providers", "claude"]) == 0
    assert selected == [True]


def test_login_menu_uses_explicit_provider_roots(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict[str, tuple[Path, ...]]] = []
    claude_dir = tmp_path / "claude-profile" / "projects"
    codex_dir = tmp_path / "codex-profile" / "sessions"
    grok_dir = tmp_path / "grok-profile"

    def fake_ensure(_wanted, **kwargs) -> None:
        captured.append(
            {source.key: source.session_paths for source in kwargs["sources"]}
        )

    monkeypatch.setattr("llm_usage_monitor.__main__._live_capable", lambda: False)
    monkeypatch.setattr("llm_usage_monitor.__main__.ensure_sessions", fake_ensure)
    monkeypatch.setattr("llm_usage_monitor.__main__.run_once", lambda *_args: None)

    assert (
        main(
            [
                "--yes",
                "--providers",
                "claude,codex,grok",
                "--claude-dir",
                str(claude_dir),
                "--codex-dir",
                str(codex_dir),
                "--grok-dir",
                str(grok_dir),
            ]
        )
        == 0
    )
    assert len(captured) == 1
    assert captured[0]["claude"] == (claude_dir.parent / ".credentials.json",)
    assert captured[0]["codex"] == (codex_dir.parent / "auth.json",)
    assert captured[0]["grok"] == (grok_dir / "auth.json",)


def test_keyboard_interrupt_returns_standard_exit_code(monkeypatch) -> None:
    monkeypatch.setattr("llm_usage_monitor.__main__._live_capable", lambda: False)
    monkeypatch.setattr(
        "llm_usage_monitor.__main__.run_once",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert main(["--once", "--providers", "claude"]) == 130


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


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"claudeAiOauth": {}},
        {"claudeAiOauth": {"accessToken": "   "}},
        {"claudeAiOauth": {"accessToken": 123}},
    ],
)
def test_claude_requires_nonempty_access_token(tmp_path: Path, payload: object) -> None:
    auth = tmp_path / "claude.json"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    claude = SourceSpec(
        key="claude",
        display="Claude",
        binaries=("claude",),
        login_args=("auth", "login"),
        session_paths=(auth,),
    )

    assert claude.has_session() is False


def test_claude_accepts_trimmed_access_token(tmp_path: Path) -> None:
    auth = tmp_path / "claude.json"
    auth.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": " token "}}),
        encoding="utf-8",
    )
    claude = SourceSpec(
        key="claude",
        display="Claude",
        binaries=("claude",),
        login_args=("auth", "login"),
        session_paths=(auth,),
    )

    assert claude.has_session() is True


def _jwt(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"x.{encoded}.x"


@pytest.mark.parametrize(
    "token",
    [
        "x.%.x",
        _jwt(b"not-json"),
        _jwt(b'[{"exp":4102444800}]'),
        _jwt(b"{}"),
        _jwt(b'{"exp":"not-a-number"}'),
    ],
)
def test_malformed_codex_jwt_is_not_connected(tmp_path: Path, token: str) -> None:
    auth = tmp_path / "codex.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": token}}),
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


def test_opaque_codex_token_remains_supported(tmp_path: Path) -> None:
    codex = _spec(tmp_path, key="codex", display="Codex", connected=True)

    assert codex.has_session() is True


def test_valid_codex_jwt_is_connected(tmp_path: Path) -> None:
    auth = tmp_path / "codex.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": _jwt(b'{"exp":4102444800}')}}),
        encoding="utf-8",
    )
    codex = SourceSpec(
        key="codex",
        display="Codex",
        binaries=("codex",),
        login_args=("login",),
        session_paths=(auth,),
    )

    assert codex.has_session() is True


def test_whitespace_environment_token_is_not_connected(monkeypatch) -> None:
    spec = SourceSpec(
        key="x",
        display="X",
        binaries=(),
        login_args=(),
        session_paths=(),
        env_keys=("LLM_USAGE_TEST_TOKEN",),
    )
    monkeypatch.setenv("LLM_USAGE_TEST_TOKEN", "   ")
    assert spec.has_session() is False

    monkeypatch.setenv("LLM_USAGE_TEST_TOKEN", " token ")
    assert spec.has_session() is True


def test_copilot_enterprise_host_token_is_not_reported_as_github_com_session(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_HOST", "ghe.corp.example")
    monkeypatch.setenv("GH_TOKEN", "enterprise-token")
    spec = SourceSpec(
        key="copilot",
        display="Copilot",
        binaries=(),
        login_args=(),
        session_paths=(),
        env_keys=("GH_TOKEN", "GITHUB_TOKEN"),
    )

    assert spec.has_session() is False


def test_copilot_hosts_file_requires_github_com_token(tmp_path: Path) -> None:
    hosts = tmp_path / "hosts.yml"
    spec = SourceSpec(
        key="copilot",
        display="Copilot",
        binaries=(),
        login_args=(),
        session_paths=(hosts,),
        json_key="github-copilot",
    )
    hosts.write_text(
        "ghe.example:\n  user: enterprise\n  oauth_token: enterprise-token\n",
        encoding="utf-8",
    )

    assert spec.has_session() is False

    hosts.write_text(
        "ghe.example:\n  oauth_token: enterprise-token\n"
        "github.com:\n  user: public\n  oauth_token: public-token\n",
        encoding="utf-8",
    )

    assert spec.has_session() is True


@pytest.mark.parametrize("field", ["key", "access", "token"])
def test_whitespace_json_token_is_not_connected(tmp_path: Path, field: str) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"service": {field: "   "}}),
        encoding="utf-8",
    )
    spec = SourceSpec(
        key="x",
        display="X",
        binaries=(),
        login_args=(),
        session_paths=(auth,),
        json_key="service",
    )

    assert spec.has_session() is False


def test_json_token_is_trimmed(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"service": {"access": " token "}}),
        encoding="utf-8",
    )
    spec = SourceSpec(
        key="x",
        display="X",
        binaries=(),
        login_args=(),
        session_paths=(auth,),
        json_key="service",
    )

    assert spec.has_session() is True


def test_probe_uses_utf8_with_replacement(monkeypatch, tmp_path: Path) -> None:
    cli = _fake_cli(tmp_path, "agy.exe")
    seen: dict[str, object] = {}

    def run(_command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="quota")

    monkeypatch.setattr("llm_usage_monitor.login.subprocess.run", run)
    spec = SourceSpec(
        key="antigravity",
        display="Antigravity",
        binaries=(str(cli),),
        login_args=(),
        session_paths=(),
        probe_args=("models",),
    )

    assert spec.has_session() is True
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_xdg_data_home_controls_opencode_auth(monkeypatch, tmp_path: Path) -> None:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    sources = {source.key: source for source in default_sources()}
    expected = data_home / "opencode" / "auth.json"
    assert sources["opencode"].session_paths == (expected,)
    assert expected in sources["copilot"].session_paths


def test_gh_config_dir_takes_precedence(monkeypatch, tmp_path: Path) -> None:
    gh_config = tmp_path / "gh-config"
    xdg_config = tmp_path / "xdg-config"
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

    copilot = next(source for source in default_sources() if source.key == "copilot")
    assert gh_config / "hosts.yml" in copilot.session_paths
    assert xdg_config / "gh" / "hosts.yml" not in copilot.session_paths


def test_xdg_config_home_controls_gh_auth(monkeypatch, tmp_path: Path) -> None:
    xdg_config = tmp_path / "xdg-config"
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

    copilot = next(source for source in default_sources() if source.key == "copilot")
    assert xdg_config / "gh" / "hosts.yml" in copilot.session_paths


def test_claude_config_dir_controls_login_detection(
    monkeypatch, tmp_path: Path
) -> None:
    profile = tmp_path / "claude-profile"
    profile.mkdir()
    (profile / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))

    claude = next(source for source in default_sources() if source.key == "claude")
    assert claude.session_paths == (profile / ".credentials.json",)
    assert claude.has_session() is True


def test_codex_home_controls_login_detection(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "codex-profile"
    profile.mkdir()
    (profile / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "opaque-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(profile))

    codex = next(source for source in default_sources() if source.key == "codex")
    assert codex.session_paths == (profile / "auth.json",)
    assert codex.has_session() is True


def test_grok_home_controls_login_detection(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "grok-profile"
    profile.mkdir()
    (profile / "auth.json").write_text(
        '{"account":{"key":"present"}}', encoding="utf-8"
    )
    monkeypatch.setenv("GROK_HOME", str(profile))

    grok = next(source for source in default_sources() if source.key == "grok")
    assert grok.session_paths == (profile / "auth.json",)
    assert grok.has_session() is True


def test_grok_blank_key_is_not_reported_as_connected(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"account":{"key":"   "}}', encoding="utf-8")
    spec = SourceSpec(
        key="grok",
        display="Grok",
        binaries=(),
        login_args=("login", "--oauth"),
        session_paths=(auth,),
    )

    assert spec.has_session() is False


def test_explicit_provider_roots_use_matching_profile_credentials(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-codex"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "other-grok"))
    claude_dir = tmp_path / "claude-profile" / "projects"
    codex_dir = tmp_path / "codex-profile" / "sessions"
    grok_dir = tmp_path / "grok-profile"

    sources = {
        source.key: source
        for source in default_sources(
            claude_dir=claude_dir,
            codex_dir=codex_dir,
            grok_dir=grok_dir,
        )
    }

    assert sources["claude"].session_paths == (claude_dir.parent / ".credentials.json",)
    assert sources["codex"].session_paths == (codex_dir.parent / "auth.json",)
    assert sources["grok"].session_paths == (grok_dir / "auth.json",)
    assert sources["claude"].login_env == (
        ("CLAUDE_CONFIG_DIR", str(claude_dir.parent)),
    )
    assert sources["codex"].login_env == (("CODEX_HOME", str(codex_dir.parent)),)
    assert sources["grok"].login_env == (("GROK_HOME", str(grok_dir)),)


def test_custom_profile_login_injects_env_and_preserves_process_env(
    monkeypatch, tmp_path: Path
) -> None:
    fake = tmp_path / ("cli.exe" if os.name == "nt" else "cli")
    fake.write_text("", encoding="utf-8")
    if os.name != "nt":
        fake.chmod(0o755)
    claude_dir = tmp_path / "claude-profile" / "projects"
    codex_dir = tmp_path / "codex-profile" / "sessions"
    grok_dir = tmp_path / "grok-profile"
    sources = {
        source.key: source
        for source in default_sources(
            claude_dir=claude_dir,
            codex_dir=codex_dir,
            grok_dir=grok_dir,
        )
    }
    expected = {
        "claude": ("CLAUDE_CONFIG_DIR", claude_dir.parent),
        "codex": ("CODEX_HOME", codex_dir.parent),
        "grok": ("GROK_HOME", grok_dir),
    }
    monkeypatch.setenv("LLM_USAGE_PRESERVE_ME", "yes")

    for key, (env_key, profile) in expected.items():
        captured: dict[str, object] = {}

        def runner(_command, _captured=captured, **kwargs):
            _captured.update(kwargs)
            return SimpleNamespace(returncode=0)

        ok, _message = login(
            replace(sources[key], binaries=(str(fake),)), runner=runner
        )

        assert ok is True
        env = captured["env"]
        assert isinstance(env, dict)
        assert env[env_key] == str(profile)
        assert env["LLM_USAGE_PRESERVE_ME"] == "yes"


def test_blank_profile_environment_matches_default_provider_paths(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
    monkeypatch.setenv("CODEX_HOME", "   ")
    monkeypatch.setenv("GROK_HOME", "   ")

    providers = {provider.key: provider for provider in build_providers()}
    sources = {source.key: source for source in default_sources()}

    assert providers["claude"].root == tmp_path / ".claude" / "projects"
    assert sources["claude"].session_paths == (
        tmp_path / ".claude" / ".credentials.json",
    )
    assert providers["codex"].root == tmp_path / ".codex" / "sessions"
    assert sources["codex"].session_paths == (tmp_path / ".codex" / "auth.json",)
    assert providers["grok"].root == tmp_path / ".grok"
    assert sources["grok"].session_paths == (tmp_path / ".grok" / "auth.json",)


def test_parse_selection_supports_multiple_and_all() -> None:
    assert parse_selection("1,3", 3) == {0, 2}
    assert parse_selection("a", 3) == {0, 1, 2}
    assert parse_selection("", 3) == set()
    assert parse_selection("9,nope", 3) == set()


def test_startup_menu_auto_keeps_connected_and_logs_selected_missing(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize("binary", ["agy", "opencode"])
def test_plain_binary_name_does_not_resolve_from_current_directory(
    monkeypatch, tmp_path: Path, binary: str
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    executable = cwd / (f"{binary}.EXE" if os.name == "nt" else binary)
    executable.write_text("", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(cwd))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".EXE")
    spec = SourceSpec(
        key=binary,
        display=binary,
        binaries=(binary,),
        login_args=("login",),
        session_paths=(),
    )

    assert spec.cli_path() is None


def test_login_runs_official_args(tmp_path: Path) -> None:
    fake = _fake_cli(tmp_path, "codex.exe")
    spec = SourceSpec(
        key="codex",
        display="Codex",
        binaries=(str(fake),),
        login_args=("login",),
        session_paths=(),
    )
    seen: list[list[str]] = []

    def runner(cmd, **_kwargs):
        seen.append(cmd)
        return SimpleNamespace(returncode=0)

    ok, msg = login(spec, runner=runner)
    assert ok is True
    assert seen[0][1:] == ["login"]
    assert "登入完成" in msg


def test_copilot_login_uses_matched_alias_when_executable_has_generic_name(
    monkeypatch, tmp_path: Path
) -> None:
    launcher = tmp_path / "generic-launcher.exe"
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        login_module,
        "trusted_which",
        lambda name: str(launcher) if name == "opencode" else None,
    )
    spec = SourceSpec(
        key="copilot",
        display="Copilot",
        binaries=("gh", "opencode"),
        login_args=("auth", "login"),
        session_paths=(),
    )
    seen: list[list[str]] = []

    ok, _msg = login(
        spec,
        runner=lambda command, **_kwargs: (
            seen.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert ok is True
    assert seen[0][1:] == ["providers", "login", "-p", "github-copilot"]


def test_login_start_error_does_not_echo_exception_details(tmp_path: Path) -> None:
    fake = _fake_cli(tmp_path, "codex.exe")
    spec = SourceSpec(
        key="codex",
        display="Codex",
        binaries=(str(fake),),
        login_args=("login",),
        session_paths=(),
    )

    def runner(*_args, **_kwargs):
        raise OSError("sentinel-secret")

    ok, msg = login(spec, runner=runner)
    assert ok is False
    assert "OSError" in msg
    assert "sentinel-secret" not in msg


def test_antigravity_agy_login_uses_read_only_usage_flow(tmp_path: Path) -> None:
    fake = _fake_cli(tmp_path, "agy.exe")
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
        if cmd[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="agy version 1.1.11", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ok, _msg = login(spec, runner=runner)
    assert ok is True
    assert seen[0][0] == [str(fake.resolve()), "--version"]
    assert seen[1][0] == [str(fake.resolve()), "-p", "/usage"]
    assert seen[1][1]["cwd"] == tmp_path
    assert seen[1][1]["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("output", ["agy version 1.1.10", "unknown"])
def test_antigravity_login_refuses_unsafe_version_without_usage(
    tmp_path: Path, output: str
) -> None:
    fake = _fake_cli(tmp_path, "agy.exe")
    spec = SourceSpec(
        key="antigravity",
        display="Antigravity",
        binaries=(str(fake),),
        login_args=("auth", "login"),
        session_paths=(),
        login_cwd=tmp_path,
    )
    seen: list[list[str]] = []

    def runner(cmd, **_kwargs):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    ok, msg = login(spec, runner=runner)

    assert ok is False
    assert "/usage" in msg
    assert seen == [[str(fake.resolve()), "--version"]]


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
