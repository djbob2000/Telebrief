"""Prepare a complete, conservative source bundle for editorial analysis."""

from __future__ import annotations

import re
import unicodedata

from src.collector import Message
from src.config_loader import SourceRoleResolver
from src.editorial_models import PreparedBundle, SourceRecord

_EMOJI_OR_SPACE = re.compile(r"[\W_]+", re.UNICODE)
_CURRENCY = re.compile(r"(?:курс|обмен|валют|доллар|евро|гривн|usd|eur|uah)", re.IGNORECASE)
_COMMERCIAL = re.compile(
    r"(?:реклама|продам|куплю|аренда|сдам|сниму|акция|услуги|доставка|звонить|карта\s*\d)",
    re.IGNORECASE,
)


class EditorialInputBuilder:
    """Build source records and prompt text without imposing a fixed message cap."""

    def __init__(self, role_resolver: SourceRoleResolver):
        self.role_resolver = role_resolver

    def build(
        self,
        messages_by_channel: dict[str, list[Message]],
        *,
        max_chars: int | None = None,
    ) -> PreparedBundle:
        ordered = self._ordered_messages(messages_by_channel)
        total_messages = len(ordered)
        ref_by_message: dict[tuple[str, int], str] = {}
        for index, message in enumerate(ordered, start=1):
            if message.message_id is not None:
                ref_by_message[(message.channel_name, message.message_id)] = f"S{index:06d}"

        records: dict[str, SourceRecord] = {}
        for index, message in enumerate(ordered, start=1):
            if self._is_noise(message.text):
                continue
            ref = f"S{index:06d}"
            parent_ref = None
            if message.reply_to_id is not None:
                parent_ref = ref_by_message.get((message.channel_name, message.reply_to_id))
            context_text = self._context_for(message, parent_ref, records)
            records[ref] = SourceRecord(
                ref=ref,
                message=message,
                source_type=self.role_resolver.resolve(message.channel_name, message.topic_id),
                parent_ref=parent_ref,
                context_text=context_text,
            )

        prompt_text = self._render_prompt(records)
        if max_chars is not None and len(prompt_text) > max_chars:
            prompt_text = prompt_text[:max_chars]
        return PreparedBundle(
            records=records,
            prompt_text=prompt_text,
            total_messages=total_messages,
            candidate_count=len(records),
        )

    @staticmethod
    def _ordered_messages(messages_by_channel: dict[str, list[Message]]) -> list[Message]:
        messages: list[Message] = []
        for channel_messages in messages_by_channel.values():
            messages.extend(channel_messages)
        return sorted(
            messages, key=lambda item: (item.timestamp, item.channel_name, item.message_id or 0)
        )

    @staticmethod
    def _is_noise(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        letters_or_numbers = "".join(
            char for char in stripped if unicodedata.category(char)[0] in {"L", "N"}
        )
        if not letters_or_numbers:
            return True
        if _CURRENCY.search(stripped) and len(stripped) < 180:
            return True
        return bool(_COMMERCIAL.search(stripped) and len(stripped) < 220)

    @staticmethod
    def _context_for(
        message: Message, parent_ref: str | None, records: dict[str, SourceRecord]
    ) -> str:
        if parent_ref and parent_ref in records:
            parent = records[parent_ref].message.text
            if len(message.text.strip()) < 120 or message.reply_to_id is not None:
                return f'reply_to: "{parent}"'
        return ""

    @staticmethod
    def _render_prompt(records: dict[str, SourceRecord]) -> str:
        blocks: list[str] = []
        for ref, record in records.items():
            message = record.message
            lines = [
                f"[{ref}] source_type={record.source_type} channel={message.channel_name}",
                f"time={message.timestamp.isoformat()} sender={message.sender}",
                f"text: {message.text}",
            ]
            if record.context_text:
                lines.append(record.context_text)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
