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
        from pathlib import Path

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        from telegram.error import TelegramError, TimedOut

        from src.utils import split_message

        text = str(payload.rendered_content.get("text", "")).strip()
        if not text:
            raise ValueError(f"telegram payload {payload.id} has no text to deliver")

        bot = self._get_bot()

        # Check for inline keyboard button (e.g. Telegraph link)
        reply_markup = None
        inline_btn = payload.rendered_content.get("inline_button")
        if inline_btn and isinstance(inline_btn, dict):
            btn_text = inline_btn.get("text")
            btn_url = inline_btn.get("url")
            if btn_text and btn_url:
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(text=btn_text, url=btn_url)]]
                )

        parse_mode_str = str(payload.rendered_content.get("parse_mode", "HTML")).upper()
        parse_mode = ParseMode.MARKDOWN if parse_mode_str == "MARKDOWN" else ParseMode.HTML

        # 1. Check if photo post is requested and photo file exists on disk
        photo_path_raw = payload.rendered_content.get("photo_path")
        if photo_path_raw:
            photo_path = Path(photo_path_raw)
            if photo_path.exists():
                try:
                    with open(photo_path, "rb") as photo_file:
                        message = await bot.send_photo(
                            chat_id=destination.destination_key,
                            photo=photo_file,
                            caption=text,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                        )
                    return {
                        "external_message_id": str(message.message_id),
                        "status": "sent",
                    }
                except (TimedOut, asyncio.TimeoutError) as exc:
                    raise TimeoutError(f"telegram photo send timed out: {exc}") from exc
                except TelegramError as exc:
                    if "Can't parse entities" in str(exc):
                        logger.warning(
                            "Entity parse error delivering photo payload %s to %s; retrying with plain text caption",
                            payload.id,
                            destination.destination_key,
                        )
                        try:
                            with open(photo_path, "rb") as photo_file:
                                message = await bot.send_photo(
                                    chat_id=destination.destination_key,
                                    photo=photo_file,
                                    caption=text,
                                    parse_mode=None,
                                    reply_markup=reply_markup,
                                )
                            return {
                                "external_message_id": str(message.message_id),
                                "status": "sent",
                            }
                        except (TimedOut, asyncio.TimeoutError) as timeout_exc:
                            raise TimeoutError(
                                f"telegram photo send timed out: {timeout_exc}"
                            ) from timeout_exc
                    else:
                        logger.warning(
                            "Failed to send photo post (%s); falling back to text delivery", exc
                        )

        # 2. Text message delivery (single part or multi-part split)
        parts = split_message(text, max_length=4000)
        sent_ids: list[str] = []

        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            markup = reply_markup if is_last else None
            try:
                message = await bot.send_message(
                    chat_id=destination.destination_key,
                    text=part,
                    parse_mode=parse_mode,
                    disable_web_page_preview=False if reply_markup else True,
                    reply_markup=markup,
                )
            except (TimedOut, asyncio.TimeoutError) as exc:
                raise TimeoutError(f"telegram send timed out: {exc}") from exc
            except TelegramError as exc:
                if "Can't parse entities" in str(exc):
                    logger.warning(
                        "HTML entity parse error delivering payload %s to %s; retrying with plain text",
                        payload.id,
                        destination.destination_key,
                    )
                    try:
                        message = await bot.send_message(
                            chat_id=destination.destination_key,
                            text=part,
                            parse_mode=None,
                            disable_web_page_preview=False if reply_markup else True,
                            reply_markup=markup,
                        )
                    except (TimedOut, asyncio.TimeoutError) as timeout_exc:
                        raise TimeoutError(
                            f"telegram send timed out: {timeout_exc}"
                        ) from timeout_exc
                else:
                    raise
            sent_ids.append(str(message.message_id))

        return {
            "external_message_id": ",".join(sent_ids),
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
