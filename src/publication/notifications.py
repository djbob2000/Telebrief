"""Final publication-intent failure notification service."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from src.config_loader import Config
from src.publication.notification_repository import PublicationNotificationRepository
from src.publication.readiness_repository import (
    PublicationReadinessRepository,
    PublicationRefreshRun,
    PublicationSourceDiagnostic,
)


class PublicationFailureNotificationService:
    """Render and enqueue one safe notification per failed intent recipient."""

    def __init__(
        self,
        *,
        config: Config,
        repo: PublicationNotificationRepository | None = None,
        readiness_repo: PublicationReadinessRepository | None = None,
    ) -> None:
        self.config = config
        self.repo = repo or PublicationNotificationRepository()
        self.readiness_repo = readiness_repo or PublicationReadinessRepository()

    def recipients(self, intent: PublicationRefreshRun) -> list[int]:
        if intent.trigger == "manual":
            if intent.requested_by_user_id is not None:
                return [intent.requested_by_user_id]
            return list(dict.fromkeys(self.config.settings.admin_user_ids))
        return list(dict.fromkeys(self.config.settings.admin_user_ids))

    def render_message(
        self,
        intent: PublicationRefreshRun,
        diagnostics: Sequence[PublicationSourceDiagnostic],
    ) -> str:
        source_lines = []
        for diagnostic in diagnostics:
            if diagnostic.stage == "preparation":
                source_lines.append("- preparation: publication snapshot preparation failed")
                continue
            label = f"source {diagnostic.source_id}"
            if diagnostic.source_name:
                label += f" ({diagnostic.source_name})"
            if diagnostic.stage == "event_processing":
                source_lines.append(
                    f"- {label}: event processing pending; "
                    f"unprocessed revisions={diagnostic.unprocessed_revision_count}, "
                    f"pending={diagnostic.pending_revision_count}, "
                    f"failed={diagnostic.failed_revision_count}"
                )
            else:
                source_lines.append(
                    f"- {label}: {diagnostic.collection_outcome or 'no completed scan'}"
                )
        sources = (
            "\n".join(source_lines)
            if source_lines
            else "- no enabled source completed a qualifying scan"
        )
        return (
            "❌ Publication not published.\n"
            f"Type: {intent.publication_type}; target: {intent.slot_at.isoformat()}\n"
            f"Reason: {intent.error_kind or 'readiness failure'}\n"
            "Problematic sources:\n"
            f"{sources}\n"
            "No stale fallback data was used."
        )

    async def enqueue_for_failed_intent(
        self,
        conn: psycopg.AsyncConnection,
        *,
        intent: PublicationRefreshRun,
        failure_kind: str | None = None,
        dispatch: bool = True,
    ) -> list[int]:
        diagnostics = await self.readiness_repo.list_unready_source_diagnostics(conn, intent.id)
        ids = await self.repo.insert_new(
            conn,
            refresh_run_id=intent.id,
            recipient_user_ids=self.recipients(intent),
            failure_kind=failure_kind or intent.error_kind or "readiness_failure",
        )
        if not ids:
            return []
        if dispatch:
            await self.dispatch_existing(
                conn,
                intent=intent,
                notification_ids=ids,
                diagnostics=diagnostics,
            )
        return ids

    async def dispatch_existing(
        self,
        conn: psycopg.AsyncConnection,
        *,
        intent: PublicationRefreshRun,
        notification_ids: Sequence[int],
        diagnostics: Sequence[PublicationSourceDiagnostic] | None = None,
    ) -> None:
        """Queue already-committed outbox rows without changing their durability."""
        if not notification_ids:
            return
        from src.jobs.admin import send_publication_failure_notification

        if diagnostics is None:
            diagnostics = await self.readiness_repo.list_unready_source_diagnostics(conn, intent.id)
        message = self.render_message(intent, diagnostics)
        for notification_id in notification_ids:
            await send_publication_failure_notification.configure(
                connection=conn,
                queueing_lock=f"publication-failure-notification:{notification_id}",
            ).defer_async(notification_id=notification_id, message=message)

    async def redrive_pending(self, *, limit: int = 100) -> list[int]:
        """Queue pending/failed outbox rows; rows remain durable if queueing fails."""
        from src.runtime import get_runtime

        runtime = get_runtime()
        queued: list[int] = []
        async with runtime.uow.transaction() as conn:
            ids = await self.repo.list_dispatchable(conn, limit=limit)
            for notification_id in ids:
                notification = await self.repo.get(conn, notification_id)
                if notification is None or notification.status == "sent":
                    continue
                intent = await self.readiness_repo.get_refresh_run(
                    conn, notification.refresh_run_id
                )
                if intent is None:
                    continue
                await self.dispatch_existing(
                    conn,
                    intent=intent,
                    notification_ids=[notification_id],
                )
                queued.append(notification_id)
        return queued
