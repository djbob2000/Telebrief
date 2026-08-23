"""Tests for legacy Telegram messages import and temporal-fidelity tracking (Plan 5 Task 8)."""

import datetime as dt

import psycopg
import pytest

from scripts.import_legacy_messages import (
    LegacyMessageImporter,
    legacy_source_key,
    parse_telegram_message_id,
)
from src.db.uow import DatabaseUnitOfWork


class TestTelegramLinkParsing:
    """Unit tests for Telegram message link extraction."""

    def test_parse_standard_channel_link(self):
        assert parse_telegram_message_id("https://t.me/berdiansk_news/12345") == "12345"

    def test_parse_private_channel_link(self):
        assert parse_telegram_message_id("https://t.me/c/1234567890/9876") == "9876"

    def test_parse_empty_or_malformed_link(self):
        assert parse_telegram_message_id("") is None
        assert parse_telegram_message_id("not_a_link") is None

    def test_legacy_source_key(self):
        assert legacy_source_key("berdiansk_news") == "@berdiansk_news"
        assert legacy_source_key("@berdiansk_news") == "@berdiansk_news"


@pytest.mark.postgres
class TestLegacyMessageImporterIntegration:
    """Integration test for migrating legacy messages table into multisource schema."""

    async def test_import_preserves_fidelity_and_is_idempotent(
        self, conn: psycopg.AsyncConnection, uow: DatabaseUnitOfWork, edition
    ):
        importer = LegacyMessageImporter(uow=uow)
        now = dt.datetime.now(dt.timezone.utc)
        t_pub = now - dt.timedelta(days=2)
        t_coll = now - dt.timedelta(days=2, minutes=5)

        # 1. Insert 3 legacy rows in `messages` table
        # Row 1: Valid link with exact collected_at
        # Row 2: Valid link with unknown/NULL collected_at
        # Row 3: Malformed link (fallback to legacy-message:id)
        await conn.execute(
            """
            INSERT INTO messages (id, channel_name, sender, text, timestamp, link, collected_at)
            OVERRIDING SYSTEM VALUE
            VALUES
            (101, '@berdiansk_news', 'Admin', 'Первое сообщение', %s, 'https://t.me/berdiansk_news/5001', %s),
            (102, '@berdiansk_news', 'Admin', 'Второе сообщение', %s, 'https://t.me/berdiansk_news/5002', NULL),
            (103, '@berdiansk_chat', 'User', 'Третье сообщение', %s, 'invalid_link', %s)
            """,
            (t_pub, t_coll, t_pub, t_pub, t_coll),
        )
        await conn.commit()

        # 2. Run importer
        report1 = await importer.import_all()

        assert report1.total_scanned == 3
        assert report1.items_created == 3
        assert report1.revisions_created == 3
        assert report1.errors == 0

        # 3. Verify temporal fidelity in DB
        async with uow.transaction() as t_conn:
            # Check row 101 (exact fidelity)
            cur = await t_conn.execute(
                """
                SELECT temporal_fidelity FROM legacy_imported_messages WHERE legacy_message_id = 101
                """
            )
            assert (await cur.fetchone())[0] == "exact"

            # Check row 102 (limited fidelity due to missing collected_at)
            cur = await t_conn.execute(
                """
                SELECT temporal_fidelity FROM legacy_imported_messages WHERE legacy_message_id = 102
                """
            )
            assert (await cur.fetchone())[0] == "limited"

            # Check row 103 (limited fidelity due to missing Telegram ID)
            cur = await t_conn.execute(
                """
                SELECT si.external_id, lim.temporal_fidelity
                FROM legacy_imported_messages lim
                JOIN source_items si ON si.id = lim.source_item_id
                WHERE lim.legacy_message_id = 103
                """
            )
            r103 = await cur.fetchone()
            assert r103[0] == "legacy-message:103"
            assert r103[1] == "limited"

        # 4. Run importer second time (idempotency check)
        report2 = await importer.import_all()

        assert report2.total_scanned == 3
        assert report2.items_created == 0
        assert report2.revisions_created == 0
        assert report2.already_imported == 3
        assert report2.errors == 0
