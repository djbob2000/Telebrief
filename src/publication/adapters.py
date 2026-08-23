"""Destination adapters that wrap existing Telegram/Telegraph senders (Plan 4 Task 7).

Adapters only transport an immutable payload to a remote platform; they never
generate or mutate content. ``reconcile_outcome`` returns ``succeeded``,
``not_delivered``, or ``unknown`` per the plan contract: automatic resend is
allowed only after a definitive ``not_delivered``; both bundled platforms
cannot reliably prove the fate of a timed-out send, so they answer
``unknown`` and the delivery service keeps the delivery in
``outcome_unknown`` for manual resolution instead of risking duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.publication.delivery import DestinationClient, MockDestinationClient
from src.publication.models import (
    DeliveryDestination,
    PublicationDeliveryAttempt,
    PublicationDeliveryPayload,
)

logger = logging.getLogger(__name__)

MOCK_DESTINATIONS_ENV = "TELEBRIEF_MOCK_DESTINATIONS"


def _mock_requested() -> bool:
    return os.getenv(MOCK_DESTINATIONS_ENV, "").strip().lower() in ("1", "true", "yes")


class TelegramChannelDestinationClient(DestinationClient):
    """Deliver immutable payloads to a Telegram channel/chat via the Bot API."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._bot: Any | None = None

    def _get_bot(self) -> Any:
        if self._bot is None:
            from telegram import Bot

            self._bot = Bot(token=self._bot_token)
        return self._bot

    async def send_payload(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
    ) -> dict[str, Any]:
        from telegram.constants import ParseMode
        from telegram.error import TimedOut

        text = str(payload.rendered_content.get("text", ""))
        if not text:
            raise ValueError(f"telegram payload {payload.id} has no text to deliver")
        try:
            message = await self._get_bot().send_message(
                chat_id=destination.destination_key,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TimedOut as exc:
            raise TimeoutError(f"telegram send timed out: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"telegram send timed out: {exc}") from exc
        return {
            "external_message_id": str(message.message_id),
            "status": "sent",
        }

    async def reconcile_outcome(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
        last_attempt: PublicationDeliveryAttempt,
    ) -> str:
        # The Bot API offers no way to look up whether a timed-out
        # sendMessage actually created a message (no idempotency keys),
        # so the honest answer is always "unknown".
        return "unknown"


class TelegraphDestinationClient(DestinationClient):
    """Publish immutable payloads as Telegra.ph pages."""

    def __init__(self) -> None:
        from src.telegraph import TelegraphPublisher

        self._publisher = TelegraphPublisher()

    async def send_payload(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
    ) -> dict[str, Any]:
        title = str(payload.rendered_content.get("title", "")).strip()
        body = str(payload.rendered_content.get("body_markdown", "")).strip()
        if not title or not body:
            raise ValueError(f"telegraph payload {payload.id} needs title and body_markdown")
        url = await self._publisher.create_page(title=title, content_markdown=body)
        return {"external_message_id": url, "status": "sent"}

    async def reconcile_outcome(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
        last_attempt: PublicationDeliveryAttempt,
    ) -> str:
        # A timed-out createPage may have created a page whose URL we never
        # received; without the URL there is nothing reliable to check.
        return "unknown"


def build_default_clients() -> dict[str, DestinationClient]:
    """Build production destination clients from the process environment.

    Real adapters are the default. Set ``TELEBRIEF_MOCK_DESTINATIONS=1`` to
    opt into fabricated deliveries (offline development/tests only).
    """
    if _mock_requested():
        logger.warning(
            "%s is set: deliveries will be fabricated by %s",
            MOCK_DESTINATIONS_ENV,
            MockDestinationClient.__name__,
        )
        return {
            "telegram_channel": MockDestinationClient(),
            "telegraph": MockDestinationClient(),
        }
    clients: dict[str, DestinationClient] = {}
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        clients["telegram_channel"] = TelegramChannelDestinationClient(bot_token=token)
    clients["telegraph"] = TelegraphDestinationClient()
    return clients
