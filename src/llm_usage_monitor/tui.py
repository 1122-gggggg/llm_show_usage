from datetime import datetime

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_usage_monitor.aggregate import fmt_tokens
from llm_usage_monitor.model import ProviderSnapshot, QuotaWindow, TokenTotals
from llm_usage_monitor.providers.base import Provider

_PROVIDER_STYLE = {
    "Claude": "bold #f4a261",
    "Codex": "bold #7eb8da",
    "Grok": "bold #e8e4d9",
    "OpenCode": "bold #9ece6a",
    "Copilot": "bold #7aa2f7",
    "Antigravity": "bold #bb9af7",
}


def remaining_percent(used: float) -> float:
    return max(0.0, min(100.0, 100.0 - used))


def _tone(remaining: float) -> str:
    if remaining <= 5:
        return "bold #f7768e"
    if remaining <= 20:
        return "#e0af68"
    if remaining <= 50:
        return "#c0caf5"
    return "#9ece6a"


def _bar(remaining: float) -> Text:
    fill = max(0, min(6, round(remaining / 100 * 6)))
    style = _tone(remaining)
    return Text("━" * fill, style=style) + Text("─" * (6 - fill), style="dim #414868")


def _fmt_reset(ts: datetime | None) -> str:
    if ts is None:
        return ""
    return ts.astimezone().strftime("%m-%d %H:%M")


def _fmt_last(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.astimezone().strftime("%H:%M:%S")


def _token_cell(totals: TokenTotals, cost: float | None = None) -> Text:
    text = Text()
    text.append(f"↑{fmt_tokens(totals.input)}", style="#7aa2f7")
    text.append(" ")
    text.append(f"↓{fmt_tokens(totals.output)}", style="#bb9af7")
    if cost is not None and cost > 0:
        text.append(f" ${cost:.4f}", style="#e0af68")
    return text


def _quota_cell(quotas: list[QuotaWindow]) -> Text:
    text = Text()
    if not quotas:
        text.append("—", style="dim")
        return text
    for i, q in enumerate(quotas):
        if i:
            text.append("\n")
        text.append(f"{q.label} ", style="dim #a9b1d6")
        if q.used_percent is None:
            if q.detail:
                text.append(q.detail, style="#c0caf5")
            if q.resets_at is not None:
                text.append(f"  {_fmt_reset(q.resets_at)}", style="dim")
            continue
        left = remaining_percent(q.used_percent)
        text.append_text(_bar(left))
        text.append(f"  {left:.0f}%", style=_tone(left))
        if q.resets_at is not None:
            text.append(f"  {_fmt_reset(q.resets_at)}", style="dim #565f89")
        if q.detail:
            text.append(f"  {q.detail}", style="dim #a9b1d6")
    return text


def _provider_cell(snap: ProviderSnapshot) -> Text:
    remaining = [
        remaining_percent(quota.used_percent)
        for quota in snap.quotas
        if quota.used_percent is not None
    ]
    if not snap.quotas or remaining and min(remaining) <= 5:
        health = "#f7768e"
    elif remaining and min(remaining) <= 20:
        health = "#e0af68"
    else:
        health = "#9ece6a"
    text = Text("● ", style=health)
    text.append(snap.name, style=_PROVIDER_STYLE.get(snap.name, "bold"))
    if snap.plan:
        text.append("\n")
        text.append(snap.plan, style="dim italic #a9b1d6")
    meta: list[str] = []
    if snap.sessions_today:
        meta.append(f"{snap.sessions_today} sessions")
    if snap.last_event is not None:
        meta.append(_fmt_last(snap.last_event))
    if meta:
        text.append("\n")
        text.append(" · ".join(meta), style="dim #565f89")
    return text


def build_table(snapshots: list[ProviderSnapshot], interval: float, now: datetime) -> RenderableType:
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        pad_edge=False,
        collapse_padding=True,
        show_edge=False,
        header_style="bold #565f89",
        row_styles=["", "on #16161e"],
        padding=(0, 1),
    )
    table.add_column("來源", no_wrap=True, min_width=14)
    table.add_column("剩餘配額", no_wrap=True, min_width=34)
    table.add_column("今日", no_wrap=True, min_width=12)
    table.add_column("本週", no_wrap=True, min_width=12)

    for snap in snapshots:
        table.add_row(
            _provider_cell(snap),
            _quota_cell(snap.quotas),
            _token_cell(snap.today, snap.cost_today),
            _token_cell(snap.week),
        )
        models = sorted(
            snap.by_model_today.items(),
            key=lambda item: item[1].total,
            reverse=True,
        )
        for model, totals in models:
            name = Text("  └ ", style="dim #414868")
            name.append(model, style="dim #a9b1d6")
            table.add_row(
                name,
                Text(""),
                _token_cell(totals),
                Text(""),
            )

    notes: list[str] = []
    remaining_values: list[float] = []
    for snap in snapshots:
        notes.extend(f"{snap.name} · {note}" for note in snap.notes)
        remaining_values.extend(
            remaining_percent(quota.used_percent)
            for quota in snap.quotas
            if quota.used_percent is not None
        )

    online = sum(bool(snap.quotas) for snap in snapshots)
    low = sum(value <= 10 for value in remaining_values)
    summary = Text()
    summary.append("● ", style="#9ece6a")
    summary.append(f"{online}/{len(snapshots)} CONNECTED", style="bold #c0caf5")
    summary.append("   ")
    summary.append("● ", style="#f7768e" if low else "#414868")
    summary.append(f"{low} LOW", style="#f7768e" if low else "dim #565f89")
    summary.append("   ")
    summary.append(f"AUTO {interval:g}s", style="#7aa2f7")

    clock = now.astimezone().strftime("%H:%M:%S")
    footer = Text()
    footer.append(f"last sync  {clock}", style="dim #565f89")
    if notes:
        footer.append("\n")
        for index, note in enumerate(notes):
            if index:
                footer.append("\n")
            footer.append("! ", style="#e0af68")
            footer.append(note, style="dim #e0af68")

    title = Text("LLM LIMITS", style="bold #7aa2f7")
    title.append("  remaining quota", style="dim italic #a9b1d6")
    return Panel(
        Group(summary, Text(""), table, Text(""), footer),
        title=title,
        title_align="left",
        border_style="#414868",
        padding=(0, 1),
        box=box.ROUNDED,
    )


def render_once(providers: list[Provider], interval: float = 10.0) -> RenderableType:
    snaps = [provider.snapshot() for provider in providers]
    now = datetime.now().astimezone()
    return build_table(snaps, interval, now)


def run_once(providers: list[Provider], interval: float = 10.0) -> None:
    Console().print(render_once(providers, interval))


def run_live(providers: list[Provider], interval: float = 10.0) -> None:
    import time

    from rich.live import Live

    console = Console()
    initial = render_once(providers, interval)
    with Live(initial, auto_refresh=False, console=console) as live:
        deadline = time.monotonic()
        try:
            while True:
                deadline += interval
                time.sleep(max(0.0, deadline - time.monotonic()))
                live.update(render_once(providers, interval), refresh=True)
        except KeyboardInterrupt:
            pass
