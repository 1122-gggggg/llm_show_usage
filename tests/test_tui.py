import select
import sys
from datetime import UTC, datetime
from io import BytesIO, StringIO, TextIOWrapper
from types import SimpleNamespace

import pytest
from rich.console import Console

from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow, TokenTotals
from llm_usage_monitor.tui import (
    _plain_report,
    _provider_cell,
    _quota_cell,
    _safe_snapshot,
    _wait_key,
    build_table,
    remaining_percent,
    run_live,
    run_once,
)


def test_remaining_percent_inverts_used() -> None:
    assert remaining_percent(0) == 100
    assert remaining_percent(97) == 3
    assert remaining_percent(100) == 0
    assert remaining_percent(float("nan")) is None
    assert remaining_percent(float("inf")) is None
    assert remaining_percent(-0.1) is None
    assert remaining_percent(100.1) is None
    assert remaining_percent("bad") is None


def test_quota_cell_shows_remaining_not_used() -> None:
    cell = _quota_cell(
        [
            QuotaWindow(
                label="週",
                used_percent=97.0,
                resets_at=datetime(2026, 8, 20, 3, 31, tzinfo=UTC),
            )
        ]
    )
    plain = cell.plain
    assert "3%" in plain
    assert "97%" not in plain
    assert "週" in plain


def test_once_summary_does_not_claim_auto_refresh() -> None:
    console = Console(record=True, width=100)
    console.print(build_table([], None, datetime(2026, 8, 23, tzinfo=UTC)))
    plain = console.export_text()

    assert "ONCE" in plain
    assert "AUTO" not in plain


def test_external_text_is_sanitized_in_actual_render_output() -> None:
    escape = "\x1b"
    clipboard_sequence = f"{escape}]52;c;ZmFrZQ=={escape}\\"
    snapshot = ProviderSnapshot(
        name=f"Provider{clipboard_sequence}\r\nName\u202e",
        plan=f"Pro{escape}[31mRed{escape}[0m\u2028Plan",
        quotas=[
            QuotaWindow(
                label="quota\tlabel\ue000",
                used_percent=None,
                resets_at=None,
                detail="detail\x00\r\ninjected\u0378",
            )
        ],
        notes=[f"warning{clipboard_sequence}\r\nfake footer\ud800"],
        by_model_today={
            f"model{escape}[31m red{escape}[0m\u2029name": TokenTotals(input=1)
        },
    )
    output = StringIO()
    console = Console(file=output, width=300, color_system=None, force_terminal=False)
    console.print(build_table([snapshot], None, datetime(2026, 8, 23, tzinfo=UTC)))
    rendered = output.getvalue()

    assert escape not in rendered
    assert "\r" not in rendered
    assert "\u202e" not in rendered
    assert "\ue000" not in rendered
    assert "\u0378" not in rendered
    assert "\ud800" not in rendered
    assert "Provider Name" in rendered
    assert "ProRed Plan" in rendered
    assert "quota label" in rendered
    assert "detail injected" in rendered
    assert "model red name" in rendered
    assert "warning fake footer" in rendered


def test_external_text_is_limited_in_actual_render_output() -> None:
    long_note = "x" * 1_000
    snapshot = ProviderSnapshot(name="Provider", plan=None, notes=[long_note])
    output = StringIO()
    console = Console(file=output, width=400, color_system=None, force_terminal=False)
    console.print(build_table([snapshot], None, datetime(2026, 8, 23, tzinfo=UTC)))
    rendered = output.getvalue()

    assert long_note not in rendered
    assert "x" * 239 + "…" in rendered
    assert "x" * 240 not in rendered


def test_plain_report_preserves_all_values_without_table_cropping() -> None:
    snapshot = ProviderSnapshot(
        name="Provider",
        plan="Pro",
        quotas=[QuotaWindow("週", 12.5, datetime(2026, 8, 24, tzinfo=UTC), "10/20")],
        today=TokenTotals(
            input=123_456, output=78_901, cached_input=234, cache_write=56, reasoning=7
        ),
        week=TokenTotals(
            input=987_654,
            output=321_098,
            cached_input=765,
            cache_write=43,
            reasoning=21,
        ),
        sessions_today=12,
        notes=["warning"],
    )

    report = _plain_report([snapshot], datetime(2026, 8, 23, tzinfo=UTC))

    for value in (
        "123456",
        "78901",
        "234",
        "56",
        "7",
        "987654",
        "321098",
        "765",
        "43",
        "21",
    ):
        assert value in report
    assert "remaining=87.5%" in report
    assert "warning=warning" in report


def test_invalid_quota_value_renders_unknown_instead_of_full() -> None:
    cell = _quota_cell([QuotaWindow("週", float("nan"), None)])

    assert "unknown" in cell.plain

    missing = _quota_cell([QuotaWindow("5h", None, None)])
    assert "unknown" in missing.plain
    assert "100%" not in cell.plain


def test_snapshot_exception_does_not_expose_message() -> None:
    class BrokenProvider:
        name = "Broken"
        key = "broken"

        def snapshot(self) -> ProviderSnapshot:
            raise ValueError("sentinel-secret")

    snapshot = _safe_snapshot(BrokenProvider())

    assert snapshot.notes == ["ValueError: snapshot failed"]
    assert "sentinel-secret" not in "".join(snapshot.notes)


def test_unknown_quota_is_not_reported_as_ok() -> None:
    snapshot = ProviderSnapshot(
        name="Provider",
        plan=None,
        quotas=[QuotaWindow("配額", None, None)],
    )

    assert "[UNKNOWN]" in _provider_cell(snapshot).plain
    assert "[OK]" not in _provider_cell(snapshot).plain


def test_mixed_unknown_quota_takes_precedence_over_ok() -> None:
    snapshot = ProviderSnapshot(
        name="Provider",
        plan=None,
        quotas=[
            QuotaWindow("known", 50, None),
            QuotaWindow("unknown", None, None),
        ],
    )

    assert "[UNKNOWN]" in _provider_cell(snapshot).plain
    assert "[OK]" not in _provider_cell(snapshot).plain


def test_low_quota_takes_precedence_over_mixed_unknown() -> None:
    snapshot = ProviderSnapshot(
        name="Provider",
        plan=None,
        quotas=[
            QuotaWindow("low", 95, None),
            QuotaWindow("unknown", None, None),
        ],
    )

    assert "[LOW]" in _provider_cell(snapshot).plain
    assert "[UNKNOWN]" not in _provider_cell(snapshot).plain


def test_narrow_table_keeps_provider_names_distinguishable() -> None:
    snapshots = [
        ProviderSnapshot(name="Codex", plan=None),
        ProviderSnapshot(name="Copilot", plan=None),
    ]
    output = StringIO()
    Console(file=output, width=72, color_system=None, force_terminal=False).print(
        build_table(snapshots, None, datetime(2026, 8, 23, tzinfo=UTC))
    )

    rendered = output.getvalue()
    assert "Codex" in rendered
    assert "Copilot" in rendered


def test_extreme_provider_timestamps_do_not_crash_rendering() -> None:
    extreme = datetime.max.replace(tzinfo=UTC)
    snapshot = ProviderSnapshot(
        name="Provider",
        plan=None,
        quotas=[QuotaWindow("週", 50, extreme)],
        last_event=extreme,
    )

    report = _plain_report([snapshot], datetime(2026, 8, 23, tzinfo=UTC))
    output = StringIO()
    Console(file=output, width=120, force_terminal=False).print(
        build_table([snapshot], None, datetime(2026, 8, 23, tzinfo=UTC))
    )

    assert "Provider" in report
    assert "Provider" in output.getvalue()


def test_run_once_uses_plain_output_when_force_color_marks_pipe_terminal(
    monkeypatch,
) -> None:
    output = StringIO()
    output.isatty = lambda: False
    console = SimpleNamespace(
        file=output,
        is_terminal=True,
        is_dumb_terminal=False,
        print=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Rich table should not be used for a pipe")
        ),
    )
    monkeypatch.setattr("llm_usage_monitor.tui.Console", lambda: console)

    run_once([])

    assert output.getvalue().startswith("LLM LIMITS")


def test_run_once_silences_non_tty_broken_pipe(monkeypatch) -> None:
    class BrokenPipe:
        def __init__(self, fail_on: str) -> None:
            self.fail_on = fail_on

        def isatty(self) -> bool:
            return False

        def write(self, _text: str) -> None:
            if self.fail_on == "write":
                raise BrokenPipeError

        def flush(self) -> None:
            if self.fail_on == "flush":
                raise BrokenPipeError

    for fail_on in ("write", "flush"):
        output = BrokenPipe(fail_on)
        console = SimpleNamespace(
            file=output,
            is_terminal=False,
            is_dumb_terminal=False,
            print=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "llm_usage_monitor.tui.Console", lambda console=console: console
        )

        run_once([])


def test_plain_output_falls_back_for_non_utf8_stream(monkeypatch) -> None:
    raw = BytesIO()
    output = TextIOWrapper(raw, encoding="cp1252", errors="strict")
    console = SimpleNamespace(
        file=output,
        is_terminal=False,
        is_dumb_terminal=False,
        print=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("llm_usage_monitor.tui.Console", lambda: console)

    run_once(
        [
            SimpleNamespace(
                name="來源",
                key="source",
                snapshot=lambda: ProviderSnapshot(name="來源", plan=None),
            )
        ]
    )
    output.flush()

    assert b"\\u" in raw.getvalue()


def test_live_refresh_uses_fixed_deadlines(monkeypatch) -> None:
    class StopLoop(Exception):
        pass

    now = [0.0]
    starts: list[float] = []

    def fake_render(_providers, _interval, **_kwargs):
        starts.append(now[0])
        if len(starts) == 4:
            raise StopLoop
        now[0] += 2.0
        return "rendered"

    class FakeLive:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def update(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr("llm_usage_monitor.tui.render_once", fake_render)
    monkeypatch.setattr("rich.live.Live", FakeLive)
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    with pytest.raises(StopLoop):
        run_live([], 10.0)

    assert starts == [0.0, 10.0, 20.0, 30.0]


def test_wait_key_sleeps_through_timeout_without_tty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    assert _wait_key(5.0) is None
    assert sleeps == [5.0]


def test_wait_key_reads_line_buffered_key(monkeypatch) -> None:
    fake_stdin = SimpleNamespace(isatty=lambda: True, readline=lambda: "L\n")
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(
        select, "select", lambda *_args, **_kwargs: ([fake_stdin], [], [])
    )

    assert _wait_key(5.0) == "l"


def test_wait_key_treats_empty_line_and_eof_as_no_key(monkeypatch) -> None:
    monkeypatch.setattr(select, "select", lambda *_args, **_kwargs: (["ready"], [], []))
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, readline=lambda: "\n")
    )
    assert _wait_key(5.0) is None

    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: True, readline=lambda: "")
    )
    assert _wait_key(5.0) is None
    assert sleeps == [5.0]


def _fake_live_class(events: list[str]):
    class FakeLive:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args) -> None:
            events.append("exit")

        def update(self, *_args, **_kwargs) -> None:
            events.append("update")

        def stop(self) -> None:
            events.append("stop")

        def start(self) -> None:
            events.append("start")

    return FakeLive


def test_live_quits_on_q_key(monkeypatch) -> None:
    keys = iter(["q"])
    monkeypatch.setattr("llm_usage_monitor.tui._wait_key", lambda _timeout: next(keys))
    monkeypatch.setattr(
        "llm_usage_monitor.tui.render_once", lambda *_args, **_kwargs: "rendered"
    )
    events: list[str] = []
    monkeypatch.setattr("rich.live.Live", _fake_live_class(events))

    run_live([], 10.0)

    assert events == ["enter", "exit"]


def test_live_login_runs_menu_and_resumes_on_l_key(monkeypatch) -> None:
    keys = iter(["l", "q"])
    monkeypatch.setattr("llm_usage_monitor.tui._wait_key", lambda _timeout: next(keys))
    monkeypatch.setattr(
        "llm_usage_monitor.tui.render_once", lambda *_args, **_kwargs: "rendered"
    )
    events: list[str] = []
    monkeypatch.setattr("rich.live.Live", _fake_live_class(events))
    calls: dict[str, object] = {}

    def fake_sessions(wanted, *, select, printer, sources):
        calls["wanted"] = wanted
        events.append("login-menu")
        return []

    monkeypatch.setattr("llm_usage_monitor.tui.ensure_sessions", fake_sessions)

    run_live([SimpleNamespace(key="codex", name="Codex")], 10.0)

    assert events == ["enter", "stop", "login-menu", "start", "update", "exit"]
    assert calls["wanted"] == {"codex"}


def test_key_hint_footer_marks_offline_sources() -> None:
    snapshots = [ProviderSnapshot(name="Codex", plan=None)]
    output = StringIO()
    Console(file=output, width=100, color_system=None, force_terminal=False).print(
        build_table(snapshots, 10.0, datetime(2026, 8, 23, tzinfo=UTC), key_hint=True)
    )
    rendered = output.getvalue()

    assert "1 OFFLINE" in rendered
    assert "l 登入未連接來源" in rendered
    assert "q 離開" in rendered


def test_table_hides_key_hint_by_default() -> None:
    snapshots = [ProviderSnapshot(name="Codex", plan=None)]
    output = StringIO()
    Console(file=output, width=100, color_system=None, force_terminal=False).print(
        build_table(snapshots, 10.0, datetime(2026, 8, 23, tzinfo=UTC))
    )
    rendered = output.getvalue()

    assert "q 離開" not in rendered
