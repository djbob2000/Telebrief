# ruff: noqa: S101
"""The Gate cost report is read-only and its arithmetic is exact."""

from __future__ import annotations

import re

from scripts.gate_cost_report import DAILY_QUERY, DailyRow, format_report, row_from_db


def test_daily_query_has_no_write_statements():
    sql_without_literals = re.sub(r"'[^']*'", "''", DAILY_QUERY)
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE)\b", sql_without_literals
    )


def test_row_from_db_maps_columns_in_order():
    row = row_from_db(("2026-10-18", 10, 3, 2, 500, 1, 9, 4, 4, 1))
    assert row == DailyRow("2026-10-18", 10, 3, 2, 500, 1, 9, 4, 4, 1)
    assert row.failed_share == 0.3


def test_report_totals_and_failed_share():
    rows = [
        DailyRow("2026-10-17", 10, 4, 0, 0, 0, 0, 0, 0, 0),
        DailyRow("2026-10-18", 30, 6, 1, 0, 2, 0, 0, 0, 0),
    ]
    text = format_report(rows)
    assert "total: 40 calls, 10 failed (25.0%)" in text
    assert "2026-10-17" in text


def test_empty_report_does_not_divide_by_zero():
    assert "total: 0 calls, 0 failed (0.0%)" in format_report([])
