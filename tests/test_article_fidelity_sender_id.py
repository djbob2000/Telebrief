# ruff: noqa: S101
"""W3.1: the Telegram sender id is stored as the observation's author identity."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.ingestion.models import ObservedItem
from src.ingestion.repository import IngestionRepository
from src.providers.telegram import sender_external_id


def test_sender_user_id_becomes_the_author_identity_as_text():
    message = SimpleNamespace(sender_id=918273645)

    assert sender_external_id(message) == "918273645"


def test_channel_post_without_sender_id_has_no_author_identity():
    assert sender_external_id(SimpleNamespace(sender_id=None)) is None


def test_mock_or_boolean_sender_ids_are_not_treated_as_platform_ids():
    assert sender_external_id(MagicMock()) is None
    assert sender_external_id(SimpleNamespace(sender_id=True)) is None
    assert sender_external_id(SimpleNamespace(sender_id="918273645")) is None


def test_observation_without_author_id_keeps_the_previous_default():
    observation = ObservedItem(
        kind="telegram_message",
        external_id="1",
        text="Свет дали",
        author_name="Анна",
        published_at=None,
        canonical_url=None,
        metadata={},
        observed_at=dt.datetime(2026, 11, 12, tzinfo=dt.timezone.utc),
    )

    assert observation.author_external_id is None


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    """Records SQL parameters and returns the inserted row."""

    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.params: list[tuple[object, ...]] = []

    async def execute(self, sql: str, params: tuple[object, ...] = ()) -> _Cursor:
        if "INSERT INTO source_items" in sql:
            self.params.append(params)
            return _Cursor(self.row)
        return _Cursor(None)


def test_insert_writes_the_author_id_into_source_items():
    observed_at = dt.datetime(2026, 11, 12, 10, 0, tzinfo=dt.timezone.utc)
    row = (
        11,
        7,
        "telegram_message",
        "1255768",
        None,
        None,
        "Анна",
        "918273645",
        "https://t.me/example/1255768",
        None,
        observed_at,
        {},
    )
    conn = _Connection(row)
    observation = ObservedItem(
        kind="telegram_message",
        external_id="1255768",
        text="Свет дали",
        author_name="Анна",
        published_at=None,
        canonical_url="https://t.me/example/1255768",
        metadata={},
        observed_at=observed_at,
        author_external_id="918273645",
    )

    item, created = asyncio.run(
        IngestionRepository().get_or_create_item_shell(conn, 7, observation)  # type: ignore[arg-type]
    )

    assert created is True
    assert item.author_external_id == "918273645"
    assert conn.params[0][-1] == "918273645"
