import errno
import math
import os
import select
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_usage_monitor.aggregate import fmt_tokens
from llm_usage_monitor.login import default_sources, ensure_sessions
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow, TokenTotals
from llm_usage_monitor.providers.base import Provider

_PROVIDER_STYLE = {
    "Claude": "bold #f4a261",
    "Codex": "bold #7eb8da",
    "Grok": "bold #e8e4d9",
    "OpenCode": "bold #9ece6a",
    "Copilot": "bold #7aa2f7",
    "Antigravity": "bold #bb9af7",
    "OMP Claude": "bold #f4a261",
    "OMP Codex": "bold #7eb8da",
    "OMP Antigravity": "bold #bb9af7",
    "OMP Grok": "bold #e8e4d9",
    "OMP GO": "bold #9ece6a",
}

_MAX_PROVIDER_NAME = 64
_MAX_PLAN = 96
_MAX_QUOTA_TEXT = 120
_MAX_MODEL_NAME = 160
_MAX_NOTE = 240
_MAX_MODEL_ROWS = 50


def _strip_terminal_sequences(value: str) -> str:
    """Remove ANSI/ECMA-48 escape sequences, including OSC strings."""
    result: list[str] = []
    index = 0
    size = len(value)
    while index < size:
        char = value[index]
        if char != "\x1b":
            result.append(char)
            index += 1
            continue

        index += 1
        if index >= size:
            break
        kind = value[index]
        index += 1

        if kind == "[":  # Control Sequence Introducer (CSI)
            while index < size:
                final = ord(value[index])
                index += 1
                if 0x40 <= final <= 0x7E:
                    break
            continue

        if kind in "]PX^_":  # OSC, DCS, SOS, PM, or APC
            while index < size:
                if kind == "]" and value[index] == "\x07":
                    index += 1
                    break
                if value[index] == "\x9c":  # C1 String Terminator
                    index += 1
                    break
                if (
                    value[index] == "\x1b"
                    and index + 1 < size
                    and value[index + 1] == "\\"
                ):
                    index += 2
                    break
                index += 1
            continue

        if 0x20 <= ord(kind) <= 0x2F:
            while index < size and 0x20 <= ord(value[index]) <= 0x2F:
                index += 1
            if index < size and 0x30 <= ord(value[index]) <= 0x7E:
                index += 1

    return "".join(result)


def _safe_external_text(value: object, *, max_length: int) -> str:
    text = _strip_terminal_sequences(str(value))
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x85", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    text = "".join(
        char for char in text if not unicodedata.category(char).startswith("C")
    )
    text = text.strip()
    if len(text) > max_length:
        text = f"{text[: max_length - 1].rstrip()}…"
    return text


def remaining_percent(used: object) -> float | None:
    try:
        value = float(used)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        return None
    return 100.0 - value


def _tone(remaining: float) -> str:
    if remaining <= 10:
        return "bold #f7768e"
    if remaining <= 20:
        return "#e0af68"
    return "#9ece6a"


def _bar(remaining: float) -> Text:
    fill = max(0, min(10, round(remaining / 100 * 10)))
    return Text("━" * fill, style=_tone(remaining)) + Text(
        "─" * (10 - fill), style="dim"
    )


def _fmt_reset(ts: datetime | None) -> str:
    local = _safe_local_time(ts)
    if local is None:
        return ""
    return local.strftime("%m-%d %H:%M")


def _fmt_last(ts: datetime | None) -> str:
    local = _safe_local_time(ts)
    if local is None:
        return "—"
    return local.strftime("%H:%M:%S")


def _safe_local_time(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return ts.astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def _token_cell(totals: TokenTotals, cost: float | None = None) -> Text:
    text = Text()
    text.append(f"↑{fmt_tokens(totals.input)}", style="#7aa2f7")
    text.append(" ")
    text.append(f"↓{fmt_tokens(totals.output)}", style="#bb9af7")
    if cost is not None and cost > 0:
        text.append(f" ${cost:.4f}", style="#e0af68")
    return text


def _quota_cell(quotas: list[QuotaWindow], *, compact: bool = False) -> Text:
    text = Text(overflow="fold")
    if not quotas:
        return Text("尚無配額資料", style="dim")
    for i, quota in enumerate(quotas):
        if i:
            text.append("\n")
        label = _safe_external_text(quota.label, max_length=_MAX_QUOTA_TEXT) or "配額"
        left = remaining_percent(quota.used_percent)
        if left is None:
            text.append("unknown", style="#e0af68")
        else:
            text.append(f"{left:>3.0f}%", style=_tone(left))
            text.append(" ")
            text.append_text(_bar(left))
        text.append(f"  {label}")
        reset = _fmt_reset(quota.resets_at)
        if reset:
            text.append("\n  " if compact else "  · ", style="dim")
            text.append(f"重設 {reset}", style="dim")
        if quota.detail:
            detail = _safe_external_text(quota.detail, max_length=_MAX_QUOTA_TEXT)
            if detail:
                text.append(f"\n  {detail}", style="dim")
    return text


def _provider_cell(snap: ProviderSnapshot) -> Text:
    remaining = [
        value
        for quota in snap.quotas
        if (value := remaining_percent(quota.used_percent)) is not None
    ]
    has_unknown = len(remaining) != len(snap.quotas)
    if not snap.quotas or remaining and min(remaining) <= 10:
        health = "#f7768e"
        status = "OFFLINE" if not snap.quotas else "LOW"
    elif remaining and min(remaining) <= 20:
        health = "#e0af68"
        status = "LOW"
    elif has_unknown:
        health = "#e0af68"
        status = "UNKNOWN"
    else:
        health = "#9ece6a"
        status = "OK"
    name = _safe_external_text(snap.name, max_length=_MAX_PROVIDER_NAME) or "unknown"
    text = Text(name, style=_PROVIDER_STYLE.get(name, "bold"), overflow="fold")
    text.append(f"\n● [{status}]", style=health)
    if snap.plan:
        plan = _safe_external_text(snap.plan, max_length=_MAX_PLAN)
        if plan:
            text.append("\n")
            text.append(plan, style="dim")
    meta: list[str] = []
    if snap.sessions_today:
        meta.append(f"{snap.sessions_today} sessions")
    if snap.last_event is not None:
        meta.append(_fmt_last(snap.last_event))
    if meta:
        text.append("\n")
        text.append(" · ".join(meta), style="dim")
    return text


def _model_rows(snap: ProviderSnapshot) -> Iterator[tuple[Text, TokenTotals]]:
    models = sorted(
        snap.by_model_today.items(), key=lambda item: item[1].total, reverse=True
    )
    for model, totals in models[:_MAX_MODEL_ROWS]:
        name = _safe_external_text(model, max_length=_MAX_MODEL_NAME) or "unknown"
        yield Text(f"└ {name}", style="dim", overflow="fold"), totals
    if len(models) > _MAX_MODEL_ROWS:
        other = TokenTotals()
        for _model, totals in models[_MAX_MODEL_ROWS:]:
            other.add(totals)
        yield Text(f"└ 其他 {len(models) - _MAX_MODEL_ROWS} 個模型", style="dim"), other


def _overview_table(snapshots: list[ProviderSnapshot]) -> Table:
    table = Table(
        box=box.SIMPLE,
        expand=True,
        show_edge=False,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
    )
    table.add_column("來源 / 狀態", ratio=2)
    table.add_column("剩餘配額 / 重設時間", ratio=5)
    table.add_column("今日 tokens", ratio=2)
    table.add_column("本週 tokens", ratio=2)
    for index, snap in enumerate(snapshots):
        if index:
            table.add_section()
        table.add_row(
            _provider_cell(snap),
            _quota_cell(snap.quotas),
            _token_cell(snap.today, snap.cost_today),
            _token_cell(snap.week),
        )
        for name, totals in _model_rows(snap):
            table.add_row(Text(""), name, _token_cell(totals), Text(""))
    return table


def _provider_card(snap: ProviderSnapshot) -> Panel:
    usage = Text("今日  ", style="dim")
    usage.append_text(_token_cell(snap.today, snap.cost_today))
    usage.append("\n本週  ", style="dim")
    usage.append_text(_token_cell(snap.week))
    parts: list[RenderableType] = [
        _provider_cell(snap),
        Text(""),
        _quota_cell(snap.quotas, compact=True),
        Text(""),
        usage,
    ]
    if snap.by_model_today:
        models = Table.grid(expand=True, padding=(0, 1))
        models.add_column(ratio=2, overflow="fold")
        models.add_column(ratio=1)
        for name, totals in _model_rows(snap):
            models.add_row(name, _token_cell(totals))
        parts.extend([Text(""), models])
    return Panel(Group(*parts), box=box.ROUNDED, border_style="dim", padding=(0, 1))


def _build_dashboard(
    snapshots: list[ProviderSnapshot],
    interval: float | None,
    now: datetime,
    *,
    width: int,
) -> RenderableType:
    notes = Text(overflow="fold")
    remaining_values: list[float] = []
    for snap in snapshots:
        safe_name = (
            _safe_external_text(snap.name, max_length=_MAX_PROVIDER_NAME) or "unknown"
        )
        for note in snap.notes:
            safe_note = _safe_external_text(note, max_length=_MAX_NOTE)
            if safe_note:
                if notes:
                    notes.append("\n")
                notes.append(f"{safe_name} · ", style="bold #e0af68")
                notes.append(safe_note)
        remaining_values.extend(
            value
            for quota in snap.quotas
            if (value := remaining_percent(quota.used_percent)) is not None
        )

    online = sum(bool(snap.quotas) for snap in snapshots)
    offline = len(snapshots) - online
    low = sum(value <= 10 for value in remaining_values)
    summary = Text()
    summary.append(f"{online}/{len(snapshots)} CONNECTED", style="bold #9ece6a")
    summary.append("   ·   ", style="dim")
    summary.append(f"{low} LOW ≤10%", style="bold #f7768e" if low else "dim")
    if offline:
        summary.append("   ·   ", style="dim")
        summary.append(f"{offline} OFFLINE", style="#e0af68")

    header = Text("LLM LIMITS", style="bold #7aa2f7")
    header.append("  /  剩餘配額", style="dim")
    sync = Text(f"更新 {_fmt_last(now)}", style="dim")
    sync.append("   ·   ", style="dim")
    sync.append("ONCE" if interval is None else f"AUTO {interval:g}s", style="#7aa2f7")
    parts: list[RenderableType] = [
        Panel(
            Group(header, Text(""), summary, sync),
            box=box.ROUNDED,
            border_style="#7aa2f7",
            padding=(0, 1),
        ),
    ]
    if not snapshots:
        parts.append(
            Panel(
                Text(
                    "尚無可顯示的來源\n使用 omp auth-broker login 登入後重新開啟儀表板。",
                    overflow="fold",
                ),
                title="開始使用",
                title_align="left",
                border_style="dim",
            )
        )
    elif width >= 120:
        parts.append(_overview_table(snapshots))
    else:
        parts.extend(_provider_card(snap) for snap in snapshots)
    if notes:
        parts.append(
            Panel(
                notes,
                title="! 來源提醒",
                title_align="left",
                border_style="#e0af68",
                padding=(0, 1),
            )
        )
    return Group(*parts)


def _navigation(start: int, stop: int, total: int) -> Text:
    hint = Text("j/k", style="bold #7aa2f7")
    hint.append(" 捲動  ·  ")
    hint.append("l", style="bold #7aa2f7")
    hint.append(" 登入  ·  ")
    hint.append("q", style="bold #7aa2f7")
    hint.append(" 離開\n")
    if os.name != "nt":
        hint.append("按鍵後 Enter  ·  ", style="dim")
    hint.append(f"{start}–{stop} / {total} 行", style="dim")
    return hint


@dataclass
class _Dashboard:
    snapshots: list[ProviderSnapshot]
    interval: float | None
    now: datetime
    key_hint: bool
    scroll: int = 0

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        dashboard = _build_dashboard(
            self.snapshots,
            self.interval,
            self.now,
            width=options.max_width,
        )
        if not self.key_hint:
            yield dashboard
            return
        render_options = options.update(height=None)
        lines = console.render_lines(dashboard, render_options, new_lines=True)
        total = len(lines)
        navigation = console.render_lines(
            _navigation(total, total, total), render_options, new_lines=True
        )
        available = max(1, options.max_height - len(navigation))
        self.scroll = max(0, min(self.scroll, total - available))
        stop = min(total, self.scroll + available)
        for line in lines[self.scroll : stop]:
            yield from line
        yield _navigation(self.scroll + 1, stop, total)


def build_table(
    snapshots: list[ProviderSnapshot],
    interval: float | None,
    now: datetime,
    *,
    key_hint: bool = False,
) -> RenderableType:
    return _Dashboard(snapshots, interval, now, key_hint)


def _safe_snapshot(provider: Provider) -> ProviderSnapshot:
    try:
        return provider.snapshot()
    except Exception as exc:  # noqa: BLE001
        note = f"{exc.__class__.__name__}: snapshot failed"
        return ProviderSnapshot(name=provider.name, plan=None, notes=[note])


def _collect_snapshots(providers: list[Provider]) -> list[ProviderSnapshot]:
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        return list(pool.map(_safe_snapshot, providers))


def _plain_totals(totals: TokenTotals) -> str:
    return (
        f"input={totals.input} output={totals.output} cached_input={totals.cached_input} "
        f"cache_write={totals.cache_write} reasoning={totals.reasoning}"
    )


def _plain_report(snapshots: list[ProviderSnapshot], now: datetime) -> str:
    lines = [f"LLM LIMITS  {now.astimezone().isoformat(timespec='seconds')}"]
    for snap in snapshots:
        name = (
            _safe_external_text(snap.name, max_length=_MAX_PROVIDER_NAME) or "unknown"
        )
        status = "CONNECTED" if snap.quotas else "UNAVAILABLE"
        plan = _safe_external_text(snap.plan, max_length=_MAX_PLAN) if snap.plan else ""
        heading = f"[{status}] {name}"
        if plan:
            heading += f"  plan={plan}"
        lines.append(heading)
        for quota in snap.quotas:
            label = (
                _safe_external_text(quota.label, max_length=_MAX_QUOTA_TEXT) or "配額"
            )
            left = remaining_percent(quota.used_percent)
            remaining = "unknown" if left is None else f"{left:g}%"
            parts = [f"  quota={label}", f"remaining={remaining}"]
            reset = _safe_local_time(quota.resets_at)
            if reset is not None:
                parts.append(f"reset={reset.isoformat(timespec='seconds')}")
            if quota.detail:
                detail = _safe_external_text(quota.detail, max_length=_MAX_QUOTA_TEXT)
                if detail:
                    parts.append(f"detail={detail}")
            lines.append("  ".join(parts))
        lines.append(f"  today  {_plain_totals(snap.today)}")
        lines.append(f"  week   {_plain_totals(snap.week)}")
        lines.append(f"  sessions_today={snap.sessions_today}")
        last_event = _safe_local_time(snap.last_event)
        if last_event is not None:
            lines.append(f"  last_event={last_event.isoformat(timespec='seconds')}")
        if snap.cost_today is not None:
            lines.append(f"  cost_today={snap.cost_today}")
        for model, totals in sorted(
            snap.by_model_today.items(), key=lambda item: item[1].total, reverse=True
        ):
            safe_model = (
                _safe_external_text(model, max_length=_MAX_MODEL_NAME) or "unknown"
            )
            lines.append(f"  model={safe_model}  {_plain_totals(totals)}")
        for note in snap.notes:
            safe_note = _safe_external_text(note, max_length=_MAX_NOTE)
            if safe_note:
                lines.append(f"  warning={safe_note}")
    return "\n".join(lines)


def render_once(
    providers: list[Provider],
    interval: float | None = 10.0,
    *,
    key_hint: bool = False,
    scroll: int = 0,
) -> _Dashboard:
    snaps = _collect_snapshots(providers)
    now = datetime.now().astimezone()
    return _Dashboard(snaps, interval, now, key_hint, scroll)


def run_once(providers: list[Provider], interval: float = 10.0) -> None:
    console = Console()
    snapshots = _collect_snapshots(providers)
    now = datetime.now().astimezone()
    is_tty = getattr(console.file, "isatty", lambda: False)()
    if is_tty and console.is_terminal and not console.is_dumb_terminal:
        console.print(build_table(snapshots, None, now))
        return
    try:
        _write_plain(console.file, f"{_plain_report(snapshots, now)}\n")
        console.file.flush()
    except OSError as exc:
        if not _is_broken_pipe(exc):
            raise
        _silence_broken_pipe(console.file)


def _is_broken_pipe(exc: OSError) -> bool:
    # CPython reports a closed Windows anonymous pipe as EINVAL rather than EPIPE.
    return isinstance(exc, BrokenPipeError) or (
        os.name == "nt" and exc.errno == errno.EINVAL
    )


def _silence_broken_pipe(file) -> None:
    try:
        descriptor = file.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(null_descriptor, descriptor)
    except OSError:
        pass
    finally:
        os.close(null_descriptor)


def _write_plain(file, text: str) -> None:
    try:
        file.write(text)
    except UnicodeEncodeError:
        encoding = getattr(file, "encoding", None) or "ascii"
        escaped = text.encode(encoding, errors="backslashreplace").decode(encoding)
        file.write(escaped)


def _stdin_select(prompt: str = "") -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _stderr_note(message: str) -> None:
    print(message, file=sys.stderr)


def _wait_key(timeout: float) -> str | None:
    """Wait up to timeout seconds for a keypress; None on timeout or when unavailable."""
    if timeout <= 0:
        return None
    try:
        if not sys.stdin.isatty():
            time.sleep(timeout)
            return None
        if os.name == "nt":
            import msvcrt

            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if msvcrt.kbhit():
                    char = msvcrt.getwch()
                    return char.lower() if char else None
                time.sleep(0.05)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        line = sys.stdin.readline()
        if not line:
            time.sleep(timeout)
            return None
        return line.strip()[:1].lower() or None
    except (OSError, ValueError):
        time.sleep(timeout)
        return None


def run_live(
    providers: list[Provider],
    interval: float = 10.0,
    *,
    sources=None,
    select: Callable[[str], str] | None = None,
    printer: Callable[[str], None] | None = None,
) -> None:
    from rich.live import Live

    console = Console()
    keys = sys.stdin.isatty()
    select_fn = select or _stdin_select
    printer_fn = printer or _stderr_note
    wanted = {provider.key for provider in providers}
    deadline = time.monotonic() + interval
    scroll = 0
    dashboard = render_once(providers, interval, key_hint=keys)
    with Live(dashboard, auto_refresh=False, console=console) as live:
        while True:
            key = _wait_key(max(0.0, deadline - time.monotonic()))
            if key == "q":
                return
            if key in ("j", "k"):
                dashboard.scroll += 5 if key == "j" else -5
                live.update(dashboard, refresh=True)
                scroll = dashboard.scroll
                continue
            if key == "l":
                live.stop()
                try:
                    ensure_sessions(
                        wanted,
                        select=select_fn,
                        printer=printer_fn,
                        sources=sources if sources is not None else default_sources(),
                    )
                finally:
                    live.start()
                deadline = time.monotonic() + interval
                dashboard = render_once(
                    providers, interval, key_hint=keys, scroll=scroll
                )
                live.update(dashboard, refresh=True)
                continue
            if time.monotonic() < deadline:
                continue
            dashboard = render_once(providers, interval, key_hint=keys, scroll=scroll)
            live.update(dashboard, refresh=True)
            deadline += interval
            now = time.monotonic()
            while deadline <= now:
                deadline += interval
