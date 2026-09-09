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
            return [intent.requested_by_user_id] if intent.requested_by_user_id is not None else []
        return list(dict.fromkeys(self.config.settings.admin_user_ids))

    def render_message(
        self,
        intent: PublicationRefreshRun,
        diagnostics: Sequence[PublicationSourceDiagnostic],
    ) -> str:
        source_lines = [
            f"- source {diagnostic.source_id}: {diagnostic.collection_outcome or 'no completed scan'}"
            for diagnostic in diagnostics
        ]
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
        from src.jobs.admin import send_publication_failure_notification

        message = self.render_message(intent, diagnostics)
        for notification_id in ids:
            await send_publication_failure_notification.configure(
                connection=conn,
                queueing_lock=f"publication-failure-notification:{notification_id}",
            ).defer_async(notification_id=notification_id, message=message)
        return ids
