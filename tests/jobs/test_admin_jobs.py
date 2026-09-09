"""Tests for durable publication failure notification delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_sent_failure_notification_is_a_noop(monkeypatch):
    from src import runtime
    from src.jobs.admin import send_publication_failure_notification
    from src.publication.notification_repository import PublicationNotificationRepository

    conn = AsyncMock()
    uow = MagicMock()
    uow.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    uow.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    runtime._runtime = SimpleNamespace(uow=uow)

    monkeypatch.setattr(
        PublicationNotificationRepository,
        "get",
        AsyncMock(
            return_value=SimpleNamespace(
                id=7,
                refresh_run_id=11,
                recipient_user_id=123,
                failure_kind="readiness_deadline",
                status="sent",
            )
        ),
    )

    await send_publication_failure_notification.func(7)

    assert uow.transaction.call_count == 1
