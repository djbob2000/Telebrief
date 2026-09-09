"""Operational helpers for the Procrastinate PostgreSQL namespace.

Telebrief owns only the namespace itself; the official Procrastinate schema is
the sole owner of every table and function inside it.
"""

from __future__ import annotations

import datetime as dt

import procrastinate
import psycopg
from procrastinate import PsycopgConnector
from procrastinate.schema import SchemaManager
from psycopg import sql
from telegram import Bot

from src.jobs.app import procrastinate_app

DEFAULT_SCHEMA = "procrastinate"

PUBLICATION_FAILURE_NOTIFICATION_TASK_NAME = "send_publication_failure_notification"
PUBLICATION_NOTIFICATION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3, wait=30, linear_wait=60
)


@procrastinate_app.task(
    name=PUBLICATION_FAILURE_NOTIFICATION_TASK_NAME,
    queue="maintenance",
    retry=PUBLICATION_NOTIFICATION_RETRY_STRATEGY,
)
async def send_publication_failure_notification(
    notification_id: int, message: str | None = None
) -> None:
    """Send one final failure notification, preserving failed intent state."""
    from src.config_loader import load_config
    from src.publication.notification_repository import PublicationNotificationRepository
    from src.publication.notifications import PublicationFailureNotificationService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    notification_repo = PublicationNotificationRepository()
    readiness_repo = PublicationReadinessRepository()
    config = load_config()
    async with runtime.uow.transaction() as conn:
        notification = await notification_repo.get(conn, notification_id, for_update=True)
        if notification is None or notification.status == "sent":
            return
        intent = await readiness_repo.get_refresh_run(conn, notification.refresh_run_id)
        if intent is None:
            raise ValueError(f"publication intent {notification.refresh_run_id} not found")
        if message is None:
            diagnostics = await readiness_repo.list_unready_source_diagnostics(conn, intent.id)
            message = PublicationFailureNotificationService(config=config).render_message(
                intent, diagnostics
            )
        recipient_user_id = notification.recipient_user_id

    try:
        async with Bot(token=config.telegram_bot_token) as bot:
            await bot.send_message(
                chat_id=recipient_user_id,
                text=message,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        async with runtime.uow.transaction() as conn:
            await notification_repo.mark_failed(
                conn,
                notification_id=notification_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise

    async with runtime.uow.transaction() as conn:
        await notification_repo.mark_sent(
            conn,
            notification_id=notification_id,
            sent_at=dt.datetime.now(dt.timezone.utc),
        )


async def ensure_schema(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Create the Procrastinate namespace if missing.

    The schema name is bound through ``psycopg.sql.Identifier`` so configured
    identifiers never reach SQL as interpolated text.
    """
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name))
        )


async def official_tables_exist(database_url: str, schema_name: str = DEFAULT_SCHEMA) -> bool:
    """Return True when the official job table exists inside ``schema_name``."""
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        cursor = await conn.execute(
            "SELECT to_regclass(%s)", (f"{schema_name}.procrastinate_jobs",)
        )
        row = await cursor.fetchone()
        return row is not None and row[0] is not None


async def apply_official_schema(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Apply the official Procrastinate schema into ``schema_name``.

    Uses procrastinate's own SchemaManager over a connector whose search path
    targets the namespace, exactly like ``procrastinate schema --apply`` does.
    """
    connector = PsycopgConnector(
        conninfo=database_url,
        kwargs={"options": f"-c search_path={schema_name}"},
    )
    await connector.open_async()
    try:
        await SchemaManager(connector).apply_schema_async()
    finally:
        await connector.close_async()


async def ensure_official_tables(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Idempotently make sure the official tables exist in ``schema_name``."""
    await ensure_schema(database_url, schema_name)
    if not await official_tables_exist(database_url, schema_name):
        await apply_official_schema(database_url, schema_name)
