from datetime import UTC, datetime

from llm_usage_monitor.model import TokenTotals


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def add_to_buckets(
    totals_today: TokenTotals,
    totals_week: TokenTotals,
    ts: datetime,
    delta: TokenTotals,
    now: datetime,
) -> None:
    local_ts = ts.astimezone()
    local_now = now.astimezone()
    if local_ts.date() == local_now.date():
        totals_today.add(delta)
    if local_ts.isocalendar()[:2] == local_now.isocalendar()[:2]:
        totals_week.add(delta)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts
