"""Telegram and channel configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config.schemas.common import FilterSpec

SOURCE_TYPES = ("news", "community", "official", "classifieds", "mixed")


@dataclass
class ForumTopicConfig:
    """A selected forum topic within a Telegram group."""

    id: int
    name: str
    source_type: str | None = None


@dataclass
class ChannelConfig:
    """Configuration for a single Telegram channel/chat."""

    id: str | int  # str for @username, int for numeric Telegram channel ID
    name: str
    lookback_hours: int | None = None  # None = use global settings.lookback_hours
    prompt_extra: str = ""  # appended to system prompt when summarizing this channel
    filters: list[FilterSpec] | None = None  # None=use global, []=explicit no-op
    group: str | None = None  # must reference digest_groups[*].name, "Other", or None
    topics: list[ForumTopicConfig] = field(default_factory=list)
    source_type: str = "mixed"


@dataclass
class TelegramConfig:
    """Telegram operational settings (processing mode control)."""

    processing_mode: str = "knowledge_full"  # "knowledge_full" | "knowledge_no_embeddings"


def effective_source_type(channel: ChannelConfig, topic: ForumTopicConfig | None = None) -> str:
    """Return the configured editorial role using topic > channel > mixed precedence."""
    return (topic.source_type if topic and topic.source_type else channel.source_type) or "mixed"


class SourceRoleResolver:
    """Resolve configured editorial roles without inferring them from names."""

    def __init__(self, channels: list[ChannelConfig]):
        self._channels = channels

    def resolve(self, channel_name: str, topic_id: int | None = None) -> str:
        for channel in self._channels:
            logical_prefix = f"{channel.name} — "
            if channel_name != channel.name and not channel_name.startswith(logical_prefix):
                continue

            topic_name = (
                channel_name[len(logical_prefix) :]
                if channel_name.startswith(logical_prefix)
                else None
            )
            for topic in channel.topics:
                if (topic_id is not None and topic.id == topic_id) or (
                    topic_name is not None and topic.name == topic_name
                ):
                    return effective_source_type(channel, topic)
            return effective_source_type(channel)
        return "mixed"
