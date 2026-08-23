"""One-time idempotent legacy Telegram messages importer (Plan 5 Task 8)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from src.config_loader import load_database_config
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_TG_LINK_REGEX = re.compile(r"https?://t\.me/(?:c/\d+/|[\w_]+/)?(\d+)")


def parse_telegram_message_id(link: str) -> str | None:
    """Extract message ID integer string from Telegram message permalink."""
    if not link:
        return None
    match = _TG_LINK_REGEX.search(link.strip())
    if match:
        return match.group(1)
    return None


def legacy_source_key(channel_name: str) -> str:
    """Normalize channel identifier to standard external_id."""
    clean = channel_name.strip()
    if not clean.startswith("@") and not clean.startswith("http"):
        clean = f"@{clean}"
    return clean


@dataclass
class ImportReport:
    """Summary report of legacy message migration."""

    total_scanned: int = 0
    items_created: int = 0
    revisions_created: int = 0
    already_imported: int = 0
    errors: int = 0


class LegacyMessageImporter:
    """Reads legacy messages and migrates them into generic source items and revisions."""

    def __init__(self, uow: DatabaseUnitOfWork) -> None:
        self.uow = uow

    async def import_all(
        self,
        *,
        batch_size: int = 500,
        dry_run: bool = False,
        max_messages: int | None = None,
    ) -> ImportReport:
        report = ImportReport()
        last_id = 0

        while True:
            limit = batch_size
            if max_messages is not None:
                remaining = max_messages - report.total_scanned
                if remaining <= 0:
                    break
                limit = min(limit, remaining)

            async with self.uow.transaction() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, channel_name, sender, text, timestamp, link, has_media, media_type, collected_at
                    FROM messages
                    WHERE id > %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (last_id, limit),
                )
                rows = await cursor.fetchall()

            if not rows:
                break

            for row in rows:
                last_id = row[0]
                report.total_scanned += 1

                msg_id = row[0]
                channel_name = row[1]
                sender = row[2]
                text = row[3]
                published_at = row[4]
                link = row[5]
                collected_at = row[8]

                # Check if already imported
                async with self.uow.transaction() as conn:
                    cur = await conn.execute(
                        "SELECT id FROM legacy_imported_messages WHERE legacy_message_id = %s",
                        (msg_id,),
                    )
                    if await cur.fetchone() is not None:
                        report.already_imported += 1
                        continue

                if dry_run:
                    report.items_created += 1
                    report.revisions_created += 1
                    continue

                try:
                    await self._import_single_message(
                        msg_id=msg_id,
                        channel_name=channel_name,
                        sender=sender,
                        text=text,
                        published_at=published_at,
                        link=link,
                        collected_at=collected_at,
                        report=report,
                    )
                except Exception as e:
                    logger.warning("Error importing legacy message %s: %s", msg_id, e)
                    report.errors += 1

        return report

    async def _import_single_message(
        self,
        *,
        msg_id: int,
        channel_name: str,
        sender: str,
        text: str,
        published_at: dt.datetime,
        link: str,
        collected_at: dt.datetime | None,
        report: ImportReport,
    ) -> None:
        source_key = legacy_source_key(channel_name)
        extracted_msg_id = parse_telegram_message_id(link)
        if extracted_msg_id is not None:
            external_id = f"msg:{extracted_msg_id}"
            temporal_fidelity = "exact"
        else:
            external_id = f"legacy-message:{msg_id}"
            temporal_fidelity = "limited"

        # Check collected_at trustworthiness
        trustworthy_collected_at = collected_at
        now = dt.datetime.now(dt.timezone.utc)
        if collected_at is None or collected_at > now + dt.timedelta(days=1):
            trustworthy_collected_at = now
            temporal_fidelity = "limited"

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        async with self.uow.transaction() as conn:
            # 1. Get or create source
            cur = await conn.execute(
                """
                INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
                VALUES ('telegram', 'channel', %s, %s, %s, 'local_media', true)
                ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (source_key, f"https://t.me/{source_key.lstrip('@')}", source_key),
            )
            source_id = (await cur.fetchone())[0]

            # 2. Get or create source item
            cur = await conn.execute(
                """
                INSERT INTO source_items (source_id, kind, external_id, published_at, first_collected_at)
                VALUES (%s, 'channel_message', %s, %s, %s)
                ON CONFLICT (source_id, external_id) DO UPDATE SET published_at = COALESCE(source_items.published_at, EXCLUDED.published_at)
                RETURNING id, (xmax = 0) AS is_new
                """,
                (source_id, external_id, published_at, trustworthy_collected_at),
            )
            item_row = await cur.fetchone()
            item_id = item_row[0]
            is_new_item = item_row[1]

            if is_new_item:
                report.items_created += 1

            # 3. Insert revision
            cur = await conn.execute(
                """
                SELECT COALESCE(MAX(revision_no), 0) + 1 FROM source_item_revisions WHERE source_item_id = %s
                """,
                (item_id,),
            )
            next_rev_no = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_item_revisions (
                    source_item_id, revision_no, text_content, collected_at, content_hash, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    item_id,
                    next_rev_no,
                    text,
                    trustworthy_collected_at,
                    content_hash,
                    f'{{"sender": "{sender}", "legacy_link": "{link}", "legacy_id": {msg_id}}}',
                ),
            )
            rev_id = (await cur.fetchone())[0]
            report.revisions_created += 1

            # 4. Record migration tracking
            await conn.execute(
                """
                INSERT INTO legacy_imported_messages (
                    legacy_message_id, source_item_id, source_item_revision_id, temporal_fidelity, imported_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (msg_id, item_id, rev_id, temporal_fidelity, now),
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy messages into multisource schema.")
    parser.add_argument("--dry-run", action="store_true", help="Perform dry run without database writes")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size for processing")
    parser.add_argument("--limit", type=int, default=None, help="Max messages to import")
    args = parser.parse_args()

    db_config = load_database_config(require_enabled=True)
    pool = await open_pool(db_config)
    try:
        uow = DatabaseUnitOfWork(pool)
        importer = LegacyMessageImporter(uow=uow)

        logger.info("Starting legacy messages import (dry_run=%s)...", args.dry_run)
        report = await importer.import_all(
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            max_messages=args.limit,
        )
        logger.info(
            "Legacy import finished: scanned=%d created_items=%d created_revisions=%d already_imported=%d errors=%d",
            report.total_scanned,
            report.items_created,
            report.revisions_created,
            report.already_imported,
            report.errors,
        )
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
