"""Digest renderers generating formatted digests from frozen Story Cards and inputs (Plan 4 Task 6)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from src.publication.editorial_adapter import FrozenEditorialInput

logger = logging.getLogger(__name__)


class PublicationDigestRenderer:
    """Formats frozen editorial Story Cards into channel or grouped digests."""

    def __init__(
        self,
        *,
        output_language: str = "Russian",
        use_emojis: bool = True,
        include_statistics: bool = True,
    ) -> None:
        self.output_language = output_language
        self.use_emojis = use_emojis
        self.include_statistics = include_statistics

    def render_grouped_digest(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
    ) -> tuple[str, str, str]:
        date_str = (snapshot_at or dt.datetime.now(dt.timezone.utc)).strftime("%d.%m.%Y")
        emoji_prefix = "🏙 " if self.use_emojis else ""
        title = f"{emoji_prefix}Дайджест: {edition_name} ({date_str})"

        cards = frozen_input.analysis.cards
        if not cards:
            body = f"# {title}\n\nНет актуальных событий за отчетный период."
            return title, "", body

        # Group cards by topic
        groups: dict[str, list[Any]] = {}
        for card in cards:
            topic = card.topic or "Городские новости"
            groups.setdefault(topic, []).append(card)

        sections: list[str] = []
        lead_summary = cards[0].summary if cards else ""

        for topic, topic_cards in groups.items():
            topic_emoji = "📌 " if self.use_emojis else ""
            topic_section = [f"### {topic_emoji}{topic}"]
            for card in topic_cards:
                bullet = "• "
                summary_text = card.summary.strip()
                facts_text = " ".join(f.text for f in card.hard_facts if f.text != summary_text)
                full_item = f"{summary_text}"
                if facts_text:
                    full_item += f" — {facts_text}"
                if card.representative_source_refs:
                    refs_formatted = ", ".join(card.representative_source_refs)
                    full_item += f" [🔗 {refs_formatted}]"
                topic_section.append(f"{bullet}{full_item}")
            sections.append("\n".join(topic_section))

        body_parts = [f"# {title}", "", lead_summary, ""]
        body_parts.extend(sections)

        if self.include_statistics:
            stat_emoji = "📊 " if self.use_emojis else ""
            body_parts.extend(
                [
                    "",
                    f"*{stat_emoji}Статистика: {len(cards)} тем(ы), {len(frozen_input.writer_bundle.records)} источников.*",
                ]
            )

        body = "\n".join(body_parts).strip()
        return title, lead_summary, body

    def render_channel_digest(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
    ) -> tuple[str, str, str]:
        date_str = (snapshot_at or dt.datetime.now(dt.timezone.utc)).strftime("%d.%m.%Y")
        emoji_prefix = "📢 " if self.use_emojis else ""
        title = f"{emoji_prefix}Сводка каналов: {edition_name} ({date_str})"

        cards = frozen_input.analysis.cards
        if not cards:
            body = f"# {title}\n\nНет сообщений за отчетный период."
            return title, "", body

        sections: list[str] = []
        lead_summary = cards[0].summary if cards else ""

        for idx, card in enumerate(cards, start=1):
            bullet = f"{idx}. "
            text = card.summary.strip()
            if card.hard_facts:
                detail = " ".join(f.text for f in card.hard_facts if f.text != text)
                if detail:
                    text += f"\n   {detail}"
            if card.community_observations:
                obs = " ".join(o.text for o in card.community_observations)
                if obs:
                    text += f"\n   *Сообщения жителей: {obs}*"
            sections.append(f"{bullet}{text}")

        body_parts = [f"# {title}", "", "\n\n".join(sections)]
        if self.include_statistics:
            stat_emoji = "📊 " if self.use_emojis else ""
            body_parts.extend(
                [
                    "",
                    f"*{stat_emoji}Всего событий: {len(cards)}.*",
                ]
            )

        body = "\n".join(body_parts).strip()
        return title, lead_summary, body
