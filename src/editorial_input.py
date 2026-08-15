"""Prepare a complete, conservative source bundle for editorial analysis."""

from __future__ import annotations

import re
import unicodedata

from src.collector import Message
from src.config_loader import SourceRoleResolver
from src.editorial_models import PreparedBundle, SourceRecord

_EMOJI_OR_SPACE = re.compile(r"[\W_]+", re.UNICODE)
_CURRENCY_ANNOUNCEMENT = re.compile(
    r"(?:курс\s+(?:доллара|евро|гривн)|обмен\s+валют|\b(?:usd|eur|uah)\b)",
    re.IGNORECASE,
)
_FINANCIAL_ACTION = re.compile(r"(?:обналичиван|продаж|покупк)", re.IGNORECASE)
_CURRENCY_UNIT = re.compile(r"(?:евро|доллар|гривн|usd|eur|uah)", re.IGNORECASE)
_RATE_NUMBER = re.compile(r"\b\d{2,}(?:[.,]\d+)?\b")
_EXPLICIT_COMMERCIAL = re.compile(
    r"(?:реклама|продам|куплю|аренда|сдам|сниму|обмен\s+валют|"
    r"курс\s+(?:доллара|евро)|заправк(?:а|и)\s+автокондиционер|"
    r"автокондиционер|банковск(?:ими|их)?\s+карт|оформлен(?:ие|ия)\s+пенси)",
    re.IGNORECASE,
)
_COMMERCIAL_MARKERS = re.compile(
    r"(?:акция|услуг|доставк|обращайт|звоните|пишите|запись\s+по|"
    r"консультац|выгодн(?:ые|о)\s+услов)",
    re.IGNORECASE,
)
_SERVICE_MARKERS = re.compile(
    r"(?:диагностик|ремонт|работаем|телефон|адрес|подробност|личн(?:ые|ых)\s+сообщен)",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?://|www\.|t\.me/)", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_COMMERCIAL_CALL_TO_ACTION = re.compile(
    r"(?:обращайт|звоните|пишите|запись|консультац|доставк|выгодн|работаем)",
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
            source_type = self.role_resolver.resolve(message.channel_name, message.topic_id)
            if self._is_noise(message.text, source_type):
                continue
            ref = f"S{index:06d}"
            parent_ref = None
            if message.reply_to_id is not None:
                parent_ref = ref_by_message.get((message.channel_name, message.reply_to_id))
            context_text = self._context_for(message, parent_ref, records)
            records[ref] = SourceRecord(
                ref=ref,
                message=message,
                source_type=source_type,
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
    def _is_noise(text: str, source_type: str = "mixed") -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        letters_or_numbers = "".join(
            char for char in stripped if unicodedata.category(char)[0] in {"L", "N"}
        )
        if not letters_or_numbers:
            return True
        if _CURRENCY_ANNOUNCEMENT.search(stripped):
            return True
        if EditorialInputBuilder._is_financial_rate_spam(stripped):
            return True
        return EditorialInputBuilder._looks_commercial(stripped, source_type)

    @staticmethod
    def _is_financial_rate_spam(text: str) -> bool:
        compact = re.sub(r"[\W_]+", "", text.lower())
        has_action = bool(_FINANCIAL_ACTION.search(text)) or "обналичиван" in compact
        return bool(has_action and _CURRENCY_UNIT.search(text) and _RATE_NUMBER.search(text))

    @staticmethod
    def _looks_commercial(text: str, source_type: str = "mixed") -> bool:
        """Reject promotional/financial spam without filtering short city observations."""
        if _EXPLICIT_COMMERCIAL.search(text):
            return True
        if source_type == "official":
            return False
        marker_count = len(_COMMERCIAL_MARKERS.findall(text))
        has_contact = bool(_URL.search(text) or _PHONE.search(text))
        has_service = bool(_SERVICE_MARKERS.search(text))
        if marker_count and has_contact:
            return True
        if marker_count >= 2:
            return True
        if has_contact and has_service and _COMMERCIAL_CALL_TO_ACTION.search(text):
            return True
        return False

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
