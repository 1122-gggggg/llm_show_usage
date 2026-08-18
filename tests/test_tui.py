from datetime import UTC, datetime

from llm_usage_monitor.model import QuotaWindow
from llm_usage_monitor.tui import _quota_cell, remaining_percent


def test_remaining_percent_inverts_used() -> None:
    assert remaining_percent(0) == 100
    assert remaining_percent(97) == 3
    assert remaining_percent(100) == 0


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
