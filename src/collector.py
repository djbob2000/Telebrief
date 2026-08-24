"""
Message collector using Telethon to fetch messages from Telegram channels.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from telethon import TelegramClient, functions, types
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.types import Message as TelegramMessage

from src.config_loader import ChannelConfig, Config, ForumTopicConfig
from src.providers.telegram import (
    MEDIA_TOKENS,
    build_user_client,
    classify_media,
    ensure_connected,
    forward_origin,
    message_link,
    sender_display_name,
)
from src.ui_strings import get_ui_strings


@dataclass
class Message:
    """Represents a collected message."""

    text: str
    sender: str
    timestamp: datetime | None = None
    link: str = ""
    channel_name: str = ""
    has_media: bool = False
    media_type: str = ""
    message_id: int | None = None
    reply_to_id: int | None = None
    topic_id: int | None = None
    forward_origin_name: str | None = None
    forward_origin_username: str | None = None
    channel_id: int | str | None = None
    temporal_fidelity: str | None = None
    raw_timestamp: str | None = None


class MessageCollector:
    """Collects messages from Telegram channels using Telethon."""

    def __init__(self, config: Config, logger: logging.Logger):
        """
        Initialize message collector.

        Args:
            config: Application configuration
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self._ui = get_ui_strings(config.settings.output_language)
        self.client = build_user_client(config)

    async def connect(self):
        """Connect to Telegram using an existing user session.

        Requires a pre-authenticated session file at sessions/user.session.
        Create one by running: python -m src.collector
        """
        await ensure_connected(self.client, self.logger)

    async def disconnect(self):
        """Disconnect from Telegram."""
        await self.client.disconnect()
        self.logger.info("Disconnected from Telegram")

    async def fetch_messages(self, hours: int = 24) -> Dict[str, List[Message]]:
        """
        Fetch messages from all configured channels.

        Args:
            hours: Number of hours to look back

        Returns:
            Dictionary mapping channel names to lists of messages
        """
        self.logger.info(
            f"Fetching messages from {len(self.config.channels)} channels "
            f"(default lookback: {hours}h; individual channels may override)"
        )

        all_messages = {}
        now = datetime.now(timezone.utc)

        for channel_config in self.config.channels:
            channel_hours = (
                hours if channel_config.lookback_hours is None else channel_config.lookback_hours
            )
            lookback_time = now - timedelta(hours=channel_hours)

            if channel_config.topics:
                topic_messages = await self._fetch_topics_for_channel(channel_config, lookback_time)
                all_messages.update(topic_messages)
            else:
                all_messages[channel_config.name] = await self._fetch_channel_with_retries(
                    channel_config, lookback_time
                )

        total_messages = sum(len(msgs) for msgs in all_messages.values())
        self.logger.info(f"Total messages collected: {total_messages}")

        return all_messages

    async def fetch_channel_messages(
        self, channel_config: ChannelConfig, lookback_time: datetime
    ) -> List[Message]:
        """
        Fetch messages from a single channel.

        Args:
            channel_config: Channel configuration
            lookback_time: Earliest message time

        Returns:
            List of Message objects
        """
        messages = []
        max_messages = self.config.settings.max_messages_per_channel

        try:
            # Get channel entity
            entity = await self.client.get_entity(channel_config.id)

            # Fetch messages
            async for message in self.client.iter_messages(
                entity, limit=max_messages, offset_date=datetime.now(timezone.utc)
            ):
                # Stop if message is older than lookback time
                if message.date < lookback_time:
                    break

                project_message = await self._to_project_message(
                    entity, channel_config.name, message
                )
                if project_message is not None:
                    messages.append(project_message)

        except Exception as e:
            self.logger.error(f"Error fetching from {channel_config.name}: {e}")
            raise

        messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages

    async def _fetch_channel_with_retries(
        self, channel_config: ChannelConfig, lookback_time: datetime
    ) -> List[Message]:
        """Fetch messages from a single channel with error handling and retry on flood wait."""
        try:
            messages = await self.fetch_channel_messages(channel_config, lookback_time)
            self.logger.info(f"✓ {channel_config.name}: {len(messages)} messages")
            return messages
        except ChannelPrivateError:
            self.logger.warning(f"✗ {channel_config.name}: Channel is private or not accessible")
            return []
        except FloodWaitError as e:
            self.logger.warning(f"✗ {channel_config.name}: Rate limited, need to wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
            try:
                messages = await self.fetch_channel_messages(channel_config, lookback_time)
                self.logger.info(f"✓ {channel_config.name}: {len(messages)} messages")
                return messages
            except Exception as retry_error:
                self.logger.error(f"Retry failed for {channel_config.name}: {retry_error}")
                return []
        except ValueError as e:
            if "Could not find the input entity" in str(e):
                self.logger.error(
                    f"✗ {channel_config.name}: Channel not found. "
                    f"Make sure you've joined this channel with your Telegram account "
                    f"and the channel ID ({channel_config.id}) is correct."
                )
            else:
                self.logger.error(f"✗ {channel_config.name}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"✗ {channel_config.name}: Error fetching messages: {e}")
            return []

    async def _fetch_topics_for_channel(
        self, channel_config: ChannelConfig, lookback_time: datetime
    ) -> Dict[str, List[Message]]:
        """Fetch messages from forum topics within a channel."""
        topic_messages: Dict[str, List[Message]] = {}
        try:
            entity = await self.client.get_entity(channel_config.id)
            for topic in channel_config.topics:
                logical_name = f"{channel_config.name} — {topic.name}"
                try:
                    messages = await self._fetch_topic_messages(
                        entity, channel_config, topic, lookback_time
                    )
                    topic_messages[logical_name] = messages
                    self.logger.info(f"✓ {logical_name}: {len(messages)} messages")
                except Exception as topic_error:
                    self.logger.error(f"✗ {logical_name}: Error fetching topic: {topic_error}")
        except ChannelPrivateError:
            self.logger.warning(f"✗ {channel_config.name}: Channel is private or not accessible")
        except Exception as e:
            self.logger.error(f"✗ {channel_config.name}: Error fetching topics: {e}")
        return topic_messages

    async def _fetch_topic_messages(
        self,
        entity,
        channel_config: ChannelConfig,
        topic: ForumTopicConfig,
        lookback_time: datetime,
    ) -> List[Message]:
        """Fetch only messages belonging to one Telegram forum topic."""
        now = datetime.now(timezone.utc)
        response = await self.client(
            functions.messages.SearchRequest(
                peer=entity,
                q="",
                filter=types.InputMessagesFilterEmpty(),
                min_date=lookback_time,
                max_date=now,
                offset_id=0,
                add_offset=0,
                limit=self.config.settings.max_messages_per_channel,
                max_id=0,
                min_id=0,
                hash=0,
                top_msg_id=topic.id,
            )
        )

        logical_name = f"{channel_config.name} — {topic.name}"
        messages: List[Message] = []
        for message in response.messages:
            if message.date < lookback_time:
                continue
            project_message = await self._to_project_message(
                entity, logical_name, message, topic_id=topic.id
            )
            if project_message is not None:
                messages.append(project_message)

        messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages

    async def _to_project_message(
        self, entity, channel_name: str, message, topic_id: int | None = None
    ) -> Message | None:
        """Convert a Telethon message to the project model, skipping empty service posts."""
        if not message.text:
            if message.media:
                media_type = self._get_media_type(message)
                text = f"[{media_type}]"
            else:
                return None
        else:
            text = message.text
            media_type = self._get_media_type(message) if message.media else ""

        sender = await self._get_sender_name(message)
        link = await self._generate_message_link(entity, message.id)
        reply_to = getattr(message, "reply_to", None)
        reply_to_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
        fwd_name, fwd_username = await self._get_forward_origin(message)
        return Message(
            text=text,
            sender=sender,
            timestamp=message.date,
            link=link,
            channel_name=channel_name,
            has_media=message.media is not None,
            media_type=media_type or "",
            message_id=getattr(message, "id", None),
            reply_to_id=reply_to_id,
            topic_id=topic_id,
            forward_origin_name=fwd_name,
            forward_origin_username=fwd_username,
            channel_id=getattr(entity, "id", None),
        )

    async def download_message_photo(
        self, channel_identifier: int | str, message_id: int
    ) -> Optional[bytes]:
        """Download photo bytes for a specific message from Telegram."""
        try:
            entity = await self.client.get_entity(channel_identifier)
            msg = await self.client.get_messages(entity, ids=message_id)
            if msg and msg.media:
                media_bytes = await self.client.download_media(msg.media, file=bytes)
                if isinstance(media_bytes, bytes) and media_bytes:
                    return media_bytes
        except Exception as e:
            self.logger.warning(
                f"Failed to download photo for message {message_id} from {channel_identifier}: {e}"
            )
        return None

    async def _get_forward_origin(self, message: TelegramMessage) -> tuple[str | None, str | None]:
        """Extract forward origin name and username if available."""
        return forward_origin(message)

    def _get_media_type(self, message: TelegramMessage) -> str:
        """
        Determine media type from message.

        Args:
            message: Telegram message

        Returns:
            Media type string
        """
        token = classify_media(message)
        if not token:
            return ""
        return self._ui.get(MEDIA_TOKENS[token], self._ui["media_other"])

    async def _get_sender_name(self, message: TelegramMessage) -> str:
        """
        Get sender name from message.

        Args:
            message: Telegram message

        Returns:
            Sender name or "Unknown"
        """
        return await sender_display_name(message)

    async def _generate_message_link(self, entity, message_id: int) -> str:
        """
        Generate clickable link to message.

        Args:
            entity: Channel entity
            message_id: Message ID

        Returns:
            Message link URL
        """
        return message_link(entity, message_id)


async def main():
    """Authenticate and test the message collector.

    Run interactively to create sessions/user.session:
        python -m src.collector
    """
    from src.config_loader import load_config
    from src.utils import setup_logging

    config = load_config()
    logger = setup_logging(config.log_level)

    # Interactive auth: call start() which prompts for phone + code
    client = TelegramClient("sessions/user", config.telegram_api_id, config.telegram_api_hash)
    print("Authenticating with Telegram User API...")
    print("You will be prompted for your phone number and a login code.")
    await client.start()
    print("Authenticated! Session saved to sessions/user.session")

    # Quick test: fetch 1 hour of messages
    collector = MessageCollector(config, logger)
    try:
        await collector.connect()
        messages = await collector.fetch_messages(hours=1)

        for channel_name, msgs in messages.items():
            print(f"\n{channel_name}: {len(msgs)} messages")
            for msg in msgs[:3]:  # Show first 3
                print(f"  - {msg.sender}: {msg.text[:50]}...")
    finally:
        await collector.disconnect()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
