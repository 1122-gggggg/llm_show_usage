import subprocess

import llm_usage_monitor.__main__ as main_module
from llm_usage_monitor.__main__ import main
from llm_usage_monitor.update import (
    UPDATE_SPECS,
    UpdateSpec,
    build_plan_table,
    build_result_table,
    describe_plan,
    run_all_updates,
    run_update,
    run_updates_command,
)


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_specs_cover_all_dashboard_sources() -> None:
    assert [spec.key for spec in UPDATE_SPECS] == [
        "claude",
        "codex",
        "grok",
        "opencode",
        "copilot",
        "antigravity",
        "ohmypi",
    ]
    assert UPDATE_SPECS[3].args == ("upgrade",)
    assert UPDATE_SPECS[4].binaries == ("gh",)


def test_missing_binary_is_skipped(monkeypatch) -> None:
    monkeypatch.setattr("llm_usage_monitor.update.trusted_which", lambda _name: None)
    result = run_update(UPDATE_SPECS[0])
    assert result.status == "skipped"
    assert result.binary is None


def test_successful_update_reports_version_change(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda _name: "/bin/fake-claude"
    )
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-1] == "--version":
            return _completed(
                cmd, stdout="2.1.263\n" if len(calls) <= 1 else "2.1.264\n"
            )
        return _completed(cmd, stdout="already up to date")

    result = run_update(UPDATE_SPECS[0], runner=runner)
    assert result.status == "updated"
    assert result.version_before == "2.1.263"
    assert result.version_after == "2.1.264"
    assert [cmd[1:] for cmd in calls] == [["--version"], ["update"], ["--version"]]


def test_unchanged_version_is_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda _name: "/bin/fake-codex"
    )

    def runner(cmd, **kwargs):
        if cmd[-1] == "--version":
            return _completed(cmd, stdout="0.153.4\n")
        return _completed(cmd, stdout="latest")

    result = run_update(UPDATE_SPECS[1], runner=runner)
    assert result.status == "ok"


def test_failed_update_keeps_hint(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda _name: "/bin/gh"
    )

    def runner(cmd, **kwargs):
        if cmd[-1] == "--version":
            return _completed(cmd, stdout="2.95.0")
        return _completed(cmd, returncode=1, stderr="network down")

    failed = run_update(
        UpdateSpec(
            key="copilot",
            display="GitHub CLI 擴充",
            binaries=("gh",),
            args=("extension",),
            hint="gh 本體請用系統套件管理器更新",
        ),
        runner=runner,
    )
    assert failed.status == "failed"
    assert "network down" in failed.detail
    assert "系統套件管理器" in failed.detail


def test_timeout_is_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda _name: "/bin/fake-grok"
    )

    def runner(cmd, **kwargs):
        if cmd[-1] == "--version":
            return _completed(cmd, stdout="1.0.13")
        raise subprocess.TimeoutExpired(cmd, timeout=300)

    result = run_update(UPDATE_SPECS[2], runner=runner)
    assert result.status == "failed"
    assert "逾時" in result.detail


def test_parallel_results_keep_spec_order(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda name: f"/bin/{name}"
    )

    def runner(cmd, **kwargs):
        return _completed(cmd, stdout="v1")

    results = run_all_updates(UPDATE_SPECS[:3], runner=runner)
    assert [result.spec.key for result in results] == ["claude", "codex", "grok"]
    assert all(result.status == "ok" for result in results)


def test_describe_plan_marks_missing(monkeypatch) -> None:
    monkeypatch.setattr("llm_usage_monitor.update.trusted_which", lambda _name: None)
    resolved = describe_plan()
    assert all(result.status == "dry" for result in resolved)
    assert build_plan_table(resolved) is not None
    assert build_result_table(resolved) is not None


def test_update_check_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr("llm_usage_monitor.update.trusted_which", lambda _name: None)
    assert run_updates_command(check_only=True) == 0
    assert "未安裝" in capsys.readouterr().out


def test_update_command_exit_codes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.update.trusted_which", lambda _name: "/bin/tool"
    )

    def ok_runner(cmd, **kwargs):
        return _completed(cmd, stdout="v1")

    assert run_updates_command(runner=ok_runner) == 0

    def fail_runner(cmd, **kwargs):
        if cmd[-1] == "--version":
            return _completed(cmd, stdout="v1")
        return _completed(cmd, returncode=1, stderr="bad")

    assert run_updates_command(runner=fail_runner) == 1
    assert "更新失敗" in capsys.readouterr().out


def test_main_routes_update_flags(monkeypatch) -> None:
    seen: list[bool] = []

    def fake_command(*, check_only, **kwargs):
        seen.append(check_only)
        return 0

    monkeypatch.setattr(main_module, "run_updates_command", fake_command)
    assert main(["--update"]) == 0
    assert main(["--update-check"]) == 0
    assert main(["--update", "--update-check"]) == 0
    assert seen == [False, True, False]


def test_self_update_reports_missing_uv(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.self_update.trusted_which", lambda _name: None
    )

    assert main(["update"]) == 1
    assert "找不到 uv" in capsys.readouterr().err


def test_self_update_reports_launch_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "llm_usage_monitor.self_update.trusted_which", lambda _name: "/bin/uv"
    )

    def denied(*_args):
        raise PermissionError("access denied")

    monkeypatch.setattr("llm_usage_monitor.self_update.os.execv", denied)

    assert main(["update"]) == 1
    captured = capsys.readouterr()
    assert "無法啟動更新" in captured.err
    assert "更新完成" not in captured.out
