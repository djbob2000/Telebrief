"""Prepare a complete, conservative source bundle for editorial analysis."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

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
_MUTUAL_AID_MARKERS = re.compile(
    r"(?:бесплатно\s+(?:зарядить|набрать|разда)|подвоз\s+(?:питьев|техническ)?воды|"
    r"раздач[аеи]\s+воды|пункт\s+обогрев|помощь\s+сосед|поделит[ьс]ся\s+генератор)",
    re.IGNORECASE,
)


class EditorialInputBuilder:
    """Build source records and prompt text without imposing a fixed message cap."""

    def __init__(
        self,
        role_resolver: SourceRoleResolver,
        city_context_resolver: Any | None = None,
    ):
        self.role_resolver = role_resolver
        self.city_context_resolver = city_context_resolver

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
            city_context = (
                self.city_context_resolver.resolve(message.text)
                if self.city_context_resolver is not None
                else None
            )
            records[ref] = SourceRecord(
                ref=ref,
                message=message,
                source_type=source_type,
                parent_ref=parent_ref,
                context_text=context_text,
                city_context=city_context,
            )

        prompt_text = self.render_records(records)
        if max_chars is not None and len(prompt_text) > max_chars:
            prompt_text = prompt_text[:max_chars]
        return PreparedBundle(
            records=records,
            prompt_text=prompt_text,
            total_messages=total_messages,
            candidate_count=len(records),
        )

    def select_records(
        self,
        bundle: PreparedBundle,
        refs: Iterable[str],
        *,
        max_refs: int = 96,
    ) -> PreparedBundle:
        """Create a compact source bundle from representative Story Card refs."""
        if max_refs < 1:
            raise ValueError("max_refs must be positive")

        selected: dict[str, SourceRecord] = {}
        for ref in refs:
            if ref in selected or ref not in bundle.records or len(selected) >= max_refs:
                continue
            record = bundle.records[ref]
            selected[ref] = record
            if record.parent_ref and record.parent_ref in bundle.records:
                selected.setdefault(record.parent_ref, bundle.records[record.parent_ref])

        return PreparedBundle(
            records=selected,
            prompt_text=self._render_prompt(selected),
            total_messages=bundle.total_messages,
            candidate_count=len(selected),
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
        if _MUTUAL_AID_MARKERS.search(text):
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

    @classmethod
    def render_records(cls, records: dict[str, SourceRecord]) -> str:
        blocks: list[str] = []
        for ref, record in records.items():
            message = record.message
            lines = [
                f"[{ref}] source_type={record.source_type} channel={message.channel_name}",
            ]
            fwd_parts: list[str] = []
            if getattr(message, "forward_origin_name", None):
                fwd_parts.append(f"name={message.forward_origin_name}")
            if getattr(message, "forward_origin_username", None):
                fwd_parts.append(f"username={message.forward_origin_username}")
            if fwd_parts:
                lines.append(f"forward_origin: {', '.join(fwd_parts)}")
            lines.extend(
                [
                    f"time={message.timestamp.isoformat()} sender={message.sender}",
                    f"text: {message.text}",
                ]
            )
            if record.context_text:
                lines.append(record.context_text)
            local_ctx = render_local_context(record.city_context)
            if local_ctx:
                lines.append(local_ctx)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @classmethod
    def _render_prompt(cls, records: dict[str, SourceRecord]) -> str:
        return cls.render_records(records)


def _format_entity_str(entity: Any) -> str:
    if entity.kind == "place":
        parts = []
        if entity.municipal_areas:
            m_areas = ",".join(c.area_id for c in entity.municipal_areas)
            parts.append(f"municipal:{m_areas}")
        if entity.colloquial_area_ids:
            c_areas = ",".join(entity.colloquial_area_ids)
            parts.append(f"colloquial:{c_areas}")
        if parts:
            return f"{entity.entity_id} -> {'; '.join(parts)}"
        return str(entity.entity_id)
    if entity.kind in {"area", "provider", "route"}:
        return f"{entity.kind}:{entity.entity_id}"
    return f"{entity.kind}:{entity.entity_id}"


def render_local_context(annotation: Any | None) -> str | None:
    """Format city context entities into compact prompt line."""
    if not annotation or not getattr(annotation, "entities", None):
        return None
    entity_strs = [_format_entity_str(e) for e in annotation.entities]
    if entity_strs:
        return f"local_context: {'; '.join(entity_strs)}"
    return None
