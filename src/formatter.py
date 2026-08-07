"""
Markdown formatter for digest output.
"""

import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.collector import Message
from src.config_loader import Config
from src.grouper import GroupedPoint
from src.summarizer import ERROR_SUMMARY_PREFIX
from src.ui_strings import get_month_names, get_ui_strings
from src.utils import TELEGRAM_MAX_MESSAGE_CHARS, TELEGRAM_SAFE_MESSAGE_CHARS

_CHANNEL_URL_RE = re.compile(r"^https://t\.me/(?:c/\d+|[^/]{2,})$")
_INLINE_SOURCE_URL_RE = re.compile(r"https://t\.me/[^\s)\]]+")
_MARKDOWN_SOURCE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https://t\.me/[^)\s]+)\)")
_LEADING_BULLET_RE = re.compile(r"^\s*(?:(?:[•●▪◦*-]+|\d+[.)])\s*)+")
_SOURCE_MARKER_RE = re.compile(r"\s*(?:🖇️|🔗)\s*")


class DigestFormatter:
    """Formats digest into Markdown with emojis and links."""

    def __init__(self, config: Config, logger: logging.Logger):
        """
        Initialize formatter.

        Args:
            config: Application configuration
            logger: Logger instance
        """
        self.config = config
        self.logger = logger
        self.use_emojis = config.settings.use_emojis
        self.include_stats = config.settings.include_statistics
        self._language = config.settings.output_language
        self._ui = get_ui_strings(self._language)
        self._month_names = get_month_names(self._language)

    def _format_date(self, dt: datetime) -> str:
        """Return date string with month name translated to output_language.

        Uses the month index rather than strftime %B to avoid depending on the
        host system's locale setting.
        """
        month_name = self._month_names[dt.month - 1]
        return f"{dt.day:02d} {month_name} {dt.year}"

    def create_digest(
        self,
        overview: str,
        channel_summaries: Dict[str, str],
        messages_by_channel: Dict[str, List[Message]],
        hours: int = 24,
    ) -> str:
        """
        Create formatted digest.

        Args:
            overview: Executive summary
            channel_summaries: Per-channel summaries
            messages_by_channel: Original messages (for links)
            hours: Time range covered

        Returns:
            Formatted Markdown digest
        """
        self.logger.info("Formatting digest")
        self.logger.debug(
            f"Overview: {len(overview) if overview else 0} chars, truthy: {bool(overview)}"
        )
        self.logger.debug(f"Channel summaries: {len(channel_summaries)} channels")

        # Build digest parts
        parts = []

        # Header
        header = self._create_header(hours)
        parts.append(header)

        # Overview section
        if overview:
            self.logger.debug("Adding overview section")
            parts.append(f"## 🎯 {self._ui['overview']}\n")
            parts.append(overview)
            parts.append("\n---\n")
        else:
            self.logger.warning("Overview is empty or None, skipping")

        # Channel sections
        for channel_name, summary in channel_summaries.items():
            self.logger.debug(
                f"Processing channel '{channel_name}': {len(summary) if summary else 0} chars"
            )
            if not summary or summary.lower().startswith(ERROR_SUMMARY_PREFIX.lower()):
                self.logger.warning(f"Skipping channel '{channel_name}': empty or contains error")
                continue

            section = self._create_channel_section(
                channel_name, summary, messages_by_channel.get(channel_name, [])
            )
            parts.append(section)
            self.logger.debug(f"Added channel section for '{channel_name}'")

        # Statistics footer
        if self.include_stats:
            stats = self._create_statistics(messages_by_channel, hours)
            parts.append(stats)

        digest = "\n".join(parts)

        self.logger.info(f"Digest formatted: {len(digest)} characters")
        return digest

    def _create_header(self, hours: int) -> str:
        """
        Create digest header.

        Args:
            hours: Time range

        Returns:
            Header string
        """
        date_str = self._format_date(datetime.now(timezone.utc))
        emoji = "📊" if self.use_emojis else ""
        return f"# {emoji} {self._ui['daily_digest']} - {date_str}\n"

    def _extract_channel_url(self, messages: List[Message]) -> Optional[str]:
        """
        Derive the channel base URL from the first message with a valid link.

        Args:
            messages: Messages from the channel

        Returns:
            Channel URL (e.g. https://t.me/username) or None if unavailable
        """
        for msg in messages:
            if msg.link and msg.link != "#":
                base = msg.link.rsplit("/", 1)[0]
                if _CHANNEL_URL_RE.match(base):
                    return base
        return None

    def _create_channel_section(
        self, channel_name: str, summary: str, messages: List[Message]
    ) -> str:
        """
        Create section for a single channel.

        Args:
            channel_name: Channel name
            summary: Channel summary
            messages: Messages from channel (for link extraction)

        Returns:
            Formatted section
        """
        emoji = self._pick_emoji(channel_name)
        channel_url = self._extract_channel_url(messages)

        if channel_url:
            header = f"## {emoji} {channel_name} · [{self._ui['open_channel']} →]({channel_url})\n"
        else:
            header = f"## {emoji} {channel_name}\n"

        section_parts = [header, summary, "\n"]

        return "\n".join(section_parts)

    def _pick_emoji(self, channel_name: str) -> str:
        """
        Pick appropriate emoji for channel.

        Args:
            channel_name: Channel name

        Returns:
            Emoji character
        """
        if not self.use_emojis:
            return "•"

        name_lower = channel_name.lower()

        # Tech/Dev
        if any(word in name_lower for word in ["tech", "dev", "code", "программ", "разработ"]):
            return "💻"
        # Crypto/Finance
        elif any(word in name_lower for word in ["crypto", "bitcoin", "финанс", "крипто"]):
            return "💰"
        # News
        elif any(word in name_lower for word in ["news", "новост"]):
            return "📰"
        # Business
        elif any(word in name_lower for word in ["business", "бизнес", "startup"]):
            return "💼"
        # Science
        elif any(word in name_lower for word in ["science", "research", "наук"]):
            return "🔬"
        # AI/ML
        elif any(word in name_lower for word in ["ai", "ml", "artificial", "ии", "искусственн"]):
            return "🤖"
        # Design
        elif any(word in name_lower for word in ["design", "дизайн", "ui", "ux"]):
            return "🎨"
        # Marketing
        elif any(word in name_lower for word in ["marketing", "маркетинг", "smm"]):
            return "📈"
        # Default
        else:
            return "📺"

    def format_channel_message(
        self, channel_name: str, summary: str, messages: List[Message], hours: int = 24
    ) -> str:
        """
        Format a single channel's summary as a standalone Telegram message.

        Args:
            channel_name: Name of the channel
            summary: AI-generated summary
            messages: Original messages from the channel
            hours: Time range covered

        Returns:
            Formatted message ready to send
        """
        self.logger.info(f"Formatting message for channel: {channel_name}")

        parts = []

        # Channel header with date
        date_str = self._format_date(datetime.now(timezone.utc))
        emoji = self._pick_emoji(channel_name)
        channel_url = self._extract_channel_url(messages)
        if channel_url:
            header = f"# {emoji} {channel_name} · [{self._ui['open_channel']} →]({channel_url})\n*{date_str}*\n"
        else:
            header = f"# {emoji} {channel_name}\n*{date_str}*\n"
        parts.append(header)

        # Summary
        parts.append(summary)

        # Statistics for this channel
        if self.include_stats:
            message_count = len(messages)
            parts.append(f"\n---\n📊 {self._ui['messages_count']}: {message_count}")
            if hours == 24:
                parts.append(f"⏱️ {self._ui['last_hours'].format(hours=hours)}")

        message = "\n".join(parts)

        # Verify length doesn't exceed Telegram's current bot-message limit
        if len(message) > TELEGRAM_MAX_MESSAGE_CHARS:
            self.logger.warning(
                f"Channel message for '{channel_name}' exceeds {TELEGRAM_MAX_MESSAGE_CHARS} chars "
                f"({len(message)}), truncating..."
            )
            # Leave a small safety margin for Telegram entity parsing.
            truncated = message[:TELEGRAM_SAFE_MESSAGE_CHARS].rsplit("\n", 1)[0]
            message = truncated + f"\n\n{self._ui['truncated']}"

        self.logger.info(f"Formatted message for {channel_name}: {len(message)} characters")
        return message

    def format_summary_message(
        self, total_channels: int, total_messages: int, hours: int = 24
    ) -> str:
        """
        Format a summary message for the digest header.

        Args:
            total_channels: Number of channels processed
            total_messages: Total messages processed
            hours: Time range covered

        Returns:
            Summary message
        """
        now = datetime.now(timezone.utc)
        date_str = self._format_date(now)
        start_time = now - timedelta(hours=hours)

        message = (
            f"📊 **{self._ui['digest_completed']}** - {date_str}\n\n"
            f"✅ {self._ui['channels_processed']}: {total_channels}\n"
            f"📨 {self._ui['total_messages']}: {total_messages}\n"
            f"⏱️ {self._ui['period']}: "
            f"{start_time.strftime('%d.%m %H:%M')} - {now.strftime('%d.%m %H:%M')} UTC\n"
        )
        return message

    def _pick_group_emoji(self, group_name: str) -> str:
        """Pick emoji for a topic group name (case-insensitive)."""
        if not self.use_emojis:
            return "•"
        name_lower = group_name.lower()
        mapping = {
            "events": "🎪",
            "event": "🎪",
            "news": "📰",
            "sport": "⚽",
            "sports": "⚽",
            "other": "📌",
        }
        return mapping.get(name_lower, "📌")

    def format_group_digest(
        self,
        grouped_sections: list[tuple[str, list[GroupedPoint]]],
        hours: int = 24,
    ) -> str:
        """Format all topic groups as one compact Telegram message."""
        sections = [(name, points) for name, points in grouped_sections if points]
        if not sections:
            return ""

        date_str = self._format_date(datetime.now(timezone.utc))
        if self._language == "Russian":
            title = f"Дайджест Бердянска · {date_str}"
        else:
            title = f"{self._ui['daily_digest']} · {date_str}"
        parts = [title]

        for group_name, points in sections:
            bullet_lines = []
            for point in points:
                point_text = point.point
                markdown_source = _MARKDOWN_SOURCE_LINK_RE.search(point_text)
                source_url = point.source_url
                if markdown_source:
                    destination_url = markdown_source.group(2)
                    visible_label = markdown_source.group(1).strip()
                    label_url = (
                        visible_label
                        if visible_label.startswith("https://")
                        else f"https://{visible_label}"
                    )
                    label_is_message_url = bool(
                        _INLINE_SOURCE_URL_RE.fullmatch(label_url)
                        and re.search(r"/\d+$", label_url)
                    )
                    source_url = label_url if label_is_message_url else destination_url
                    replacement = "" if label_url.startswith("https://t.me/") else visible_label
                    point_text = _MARKDOWN_SOURCE_LINK_RE.sub(replacement, point_text, count=1)

                inline_urls = _INLINE_SOURCE_URL_RE.findall(point_text)
                if inline_urls:
                    source_url = inline_urls[0]
                if inline_urls:
                    point_text = _INLINE_SOURCE_URL_RE.sub("", point_text)
                    point_text = re.sub(r"\s*(?:→|->|—|–)\s*$", "", point_text).rstrip()
                point_text = _SOURCE_MARKER_RE.sub(" ", point_text)
                point_text = _LEADING_BULLET_RE.sub("", point_text)
                # The AI may emit a visible link arrow even when the URL is
                # already carried by source_url and will be rendered below.
                point_text = re.sub(r"(?:\s*(?:→|↗))+\s*$", "", point_text).rstrip()
                point_text = re.sub(r"[ \t]{2,}", " ", point_text).strip()

                line = f"• {point_text}"
                if source_url and (
                    _CHANNEL_URL_RE.match(source_url) or _INLINE_SOURCE_URL_RE.fullmatch(source_url)
                ):
                    line += f" [↗]({source_url})"
                bullet_lines.append(line)

            parts.append(
                "\n".join(
                    [
                        f"**📌 {group_name}**",
                        "",
                        *bullet_lines,
                    ]
                )
            )

        return "\n\n".join(parts)

    def _clean_group_point(self, point: GroupedPoint) -> tuple[str, str]:
        """Normalize one grouped point and resolve its Telegram source URL."""
        point_text = point.point
        markdown_source = _MARKDOWN_SOURCE_LINK_RE.search(point_text)
        source_url = point.source_url
        if markdown_source:
            destination_url = markdown_source.group(2)
            visible_label = markdown_source.group(1).strip()
            label_url = (
                visible_label
                if visible_label.startswith("https://")
                else f"https://{visible_label}"
            )
            label_is_message_url = bool(
                _INLINE_SOURCE_URL_RE.fullmatch(label_url)
                and re.search(r"/\d+$", label_url)
            )
            source_url = label_url if label_is_message_url else destination_url
            replacement = "" if label_url.startswith("https://t.me/") else visible_label
            point_text = _MARKDOWN_SOURCE_LINK_RE.sub(replacement, point_text, count=1)

        inline_urls = _INLINE_SOURCE_URL_RE.findall(point_text)
        if inline_urls:
            source_url = inline_urls[0]
            point_text = _INLINE_SOURCE_URL_RE.sub("", point_text)
            point_text = re.sub(r"\s*(?:→|->|—|–)\s*$", "", point_text).rstrip()
        point_text = _SOURCE_MARKER_RE.sub(" ", point_text)
        point_text = _LEADING_BULLET_RE.sub("", point_text)
        point_text = re.sub(r"(?:\s*(?:→|↗))+\s*$", "", point_text).rstrip()
        point_text = re.sub(r"[ \t]{2,}", " ", point_text).strip()
        return point_text, source_url

    def format_group_rich_digest(
        self,
        grouped_sections: list[tuple[str, list[GroupedPoint]]],
    ) -> dict:
        """Build one Telegram Rich Message document for grouped news."""
        sections = [(name, points) for name, points in grouped_sections if points]
        if not sections:
            return {"rich_message": {"blocks": []}}

        date_str = self._format_date(datetime.now(timezone.utc))
        if self._language == "Russian":
            title = f"Дайджест Бердянска · {date_str}"
        else:
            title = f"{self._ui['daily_digest']} · {date_str}"

        blocks = [{"type": "heading", "size": 2, "text": title}]
        for group_name, points in sections:
            items = []
            for point in points:
                point_text, source_url = self._clean_group_point(point)
                text_parts: list[object] = [point_text]
                if source_url and (
                    _CHANNEL_URL_RE.match(source_url)
                    or _INLINE_SOURCE_URL_RE.fullmatch(source_url)
                ):
                    text_parts.extend(
                        [
                            " ",
                            {"type": "url", "text": "↗", "url": source_url},
                        ]
                    )
                items.append(
                    {
                        "blocks": [
                            {
                                "type": "paragraph",
                                "text": text_parts,
                            }
                        ]
                    }
                )
            blocks.extend(
                [
                    {"type": "heading", "size": 3, "text": f"📌 {group_name}"},
                    {"type": "list", "items": items},
                ]
            )

        return {"rich_message": {"blocks": blocks}}

    def split_group_rich_digest(
        self,
        document: dict,
        max_length: int = TELEGRAM_SAFE_MESSAGE_CHARS,
    ) -> list[dict]:
        """Split a Rich digest only between complete group/list blocks."""
        blocks = document.get("rich_message", {}).get("blocks", [])
        if not blocks:
            return []
        title = blocks[0]
        parts: list[dict] = []
        current = [title]

        def encoded_size(candidate: list[dict]) -> int:
            return len(
                json.dumps(
                    {"rich_message": {"blocks": candidate}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

        content_blocks = blocks[1:]
        units = [
            content_blocks[index : index + 2]
            for index in range(0, len(content_blocks), 2)
        ]
        for unit in units:
            candidate = current + unit
            if len(current) > 1 and encoded_size(candidate) > max_length:
                parts.append({"rich_message": {"blocks": current}})
                current = [title, *unit]
            else:
                current = candidate
        if len(current) > 1:
            parts.append({"rich_message": {"blocks": current}})
        if not parts:
            parts.append({"rich_message": {"blocks": blocks}})
        return parts

    def _create_statistics(self, messages_by_channel: Dict[str, List[Message]], hours: int) -> str:
        """
        Create statistics footer.

        Args:
            messages_by_channel: Messages grouped by channel
            hours: Time range

        Returns:
            Statistics string
        """
        total_messages = sum(len(msgs) for msgs in messages_by_channel.values())
        active_channels = sum(1 for msgs in messages_by_channel.values() if msgs)

        # Time range
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)

        stats_parts = [
            "---\n",
            f"📈 **{self._ui['stats_header']}**: {active_channels} {self._ui['channels']}, "
            f"{total_messages} {self._ui['messages_processed']}",
        ]

        if hours == 24:
            stats_parts.append(
                f"⏱️ {self._ui['digest_for']}: {start_time.strftime('%d.%m %H:%M')} - "
                f"{end_time.strftime('%d.%m %H:%M')} UTC"
            )
        else:
            stats_parts.append(f"⏱️ {self._ui['period_last_hours'].format(hours=hours)}")

        return "\n".join(stats_parts)


def main():
    """Test formatter."""
    from src.config_loader import load_config
    from src.utils import setup_logging

    config = load_config()
    logger = setup_logging(config.log_level)

    formatter = DigestFormatter(config, logger)

    # Test data
    overview = """
    Сегодня основные темы: запуск новой версии Python 3.13 обсуждался
    в нескольких технических каналах, криптовалютный рынок показал высокую
    волатильность на фоне новостей о регулировании.
    """

    channel_summaries = {
        "TechCrunch": """
- 🚀 Python 3.13 официально выпущен с улучшенной производительностью
- 🤖 OpenAI анонсировала GPT-5
- 📱 Apple vs EU: новые требования по interoperability
        """,
        "Crypto News": """
- 📈 Bitcoin волатильность: цена колебалась между $43K и $46K
- ⚠️ SEC предупреждение о новой схеме мошенничества
- 🔐 Ethereum upgrade успешно завершен
        """,
    }

    messages_by_channel: dict[str, list] = {"TechCrunch": [], "Crypto News": []}

    digest = formatter.create_digest(
        overview=overview,
        channel_summaries=channel_summaries,
        messages_by_channel=messages_by_channel,
        hours=24,
    )

    print(digest)


if __name__ == "__main__":
    main()
