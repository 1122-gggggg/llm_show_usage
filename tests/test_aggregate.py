from datetime import UTC, datetime, timedelta, timezone

from llm_usage_monitor.aggregate import fmt_tokens, parse_iso


def test_parse_iso_assumes_utc_when_offset_is_missing() -> None:
    assert parse_iso("2026-08-23T12:34:56") == datetime(
        2026, 8, 23, 12, 34, 56, tzinfo=UTC
    )


def test_parse_iso_preserves_explicit_offset() -> None:
    offset = timezone(timedelta(hours=8))

    assert parse_iso("2026-08-23T12:34:56+08:00") == datetime(
        2026, 8, 23, 12, 34, 56, tzinfo=offset
    )


def test_fmt_tokens_rounds_without_float_overflow() -> None:
    assert fmt_tokens(1_949) == "1.9k"
    assert fmt_tokens(1_950) == "1.9k"
    assert fmt_tokens(1_999) == "2.0k"
    assert fmt_tokens(1_999_999) == "2.0M"
    assert fmt_tokens(10**4000).endswith(".0M")
