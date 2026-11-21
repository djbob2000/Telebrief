"""Telegram photo posts retry once when Telegram fails to process the image.

``Image_process_failed`` has been seen on a valid 2048x1152 JPEG. A single retry
with the same file usually succeeds; a second failure falls back to text delivery.
"""

# ruff: noqa: S101
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import TelegramError

from src.publication import adapters

_PLACEHOLDER_TOKEN = "000:placeholder"  # noqa: S105 - never used; the bot is faked


class _FakeBot:
    def __init__(self, photo_results: list[object]) -> None:
        self._photo_results = list(photo_results)
        self.photo_calls = 0
        self.text_calls = 0

    async def send_photo(self, **_kwargs):
        self.photo_calls += 1
        result = self._photo_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(message_id=result)

    async def send_message(self, **_kwargs):
        self.text_calls += 1
        return SimpleNamespace(message_id=900)


def _deliver(bot: _FakeBot, photo_path) -> dict:
    client = adapters.TelegramChannelDestinationClient(bot_token=_PLACEHOLDER_TOKEN)
    client._get_bot = lambda: bot  # type: ignore[method-assign]
    destination = SimpleNamespace(destination_key="@channel")
    payload = SimpleNamespace(
        id=1,
        rendered_content={"text": "Статья", "photo_path": str(photo_path), "parse_mode": "HTML"},
    )
    return asyncio.run(client.send_payload(destination=destination, payload=payload))


def test_image_process_failure_is_retried_once_and_succeeds(tmp_path):
    photo = tmp_path / "cover.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    bot = _FakeBot([TelegramError("Image_process_failed"), 77])

    result = _deliver(bot, photo)

    assert result == {"external_message_id": "77", "status": "sent"}
    assert bot.photo_calls == 2
    assert bot.text_calls == 0


def test_second_image_failure_falls_back_to_text(tmp_path):
    photo = tmp_path / "cover.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    bot = _FakeBot([TelegramError("Image_process_failed"), TelegramError("Image_process_failed")])

    result = _deliver(bot, photo)

    assert result["status"] == "sent"
    assert result["external_message_id"] == "900"
    assert bot.photo_calls == 2
    assert bot.text_calls == 1


def test_other_photo_errors_are_not_retried(tmp_path):
    photo = tmp_path / "cover.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    bot = _FakeBot([TelegramError("Bad Request: wrong file identifier")])

    result = _deliver(bot, photo)

    assert result["status"] == "sent"
    assert bot.photo_calls == 1
    assert bot.text_calls == 1
