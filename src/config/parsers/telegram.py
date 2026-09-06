"""Telegram channels and chat parsing and validation routines."""

from __future__ import annotations

from typing import List

from src.config.parsers.common import _parse_filter_specs
from src.config.schemas.common import FilterSpec
from src.config.schemas.publication import DigestGroupConfig
from src.config.schemas.telegram import (
    SOURCE_TYPES,
    ChannelConfig,
    ForumTopicConfig,
    TelegramConfig,
)


def _normalize_source_type(value: object, label: str, *, allow_none: bool = False) -> str | None:
    """Normalize and validate an editorial source role."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if allow_none else ""
        raise ValueError(f"{label} must be one of {', '.join(SOURCE_TYPES)}{suffix}")
    normalized = value.strip().lower()
    if normalized not in SOURCE_TYPES:
        raise ValueError(f"{label} must be one of {', '.join(SOURCE_TYPES)}, got {value!r}")
    return normalized


def _validate_channel_lookback(i: int, ch: dict) -> int | None:
    lookback_hours = ch.get("lookback_hours")
    if lookback_hours is None:
        return None
    if not isinstance(lookback_hours, int) or isinstance(lookback_hours, bool):
        raise ValueError(
            f"channels[{i}].lookback_hours must be an int, got {type(lookback_hours).__name__}"
        )
    if lookback_hours <= 0:
        raise ValueError(f"channels[{i}].lookback_hours must be positive, got {lookback_hours}")
    return lookback_hours


def _validate_channel_group(i: int, ch: dict) -> str | None:
    group = ch.get("group")
    if group is None:
        return None
    if not isinstance(group, str) or not group.strip():
        raise ValueError(f"channels[{i}].group must be a non-empty string or null, got {group!r}")
    return group.strip()


def _validate_channel_source_type(i: int, ch: dict) -> str:
    """Validate a channel's optional editorial source role."""
    return (
        _normalize_source_type(ch.get("source_type", "mixed"), f"channels[{i}].source_type")
        or "mixed"
    )


def _validate_channel_topics(i: int, ch: dict) -> list[ForumTopicConfig]:
    raw_topics = ch.get("topics", [])
    if not isinstance(raw_topics, list):
        raise ValueError(f"channels[{i}].topics must be a list, got {type(raw_topics).__name__}")

    topics: list[ForumTopicConfig] = []
    for topic_index, raw_topic in enumerate(raw_topics):
        path = f"channels[{i}].topics[{topic_index}]"
        if not isinstance(raw_topic, dict):
            raise ValueError(f"{path} must be a mapping, got {type(raw_topic).__name__}")
        topic_id = raw_topic.get("id")
        if not isinstance(topic_id, int) or isinstance(topic_id, bool) or topic_id <= 0:
            raise ValueError(f"{path}.id must be a positive int, got {topic_id!r}")
        topic_name = raw_topic.get("name")
        if not isinstance(topic_name, str) or not topic_name.strip():
            raise ValueError(f"{path}.name must be a non-empty string, got {topic_name!r}")
        topic_source_type = _normalize_source_type(
            raw_topic.get("source_type"), f"{path}.source_type", allow_none=True
        )
        topics.append(
            ForumTopicConfig(
                id=topic_id,
                name=topic_name.strip(),
                source_type=topic_source_type,
            )
        )
    return topics


def _validate_channel_id_name(i: int, ch: dict) -> None:
    for required in ("id", "name"):
        if required not in ch:
            raise ValueError(f"channels[{i}] missing required field '{required}'")
    if not isinstance(ch["name"], str) or not ch["name"].strip():
        raise ValueError(f"channels[{i}].name must be a non-empty string, got {ch['name']!r}")
    if not isinstance(ch["id"], (str, int)) or isinstance(ch["id"], bool):
        raise ValueError(f"channels[{i}].id must be a string or int, got {type(ch['id']).__name__}")


def _parse_channel_entry(i: int, ch: object) -> ChannelConfig:
    """Parse and validate a single channel entry from YAML."""
    if not isinstance(ch, dict):
        raise ValueError(f"channels[{i}] must be a mapping, got {type(ch).__name__}")
    _validate_channel_id_name(i, ch)
    lookback_hours = _validate_channel_lookback(i, ch)
    prompt_extra = ch.get("prompt_extra", "")
    if not isinstance(prompt_extra, str):
        raise ValueError(
            f"channels[{i}].prompt_extra must be a string, got {type(prompt_extra).__name__}"
        )
    raw_filters = ch.get("filters")
    channel_filters: list[FilterSpec] | None = None
    if raw_filters is not None:
        channel_filters = _parse_filter_specs(raw_filters, f"channels[{i}].filters")
    return ChannelConfig(
        id=ch["id"],
        name=ch["name"],
        lookback_hours=lookback_hours,
        prompt_extra=prompt_extra,
        filters=channel_filters,
        group=_validate_channel_group(i, ch),
        topics=_validate_channel_topics(i, ch),
        source_type=_validate_channel_source_type(i, ch),
    )


def _validate_channel_groups(
    channels: List[ChannelConfig],
    digest_groups: list[DigestGroupConfig],
    output_language: str,
) -> None:
    """Cross-validate that channels[*].group references a known group name."""
    from src.ui_strings import get_ui_strings

    ui = get_ui_strings(output_language)
    localized_other = ui.get("group_other", "Other")
    valid_names = {g.name for g in digest_groups} | {"Other", localized_other}

    bad: list[str] = []
    for ch in channels:
        if ch.group is not None and ch.group not in valid_names:
            bad.append(f"channel {ch.name!r}: group {ch.group!r}")

    if bad:
        raise ValueError(
            "Unknown group references in channels config:\n"
            + "\n".join(f"  {b}" for b in bad)
            + f"\nValid groups: {', '.join(sorted(valid_names))}"
        )


def _parse_channels(yaml_config: dict) -> List[ChannelConfig]:
    """Parse and validate channel configs from YAML."""
    if not isinstance(yaml_config, dict):
        raise ValueError(
            f"config.yaml must contain a top-level mapping, got {type(yaml_config).__name__}"
        )
    channels_value = yaml_config.get("channels", [])
    if not isinstance(channels_value, list):
        raise ValueError(
            f"config.yaml field 'channels' must be a list, got {type(channels_value).__name__}"
        )
    channels = [_parse_channel_entry(i, ch) for i, ch in enumerate(channels_value)]

    if not channels:
        raise ValueError("No channels configured in config.yaml")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for c in channels:
        if c.name in seen:
            duplicates.add(c.name)
        seen.add(c.name)
    if duplicates:
        raise ValueError(f"Duplicate channel names in config.yaml: {', '.join(sorted(duplicates))}")

    return channels


def _parse_telegram_config(yaml_config: dict) -> TelegramConfig:
    """Parse and validate the optional top-level telegram: block."""
    raw = yaml_config.get("telegram")
    if raw is None:
        return TelegramConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'telegram' must be a mapping, got {type(raw).__name__}")

    mode = raw.get("processing_mode", "knowledge_full")
    if not isinstance(mode, str) or mode not in ("knowledge_full", "knowledge_no_embeddings"):
        raise ValueError(
            "telegram.processing_mode must be 'knowledge_full' or 'knowledge_no_embeddings', "
            f"got {mode!r}"
        )
    return TelegramConfig(processing_mode=mode)
