"""Tests for idempotent final publication failure notifications."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from src.publication.notification_repository import PublicationNotificationRepository
from src.publication.notifications import PublicationFailureNotificationService
from src.publication.readiness_repository import (
    PublicationRefreshRun,
    PublicationSourceDiagnostic,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 9, 9, 20, tzinfo=UTC)


def _intent(trigger: str, requester: int | None = 123) -> PublicationRefreshRun:
    return PublicationRefreshRun(
        id=1,
        edition_id=2,
        publication_type="digest_grouped",
        slot_at=dt.datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
        requested_at=dt.datetime(2026, 9, 9, 8, 30, tzinfo=UTC),
        normal_source_cutoff_at=dt.datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
        fallback_snapshot_at=dt.datetime(2026, 9, 9, 8, 30, tzinfo=UTC),
        deadline_at=NOW,
        status="failed",
        collection_ready_at=None,
        processing_ready_at=None,
        prepared_at=None,
        publication_run_id=None,
        fallback_used=False,
        error_kind="readiness_deadline",
        metadata={},
        trigger=trigger,
        request_key=f"{trigger}:test",
        freshness_cutoff_at=dt.datetime(2026, 9, 9, 8, 30, tzinfo=UTC),
        requested_by_user_id=requester,
    )


def test_notification_recipients_are_trigger_specific(sample_config):
    config = replace(
        sample_config, settings=replace(sample_config.settings, admin_user_ids=[123, 456, 456])
    )
    service = PublicationFailureNotificationService(config=config)
    assert service.recipients(_intent("manual", requester=77)) == [77]
    assert service.recipients(_intent("manual", requester=None)) == [123, 456]
    assert service.recipients(_intent("scheduled", requester=77)) == [123, 456]


def test_notification_message_is_explicit_and_safe(sample_config):
    service = PublicationFailureNotificationService(config=sample_config)
    message = service.render_message(
        _intent("scheduled"),
        [
            PublicationSourceDiagnostic(
                source_id=55,
                status="degraded",
                collection_outcome="auth_required",
                collection_run_id=9,
                backoff_until=None,
                retryable=False,
            )
        ],
    )
    assert "not published" in message
    assert "digest_grouped" in message
    assert "auth_required" in message
    assert "source 55" in message
    assert "No stale fallback data was used" in message
    assert "traceback" not in message.lower()


@pytest.mark.postgres
async def test_notification_rows_are_idempotent(conn, edition):
    repo = PublicationNotificationRepository()
    cursor = await conn.execute(
        """
        INSERT INTO publication_refresh_runs (
            edition_id, publication_type, slot_at, requested_at,
            normal_source_cutoff_at, fallback_snapshot_at, deadline_at,
            status, trigger, request_key, freshness_cutoff_at
        ) VALUES (%s, 'digest_grouped', %s, %s, %s, %s, %s,
                  'failed', 'manual', 'manual:notification-test', %s)
        RETURNING id
        """,
        (
            edition.id,
            NOW - dt.timedelta(minutes=20),
            NOW - dt.timedelta(minutes=50),
            NOW - dt.timedelta(minutes=20),
            NOW - dt.timedelta(minutes=50),
            NOW,
            NOW - dt.timedelta(minutes=50),
        ),
    )
    refresh_run_id = int((await cursor.fetchone())[0])
    first = await repo.insert_new(
        conn,
        refresh_run_id=refresh_run_id,
        recipient_user_ids=[123, 456, 123],
        failure_kind="readiness_deadline",
    )
    second = await repo.insert_new(
        conn,
        refresh_run_id=refresh_run_id,
        recipient_user_ids=[123, 456],
        failure_kind="readiness_deadline",
    )
    assert len(first) == 2
    assert second == []
