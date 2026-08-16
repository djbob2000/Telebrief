"""
Telegram bot sender for delivering digests.
"""

import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from src.config_loader import Config
from src.formatter import DigestFormatter
from src.utils import (
    TELEGRAM_MAX_MESSAGE_CHARS,
    TELEGRAM_SAFE_MESSAGE_CHARS,
    clear_digest_message_ids,
    get_digest_message_ids,
    save_digest_message_ids,
    split_message,
)


class DigestSender:
    """Sends digests via Telegram bot."""

    def __init__(self, config: Config, logger: logging.Logger):
        """
        Initialize sender.

        Args:
            config: Application configuration
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.bot = Bot(token=config.telegram_bot_token)
        self.target_user_id = config.settings.target_user_id
        self.target_chat_id = config.settings.target_chat_id or self.target_user_id
        self.formatter = DigestFormatter(config, logger)

    async def _send_message_part(self, chat_id: str | int, text: str, part_num: int) -> None:
        """
        Send a single message part with markdown fallback.

        Args:
            chat_id: Telegram destination chat ID
            text: Message text
            part_num: Part number for logging

        Raises:
            TelegramError: If sending fails (not markdown parsing)
        """
        try:
            # Try with Markdown first
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            # If markdown parsing fails, try plain text
            if "Can't parse entities" in str(e):
                self.logger.warning(
                    f"Markdown parse error in part {part_num}, falling back to plain text"
                )
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
            else:
                raise

    async def send_digest(self, digest: str, user_id: Optional[int] = None) -> bool:
        """
        Send digest to user.

        Args:
            digest: Formatted digest text
            user_id: Target user ID (defaults to configured user)

        Returns:
            True if successful, False otherwise
        """
        if user_id is None:
            user_id = self.target_user_id

        # Verify authorized user
        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        self.logger.info(f"Sending digest to user {user_id}")

        try:
            # Split message if too long
            parts = split_message(digest, max_length=TELEGRAM_SAFE_MESSAGE_CHARS)

            if len(parts) > 1:
                self.logger.info(f"Digest split into {len(parts)} messages")

            # Send each part
            for i, part in enumerate(parts, 1):
                await self._send_message_part(self.target_chat_id, part, i)

                if len(parts) > 1:
                    self.logger.info(f"Sent part {i}/{len(parts)}")

            self.logger.info("✅ Digest sent successfully")
            return True

        except TelegramError as e:
            self.logger.error(f"❌ Failed to send digest: {e}")
            return False

    async def _send_rich_message(self, chat_id: str | int, rich_message: dict) -> int:
        """Send a structured message through Telegram's Rich Messages API."""
        response = await self.bot._post(
            "sendRichMessage",
            data={
                "chat_id": chat_id,
                "rich_message": rich_message,
            },
        )
        if not isinstance(response, dict) or not response.get("message_id"):
            raise TelegramError("Rich Message API returned no message_id")
        return int(response["message_id"])

    @staticmethod
    def _rich_message_to_plain_text(rich_message: dict) -> str:
        """Create a readable fallback from a Rich Message document."""
        lines: list[str] = []
        for block in rich_message.get("blocks", []):
            block_type = block.get("type")
            if block_type == "heading":
                lines.append(str(block.get("text", "")))
            elif block_type == "list":
                for item in block.get("items", []):
                    for item_block in item.get("blocks", []):
                        text = item_block.get("text", "")
                        if isinstance(text, list):
                            text = "".join(
                                part if isinstance(part, str) else str(part.get("text", ""))
                                for part in text
                            )
                        lines.append(f"• {text}")
        return "\n\n".join(line for line in lines if line)

    async def send_rich_digest(
        self,
        document: dict,
        user_id: Optional[int] = None,
        fallback_text: Optional[str] = None,
    ) -> bool:
        """Send a combined Rich Message digest and track its message ID."""
        if user_id is None:
            user_id = self.target_user_id
        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        rich_message = document.get("rich_message", document)
        try:
            documents = self.formatter.split_group_rich_digest({"rich_message": rich_message})
            if not documents:
                self.logger.warning("Rich digest has no blocks")
                return False
            message_ids = []
            for part in documents:
                message_ids.append(
                    await self._send_rich_message(self.target_chat_id, part["rich_message"])
                )
            save_digest_message_ids(message_ids, self.target_chat_id)
            self.logger.info("✅ Rich digest sent successfully (%s message(s))", len(message_ids))
            return True
        except Exception as error:
            self.logger.warning(f"Rich Message send failed, using Markdown fallback: {error}")
            text = fallback_text or self._rich_message_to_plain_text(rich_message)
            return await self.send_digest(text, user_id=user_id)

    async def send_channel_messages(
        self, channel_messages: list[tuple[str, str]], user_id: Optional[int] = None
    ) -> bool:
        """
        Send separate messages for each channel.

        Args:
            channel_messages: List of (channel_name, message_text) tuples
            user_id: Target user ID (defaults to configured user)

        Returns:
            True if all messages sent successfully, False otherwise
        """
        if user_id is None:
            user_id = self.target_user_id

        # Verify authorized user
        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        self.logger.info(
            "Sending %s channel messages to chat %s for user %s",
            len(channel_messages),
            self.target_chat_id,
            user_id,
        )

        success_count = 0
        failed_channels = []

        for i, (channel_name, message_text) in enumerate(channel_messages, 1):
            try:
                self.logger.info(f"Sending message {i}/{len(channel_messages)}: {channel_name}")

                # Verify message length
                if len(message_text) > TELEGRAM_MAX_MESSAGE_CHARS:
                    self.logger.error(
                        f"Message for '{channel_name}' exceeds {TELEGRAM_MAX_MESSAGE_CHARS} chars "
                        f"({len(message_text)}), splitting..."
                    )
                    # Split and send parts
                    parts = split_message(message_text, max_length=TELEGRAM_SAFE_MESSAGE_CHARS)
                    for part_num, part in enumerate(parts, 1):
                        await self._send_message_part(self.target_chat_id, part, part_num)
                        self.logger.info(f"Sent part {part_num}/{len(parts)} for {channel_name}")
                else:
                    # Send as single message
                    await self._send_message_part(self.target_chat_id, message_text, 1)

                success_count += 1
                self.logger.info(f"✅ Successfully sent message for {channel_name}")

                # Small delay between messages to avoid rate limiting
                if i < len(channel_messages):
                    await asyncio.sleep(0.5)

            except TelegramError as e:
                self.logger.error(f"❌ Failed to send message for {channel_name}: {e}")
                failed_channels.append(channel_name)
                continue

        # Log summary
        if success_count == len(channel_messages):
            self.logger.info(f"✅ All {success_count} channel messages sent successfully")
            return True
        else:
            self.logger.warning(
                f"⚠️ Sent {success_count}/{len(channel_messages)} messages. "
                f"Failed: {', '.join(failed_channels)}"
            )
            return False

    async def send_message(self, text: str, user_id: Optional[int] = None) -> bool:
        """
        Send a simple text message.

        Args:
            text: Message text
            chat_id: Telegram destination chat ID

        Returns:
            True if successful
        """
        if user_id is None:
            user_id = self.target_user_id

        if user_id != self.target_user_id:
            return False

        try:
            await self.bot.send_message(
                chat_id=self.target_chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
            return True
        except TelegramError as e:
            if "Can't parse entities" in str(e):
                self.logger.warning(
                    "Markdown parse error in send_message, falling back to plain text"
                )
                try:
                    await self.bot.send_message(
                        chat_id=self.target_chat_id, text=text, parse_mode=None
                    )
                    return True
                except TelegramError as e2:
                    self.logger.error(f"Failed to send message (plain text fallback): {e2}")
                    return False
            self.logger.error(f"Failed to send message: {e}")
            return False

    async def cleanup_old_digests(self, user_id: Optional[int] = None) -> bool:
        """
        Delete previous digest messages.

        Args:
            user_id: Target user ID (defaults to configured user)

        Returns:
            True if cleanup successful, False otherwise
        """
        if user_id is None:
            user_id = self.target_user_id

        # Verify authorized user
        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized cleanup attempt for user {user_id}")
            return False

        # Get stored message IDs
        message_ids = get_digest_message_ids(self.target_chat_id)

        if not message_ids:
            self.logger.info("No previous digest messages to clean up")
            return True

        self.logger.info(f"Cleaning up {len(message_ids)} previous digest messages")

        deleted_count = 0
        failed_count = 0

        for message_id in message_ids:
            try:
                await self.bot.delete_message(chat_id=self.target_chat_id, message_id=message_id)
                deleted_count += 1
            except TelegramError as e:
                # Message might already be deleted or not found
                if "message to delete not found" in str(e).lower():
                    self.logger.debug(f"Message {message_id} already deleted")
                    deleted_count += 1
                else:
                    self.logger.warning(f"Failed to delete message {message_id}: {e}")
                    failed_count += 1

        # Clear stored message IDs
        clear_digest_message_ids(self.target_chat_id)

        if failed_count == 0:
            self.logger.info(f"✅ Cleaned up {deleted_count} messages successfully")
            return True
        else:
            self.logger.warning(
                f"⚠️ Cleaned up {deleted_count} messages, {failed_count} failed to delete"
            )
            return deleted_count > 0

    async def _send_message_with_tracking(
        self, chat_id: str | int, message_text: str, channel_name: str
    ) -> Optional[int]:
        """
        Send a message with markdown fallback and return message ID.

        Args:
            chat_id: Telegram destination chat ID
            message_text: Message text to send
            channel_name: Channel name for logging

        Returns:
            Message ID if successful, None otherwise
        """
        try:
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            return message.message_id
        except TelegramError as e:
            if "Can't parse entities" in str(e):
                self.logger.warning("Markdown parse error, falling back to plain text")
                message = await self.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                return message.message_id
            raise

    async def _send_summary_message(
        self,
        chat_id: str | int,
        summary_message: str,
    ) -> Optional[int]:
        """
        Send summary message and return message ID.

        Args:
            chat_id: Telegram destination chat ID
            summary_message: Summary message text

        Returns:
            Message ID if successful, None otherwise
        """
        try:
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=summary_message,
                parse_mode=ParseMode.MARKDOWN,
            )
            self.logger.info("✅ Summary message sent")
            return message.message_id
        except TelegramError as e:
            if "Can't parse entities" in str(e):
                self.logger.warning("Markdown parse error in summary, falling back to plain text")
                message = await self.bot.send_message(
                    chat_id=chat_id,
                    text=summary_message,
                    parse_mode=None,
                )
                self.logger.info("✅ Summary message sent (plain text fallback)")
                return message.message_id
            self.logger.warning(f"⚠️ Failed to send summary message: {e}")
            return None

    def _log_and_return_result(
        self, success_count: int, total_count: int, failed_channels: list[str]
    ) -> bool:
        """
        Log summary and return success status.

        Args:
            success_count: Number of successful sends
            total_count: Total number of messages
            failed_channels: List of failed channel names

        Returns:
            True if all successful, False otherwise
        """
        if success_count == total_count:
            self.logger.info(f"✅ All {success_count} channel messages sent successfully")
            return True
        self.logger.warning(
            f"⚠️ Sent {success_count}/{total_count} messages. Failed: {', '.join(failed_channels)}"
        )
        return success_count > 0

    async def _send_channel_messages_loop(
        self, chat_id: str | int, channel_messages: list[tuple[str, str]]
    ) -> tuple[list[int], int, list[str]]:
        """
        Send messages for all channels.

        Args:
            user_id: Target user ID
            channel_messages: List of (channel_name, message_text) tuples

        Returns:
            Tuple of (sent_message_ids, success_count, failed_channels)
        """
        sent_message_ids = []
        success_count = 0
        failed_channels = []

        for i, (channel_name, message_text) in enumerate(channel_messages, 1):
            try:
                self.logger.info(f"Sending message {i}/{len(channel_messages)}: {channel_name}")

                message_id = await self._send_message_with_tracking(
                    chat_id, message_text, channel_name
                )
                if message_id is not None:
                    sent_message_ids.append(message_id)
                    success_count += 1
                    self.logger.info(f"✅ Successfully sent message for {channel_name}")

                if i < len(channel_messages):
                    await asyncio.sleep(0.5)

            except TelegramError as e:
                self.logger.error(f"❌ Failed to send message for {channel_name}: {e}")
                failed_channels.append(channel_name)
                continue

        return sent_message_ids, success_count, failed_channels

    async def send_channel_messages_with_tracking(
        self,
        channel_messages: list[tuple[str, str]],
        summary_message: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> bool:
        """
        Send separate messages for each channel and track message IDs for cleanup.

        Sends the summary placeholder first so it appears at the top, then sends
        each channel message.

        Args:
            channel_messages: List of (channel_name, message_text) tuples
            summary_message: Optional summary message to send first as digest header
            user_id: Target user ID (defaults to configured user)

        Returns:
            True if all messages sent successfully, False otherwise
        """
        if user_id is None:
            user_id = self.target_user_id

        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        self.logger.info(
            "Sending %s channel messages to chat %s for user %s",
            len(channel_messages),
            self.target_chat_id,
            user_id,
        )

        # Send summary placeholder FIRST so it appears at the top of the chat
        summary_id = None
        if summary_message:
            summary_id = await self._send_summary_message(self.target_chat_id, summary_message)

        # Send all channel messages and collect their IDs
        sent_message_ids, success_count, failed_channels = await self._send_channel_messages_loop(
            self.target_chat_id, channel_messages
        )

        if summary_id is not None and success_count == 0:
            # All channel sends failed; remove the orphaned placeholder
            try:
                await self.bot.delete_message(chat_id=self.target_chat_id, message_id=summary_id)
                self.logger.info("🗑️ Removed orphaned summary placeholder (no channels succeeded)")
                summary_id = None
            except TelegramError as e:
                self.logger.warning(f"⚠️ Failed to delete orphaned summary placeholder: {e}")

        # Save all message IDs for future cleanup (summary first for correct order)
        all_ids = ([summary_id] if summary_id else []) + sent_message_ids
        if all_ids:
            save_digest_message_ids(all_ids, self.target_chat_id)
            self.logger.info(f"Saved {len(all_ids)} message IDs for cleanup")

        return self._log_and_return_result(success_count, len(channel_messages), failed_channels)

    async def send_article_instant_view(
        self,
        title: str,
        lead: str,
        telegraph_url: str,
        user_id: Optional[int] = None,
    ) -> bool:
        """Send an editorial article announcement with Telegram Instant View preview.

        Args:
            title: Headline of the article
            lead: Short introductory lead text
            telegraph_url: Telegra.ph page URL
            user_id: Optional target user or chat ID

        Returns:
            True if message was sent successfully
        """
        if user_id is None:
            user_id = self.target_user_id

        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        destination_chat = self.target_chat_id or user_id

        lead_part = f"{lead.strip()}\n\n" if lead.strip() else ""
        message_text = (
            f"📰 *{title.strip()}*\n\n"
            f"{lead_part}"
            f"⚡️ [Читать полностью в Instant View]({telegraph_url})\n"
            f"{telegraph_url}"
        )
        try:
            await self.bot.send_message(
                chat_id=destination_chat,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            self.logger.info(f"Sent article Instant View to {destination_chat}")
            return True
        except TelegramError as e:
            self.logger.error(f"Failed to send article Instant View: {e}")
            return False


async def main():
    """Test sender."""
    from src.config_loader import load_config
    from src.utils import setup_logging

    config = load_config()
    logger = setup_logging(config.log_level)

    sender = DigestSender(config, logger)

    test_digest = """
📰Тестовый дайджест

📌Краткий обзор
Это тестовое сообщение для проверки отправки дайджеста.

💻 Test Channel
- Тестовый пункт 1
- Тестовый пункт 2

---
📈 **Статистика**: 1 канал, 10 сообщений
    """

    success = await sender.send_digest(test_digest)
    print(f"Send result: {'✅ Success' if success else '❌ Failed'}")


if __name__ == "__main__":
    asyncio.run(main())
