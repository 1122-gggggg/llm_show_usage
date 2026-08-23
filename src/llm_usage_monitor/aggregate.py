from datetime import UTC, date, datetime

from llm_usage_monitor.model import TokenTotals

LocalPeriod = tuple[date, int, int, int | None]


def parse_iso(value: str) -> datetime:
    return ensure_aware(datetime.fromisoformat(value))


def local_period(now: datetime) -> LocalPeriod:
    local = now.astimezone()
    iso = local.isocalendar()
    offset = local.utcoffset()
    return (
        local.date(),
        iso.year,
        iso.week,
        None if offset is None else int(offset.total_seconds()),
    )


def period_requires_replay(previous: LocalPeriod, current: LocalPeriod) -> bool:
    """Return whether already-consumed local-day events can become current again."""
    return current[0] < previous[0] or current[3] != previous[3]


def add_to_buckets(
    totals_today: TokenTotals,
    totals_week: TokenTotals,
    ts: datetime,
    delta: TokenTotals,
    now: datetime,
) -> None:
    try:
        local_ts = ts.astimezone()
        local_now = now.astimezone()
    except (OSError, OverflowError, ValueError):
        return
    if local_ts.date() == local_now.date():
        totals_today.add(delta)
    if local_ts.isocalendar()[:2] == local_now.isocalendar()[:2]:
        totals_week.add(delta)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return _scaled_tokens(n, 1_000_000, "M")
    if n >= 1_000:
        return _scaled_tokens(n, 1_000, "k")
    return str(n)


def _scaled_tokens(value: int, divisor: int, suffix: str) -> str:
    try:
        return f"{value / divisor:.1f}{suffix}"
    except OverflowError:
        pass
    tenths, remainder = divmod(value * 10, divisor)
    halfway = remainder * 2 == divisor
    if remainder * 2 > divisor or (halfway and tenths % 2):
        tenths += 1
    whole, decimal = divmod(tenths, 10)
    return f"{whole}.{decimal}{suffix}"


def ensure_aware(ts: datetime) -> datetime:
    if ts.utcoffset() is None:
        return ts.replace(tzinfo=UTC)
    return ts
