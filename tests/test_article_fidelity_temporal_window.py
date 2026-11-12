# ruff: noqa: S101
"""article-fidelity W1.1: the reporting window continues where the previous issue ended."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from src.publication.models import PublicationRun
from src.publication.publication_window import (
    WINDOW_METADATA_KEY,
    build_window_spec,
    compute_window_start,
    is_window_chain_type,
    window_spec_from_metadata,
    window_spec_to_metadata,
)
from src.publication.repository import PublicationRepository

UTC = dt.timezone.utc


def _at(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


# Real run times from the spec: run 391 cut at 19:15 UTC, previous run 389 at 21:51:27 UTC
# on the day before. The nominal 24 h lookback would start before run 389 was cut.
RUN_391_CUTOFF = _at(2026, 11, 12, 19, 15)
RUN_389_CUTOFF = _at(2026, 11, 11, 21, 51, 27)


def test_late_previous_run_starts_the_window_at_its_cutoff() -> None:
    start = compute_window_start(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_source_cutoff_at=RUN_389_CUTOFF,
    )

    assert start == RUN_389_CUTOFF


def test_window_does_not_overlap_the_previous_issue() -> None:
    spec = build_window_spec(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_run_id=389,
        previous_source_cutoff_at=RUN_389_CUTOFF,
    )

    assert spec.window_start >= RUN_389_CUTOFF
    assert spec.previous_run_id == 389


def test_without_a_previous_succeeded_run_the_nominal_lookback_applies() -> None:
    start = compute_window_start(source_cutoff_at=RUN_391_CUTOFF, lookback_hours=24)

    assert start == _at(2026, 11, 11, 19, 15)


def test_previous_run_older_than_the_lookback_does_not_extend_the_window() -> None:
    # A gap longer than 24 h: the window stays 24 h, it never grows beyond the lookback.
    old_cutoff = _at(2026, 11, 9, 12)

    start = compute_window_start(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_source_cutoff_at=old_cutoff,
    )

    assert start == _at(2026, 11, 11, 19, 15)
    assert RUN_391_CUTOFF - start == dt.timedelta(hours=24)


def test_previous_cutoff_not_before_the_current_one_is_ignored() -> None:
    start = compute_window_start(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_source_cutoff_at=RUN_391_CUTOFF,
    )

    assert start == _at(2026, 11, 11, 19, 15)


def test_naive_timestamps_are_read_as_utc() -> None:
    spec = build_window_spec(
        source_cutoff_at=RUN_391_CUTOFF.replace(tzinfo=None),
        lookback_hours=24,
        previous_source_cutoff_at=RUN_389_CUTOFF.replace(tzinfo=None),
    )

    assert spec.window_start == RUN_389_CUTOFF


def test_window_metadata_round_trips_for_replay() -> None:
    spec = build_window_spec(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_run_id=389,
        previous_source_cutoff_at=RUN_389_CUTOFF,
    )
    metadata = window_spec_to_metadata(spec)

    assert WINDOW_METADATA_KEY in metadata
    assert window_spec_from_metadata(metadata) == spec


def test_missing_or_damaged_window_metadata_is_not_trusted() -> None:
    assert window_spec_from_metadata({}) is None
    assert window_spec_from_metadata({WINDOW_METADATA_KEY: {"window_start": "x"}}) is None


def test_only_lookback_issues_chain_their_windows() -> None:
    assert is_window_chain_type("daily_article")
    assert is_window_chain_type("digest_grouped")
    assert not is_window_chain_type("monthly_review")
    assert not is_window_chain_type("weekly_article")


class _Cursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def fetchone(self) -> Any:
        return self._row


class _Conn:
    """Fake connection that answers the two window queries and records their SQL."""

    def __init__(self, previous_row: Any, lookback_row: Any = ("24",)) -> None:
        self.previous_row = previous_row
        self.lookback_row = lookback_row
        self.statements: list[str] = []

    async def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.statements.append(sql)
        if "eligibility_policy_versions" in sql:
            return _Cursor(self.lookback_row)
        if sql.lstrip().startswith("UPDATE"):
            return _Cursor(None)
        assert "status = 'succeeded'" in sql
        return _Cursor(self.previous_row)


def _run(*, metadata: dict[str, Any] | None = None) -> PublicationRun:
    return PublicationRun(
        id=391,
        edition_id=7,
        publication_type="daily_article",
        request_key="k",
        snapshot_at=_at(2026, 11, 12, 18, 48),
        eligibility_policy_id=3,
        selection_policy_id=1,
        writer_policy_id=1,
        status="running",
        error_kind=None,
        metadata=metadata or {},
        created_at=_at(2026, 11, 12, 19, 0),
        source_cutoff_at=RUN_391_CUTOFF,
    )


def test_resolve_uses_the_previous_succeeded_issue_and_lookback() -> None:
    conn = _Conn(previous_row=(389, RUN_389_CUTOFF))

    spec = asyncio.run(PublicationRepository().resolve_run_publication_window(conn, _run()))

    assert spec.window_start == RUN_389_CUTOFF
    assert spec.previous_run_id == 389
    assert len(conn.statements) == 2


def test_resolve_falls_back_to_the_lookback_without_a_previous_issue() -> None:
    conn = _Conn(previous_row=None)

    spec = asyncio.run(PublicationRepository().resolve_run_publication_window(conn, _run()))

    assert spec.window_start == _at(2026, 11, 11, 19, 15)
    assert spec.previous_run_id is None


def test_resolve_reuses_the_stored_window_of_a_sealed_run_without_queries() -> None:
    stored = build_window_spec(
        source_cutoff_at=RUN_391_CUTOFF,
        lookback_hours=24,
        previous_run_id=389,
        previous_source_cutoff_at=RUN_389_CUTOFF,
    )
    conn = _Conn(previous_row=None)

    spec = asyncio.run(
        PublicationRepository().resolve_run_publication_window(
            conn, _run(metadata=window_spec_to_metadata(stored))
        )
    )

    assert spec == stored
    assert conn.statements == []


def test_ensure_persists_the_window_once_at_sealing() -> None:
    conn = _Conn(previous_row=(389, RUN_389_CUTOFF))
    run = _run()

    spec = asyncio.run(PublicationRepository().ensure_run_publication_window(conn, run))

    assert spec.window_start == RUN_389_CUTOFF
    assert window_spec_from_metadata(run.metadata) == spec
    assert any(sql.lstrip().startswith("UPDATE publication_runs") for sql in conn.statements)
