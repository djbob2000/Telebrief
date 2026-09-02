"""Digest renderers generating formatted Telegram digests from frozen Story Cards and inputs."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Sequence

from src.config_loader import DigestGroupConfig, DigestRubricsConfig
from src.editorial_models import StoryCard
from src.publication.digest_contracts import GENERIC_FALLBACK_TOPICS
from src.publication.editorial_adapter import FrozenEditorialInput

logger = logging.getLogger(__name__)


# Standard category definitions with emojis and keywords
STANDARD_RUBRICS: list[dict[str, Any]] = [
    {
        "id": "utilities",
        "title": "Коммунальная обстановка",
        "emoji": "⚡️",
        "keywords": [
            "свет",
            "вода",
            "газ",
            "отоплен",
            "блэкаут",
            "жкх",
            "подстанци",
            "гидрофор",
            "водопровод",
            "электроэнерг",
            "электричеств",
            "насосн",
            "канализац",
            "сетей",
            "труб",
            "генератор",
        ],
    },
    {
        "id": "security",
        "title": "Безопасность и чрезвычайные ситуации",
        "emoji": "💥",
        "keywords": [
            "бпла",
            "взрыв",
            "обстрел",
            "пво",
            "пожар",
            "хлопк",
            "мошеннич",
            "дрон",
            "сирен",
            "разрушен",
            "осколк",
            "прилет",
        ],
    },
    {
        "id": "transport",
        "title": "Транспорт и дороги",
        "emoji": "🚌",
        "keywords": [
            "автобус",
            "маршрут",
            "дорог",
            "перекрыт",
            "проезд",
            "автовокзал",
            "рейс",
            "транспорт",
            "схем движен",
            "объезд",
            "трасс",
        ],
    },
    {
        "id": "telecom",
        "title": "Связь и интернет",
        "emoji": "📶",
        "keywords": [
            "связь",
            "интернет",
            "оператор",
            "миранд",
            "вышк",
            "роутер",
            "провайдер",
            "покрыти",
            "сигнал",
            "сим-карт",
        ],
    },
    {
        "id": "health",
        "title": "Медицина и здоровье",
        "emoji": "🩸",
        "keywords": [
            "кров",
            "донор",
            "больниц",
            "врач",
            "поликлиник",
            "аптек",
            "медикамент",
            "пациент",
            "здоров",
            "лекарств",
            "прием врачей",
        ],
    },
    {
        "id": "culture",
        "title": "Образование и культура",
        "emoji": "🎨",
        "keywords": [
            "школ",
            "гимнази",
            "учител",
            "педагог",
            "дети",
            "конкурс",
            "stem",
            "художеств",
            "образован",
            "выставк",
            "учащ",
            "учебн",
            "кружк",
        ],
    },
    {
        "id": "social",
        "title": "Социальная помощь и общество",
        "emoji": "🤝",
        "keywords": [
            "гуманитар",
            "подгузник",
            "выплат",
            "пенси",
            "пособи",
            "соц",
            "помощ",
            "льгот",
            "волонтер",
            "нуждающ",
            "поддержк",
        ],
    },
    {
        "id": "contacts",
        "title": "Полезные контакты и объявления",
        "emoji": "📌",
        "keywords": [
            "контакт",
            "горяч лини",
            "телефон",
            "график работ",
            "адрес",
            "режим работ",
            "прием граждан",
        ],
    },
    {
        "id": "other",
        "title": "Другие городские события",
        "emoji": "📋",
        "keywords": [],
    },
]


def _build_rubrics_from_config(
    custom_groups: Sequence[DigestGroupConfig] | None,
) -> list[dict[str, Any]]:
    """Build active rubric list from configuration or defaults."""
    if not custom_groups:
        return list(STANDARD_RUBRICS)

    std_by_name = {r["title"].lower(): r for r in STANDARD_RUBRICS}

    rubrics: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, group in enumerate(custom_groups, start=1):
        name_lower = group.name.lower()
        matched = std_by_name.get(name_lower)
        if not matched:
            for std in STANDARD_RUBRICS:
                if std["id"] in name_lower or any(kw in name_lower for kw in std["keywords"][:3]):
                    matched = std
                    break

        if matched:
            r_id = matched["id"]
            if r_id in seen_ids:
                r_id = f"{r_id}_{idx}"
            seen_ids.add(r_id)
            rubrics.append(
                {
                    "id": r_id,
                    "title": group.name,
                    "emoji": matched["emoji"],
                    "keywords": matched["keywords"],
                }
            )
        else:
            r_id = f"custom_{idx}"
            seen_ids.add(r_id)
            rubrics.append(
                {
                    "id": r_id,
                    "title": group.name,
                    "emoji": "📌",
                    "keywords": [w.lower() for w in group.name.split() if len(w) > 3],
                }
            )

    # Ensure catch-all "other" exists at the end
    if not any(r["id"] == "other" or "друг" in r["title"].lower() for r in rubrics):
        rubrics.append(STANDARD_RUBRICS[-1])

    return rubrics


def classify_card_rubric(card: StoryCard, rubrics: list[dict[str, Any]] | None = None) -> str:
    """Determine the best rubric for a StoryCard based on explicit category or keyword analysis."""
    active_rubrics = rubrics or STANDARD_RUBRICS
    cat = (card.category or "").strip().lower()

    if cat:
        for rubric in active_rubrics:
            if cat == rubric["id"] or cat in rubric["title"].lower():
                return str(rubric["id"])
            if cat in ("utilities", "жкх", "коммуналка") and rubric["id"] == "utilities":
                return "utilities"
            if cat in ("security", "безопасность", "чс") and rubric["id"] == "security":
                return "security"
            if cat in ("transport", "транспорт", "дороги") and rubric["id"] == "transport":
                return "transport"
            if cat in ("telecom", "связь", "интернет") and rubric["id"] == "telecom":
                return "telecom"
            if cat in ("health", "медицина", "здоровье") and rubric["id"] == "health":
                return "health"
            if (
                cat in ("culture", "education", "культура", "образование")
                and rubric["id"] == "culture"
            ):
                return "culture"
            if cat in ("social", "социальная помощь", "общество") and rubric["id"] == "social":
                return "social"
            if cat in ("contacts", "контакты", "объявления") and rubric["id"] == "contacts":
                return "contacts"

    text = f"{card.topic} {card.summary}".lower()
    for rubric in active_rubrics:
        if rubric["id"] == "other":
            continue
        for kw in rubric.get("keywords", []):
            if kw in text:
                return str(rubric["id"])

    return str(active_rubrics[-1]["id"])


def split_into_telegram_chunks(text: str, max_chars: int = 3900) -> list[str]:
    """Split a formatted digest into multiple Telegram messages without breaking markdown sections."""
    if len(text) <= max_chars:
        return [text]

    # Split by section headers (*\n* or \n\n*)
    raw_sections = text.split("\n\n*")
    reconstructed_sections = [raw_sections[0]]
    for sec in raw_sections[1:]:
        reconstructed_sections.append(f"*{sec}")

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for section in reconstructed_sections:
        sec_len = len(section) + 2
        if current_chunk and (current_len + sec_len > max_chars):
            chunks.append("\n\n".join(current_chunk).strip())
            current_chunk = [section]
            current_len = sec_len
        else:
            current_chunk.append(section)
            current_len += sec_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk).strip())

    # If split into multiple parts, add numbering
    if len(chunks) > 1:
        total = len(chunks)
        for idx in range(total):
            chunks[idx] = f"*(Часть {idx + 1}/{total})*\n\n{chunks[idx]}"

    return chunks


class PublicationDigestRenderer:
    """Formats frozen editorial Story Cards into premium structured Telegram digests."""

    def __init__(
        self,
        *,
        output_language: str = "Russian",
        use_emojis: bool = True,
        include_statistics: bool = False,
        rubrics_config: DigestRubricsConfig | None = None,
        custom_rubrics: Sequence[DigestGroupConfig] | None = None,
    ) -> None:
        self.output_language = output_language
        self.use_emojis = use_emojis
        self.include_statistics = include_statistics
        self.rubrics_config = rubrics_config
        if rubrics_config is not None:
            self.rubrics = [
                {
                    "id": r.id,
                    "title": r.name,
                    "emoji": r.emoji,
                    "fallback": r.fallback,
                }
                for r in rubrics_config.items
            ]
        else:
            self.rubrics = _build_rubrics_from_config(custom_rubrics)

    def render_grouped_digest(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
        narrative_draft: Any | None = None,
        presentation_plan: Any | None = None,
    ) -> tuple[str, str, str]:
        date_str = (snapshot_at or dt.datetime.now(dt.timezone.utc)).strftime("%d.%m.%Y")
        title = (
            f"Дайджест: {edition_name} · {date_str}" if edition_name else f"Дайджест · {date_str}"
        )

        cards = frozen_input.analysis.cards
        if not cards:
            body = f"*{title}*\n\n📭 Нет актуальных событий за отчетный период."
            return title, "", body

        sections: list[str] = [f"*{title}*"]

        sit_plan = None
        if presentation_plan is not None:
            sit_plan = getattr(presentation_plan, "city_situation", presentation_plan)

        if sit_plan is not None:
            from src.publication.digest_presentation import render_city_situation_presentation

            sit_text = render_city_situation_presentation(sit_plan, use_emojis=self.use_emojis)
            if sit_text:
                sections.append(sit_text)
        elif narrative_draft is not None and getattr(narrative_draft, "situation_items", None):
            emoji = "🏙 " if self.use_emojis else ""
            sit_lines = [f"*{emoji}Городская обстановка*"]
            for s_item in narrative_draft.situation_items:
                sit_lines.append(f"• **{s_item.label.strip()}**: {s_item.body.strip()}")
            if len(sit_lines) > 1:
                sections.append("\n".join(sit_lines))
        elif frozen_input.analysis.city_situation:
            from src.publication.city_situation import render_city_situation_section

            sit_text = render_city_situation_section(frozen_input.analysis.city_situation)
            if sit_text:
                sections.append(sit_text)

        if narrative_draft is not None and getattr(narrative_draft, "blocks", None):
            current_rubric_id: str | None = None
            current_rubric_items: list[str] = []

            def _flush_rubric_section() -> None:
                if current_rubric_id is not None and current_rubric_items:
                    rubric = next(
                        (r for r in self.rubrics if r.get("id") == current_rubric_id), None
                    )
                    emoji = (
                        f"{rubric['emoji']} "
                        if (rubric and self.use_emojis and rubric.get("emoji"))
                        else ""
                    )
                    heading = rubric["title"] if rubric else "Разное"
                    header = f"*{emoji}{heading}*"
                    sections.append(f"{header}\n" + "\n".join(current_rubric_items))

            for block_draft in narrative_draft.blocks:
                rubric_id = (
                    block_draft.block_id.split(":")[1]
                    if ":" in block_draft.block_id
                    else block_draft.block_id
                )
                item_lines = [
                    f"• **{item.headline.strip()}**: {item.body.strip()}"
                    for item in block_draft.items
                    if item.headline.strip() and item.body.strip()
                ]
                if not item_lines:
                    continue

                if rubric_id != current_rubric_id:
                    _flush_rubric_section()
                    current_rubric_id = rubric_id
                    current_rubric_items = list(item_lines)
                else:
                    current_rubric_items.extend(item_lines)

            _flush_rubric_section()
        else:
            # Group cards by classified rubric
            grouped_cards: dict[str, list[StoryCard]] = {}
            for card in cards:
                if card.rubric_id and any(r["id"] == card.rubric_id for r in self.rubrics):
                    rubric_id = card.rubric_id
                elif self.rubrics_config is not None:
                    rubric_id = self.rubrics_config.fallback.id
                else:
                    rubric_id = classify_card_rubric(card, self.rubrics)
                grouped_cards.setdefault(rubric_id, []).append(card)

            for rubric in self.rubrics:
                rubric_id = str(rubric["id"])
                if rubric_id not in grouped_cards:
                    continue

                rubric_cards = grouped_cards[rubric_id]
                emoji = f"{rubric['emoji']} " if (self.use_emojis and rubric.get("emoji")) else ""
                header = f"*{emoji}{rubric['title']}*"

                rubric_bullets: list[str] = []
                for card in rubric_cards:
                    bullet_lines: list[str] = []
                    topic = card.topic.strip()
                    summary = card.summary.strip().rstrip(".")

                    has_generic_topic = not topic or topic.lower() in {
                        t.lower() for t in GENERIC_FALLBACK_TOPICS
                    }

                    is_tautological_summary = (
                        not summary
                        or summary.lower() == topic.lower()
                        or summary.lower().startswith(topic.lower() + ":")
                        or summary.lower().startswith(topic.lower() + ".")
                        or topic.lower().startswith(summary.lower())
                    )

                    # Extract clean additional hard facts not already in summary/topic
                    extra_facts: list[str] = []
                    base_text_for_dedup = f"{topic} {summary}".lower()
                    for fact in card.hard_facts:
                        f_text = fact.text.strip().rstrip(".")
                        if (
                            f_text
                            and f_text.lower() not in base_text_for_dedup
                            and len(f_text) > 10
                        ):
                            extra_facts.append(f_text)

                    # Extract useful details (working hours, contacts, exact addresses)
                    extra_details: list[str] = []
                    for detail in card.useful_details:
                        d_text = detail.text.strip().rstrip(".")
                        if d_text and d_text.lower() not in base_text_for_dedup:
                            extra_details.append(d_text)

                    details_parts = [p for p in (extra_facts[:2] + extra_details[:2]) if p]
                    combined_details = ". ".join(details_parts).strip()

                    topic_has_attribution = bool(
                        re.search(
                            r"\b(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+жителей)\b",
                            topic,
                            re.IGNORECASE,
                        )
                    )
                    if topic_has_attribution:
                        clean_summary = re.sub(
                            r"^(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+жителей)[,:\s]+",
                            "",
                            summary,
                            flags=re.IGNORECASE,
                        ).strip()
                        if clean_summary:
                            summary = clean_summary[:1].upper() + clean_summary[1:]

                    if has_generic_topic:
                        if not is_tautological_summary:
                            body_text = (
                                f"{summary}. {combined_details}."
                                if combined_details
                                else f"{summary}."
                            )
                        else:
                            body_text = f"{combined_details}." if combined_details else f"{topic}."
                        bullet_text = f"• {body_text}"
                    else:
                        if not is_tautological_summary:
                            if combined_details and combined_details.lower() not in summary.lower():
                                body_text = f"{summary}. {combined_details}."
                            else:
                                body_text = f"{summary}."
                            bullet_text = f"• **{topic}**: {body_text}"
                        else:
                            if combined_details:
                                bullet_text = f"• **{topic}**: {combined_details}."
                            else:
                                bullet_text = f"• **{topic}**"

                    bullet_lines.append(bullet_text)

                    # If there are resident observations / quotes, add as a subtle sub-point
                    if card.community_observations and len(card.community_observations) <= 2:
                        obs_items = []
                        for o in card.community_observations:
                            txt = o.text.strip()
                            if not txt:
                                continue
                            clean_obs = re.sub(
                                r"^(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+жителей)[,:\s]+",
                                "",
                                txt,
                                flags=re.IGNORECASE,
                            ).strip()
                            if (
                                clean_obs
                                and clean_obs.lower() not in summary.lower()
                                and clean_obs.lower() not in combined_details.lower()
                            ):
                                obs_items.append(clean_obs)
                        if obs_items and not topic_has_attribution:
                            obs_text = "; ".join(obs_items)
                            bullet_lines.append(f"  _По сообщениям жителей: {obs_text}_")

                    rubric_bullets.append("\n".join(bullet_lines))

                if rubric_bullets:
                    sections.append(f"{header}\n" + "\n".join(rubric_bullets))

        if self.include_statistics:
            stat_emoji = "📊 " if self.use_emojis else ""
            records = list(frozen_input.writer_bundle.records.values())
            unique_sources = (
                len({r.message.channel_id or r.message.channel_name for r in records})
                if records
                else 0
            )
            total_messages = (
                len({r.message.link or r.ref.split(":frag:")[0] for r in records}) if records else 0
            )
            total_frags = len(records)
            total_events = len(cards)

            if total_frags > 0 and (
                total_frags != total_messages or total_messages != unique_sources
            ):
                stat_text = (
                    f"источников: {unique_sources}, сообщений: {total_messages}, "
                    f"фактов: {total_frags}, событий: {total_events}"
                )
            else:
                stat_text = (
                    f"обработано {len(frozen_input.writer_bundle.records)} источников, "
                    f"{len(cards)} тем"
                )

            sections.append(f"_{stat_emoji}Статистика: {stat_text}._")

        body = "\n\n".join(s.strip() for s in sections if s.strip())
        lead = ""
        return title, lead, body

    def render_grouped_digest_chunks(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
        max_chars: int = 3900,
    ) -> list[str]:
        """Render digest and split into Telegram message chunks if it exceeds max length."""
        _, _, body = self.render_grouped_digest(
            frozen_input, edition_name=edition_name, snapshot_at=snapshot_at
        )
        return split_into_telegram_chunks(body, max_chars=max_chars)

    def render_channel_digest(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
    ) -> tuple[str, str, str]:
        """Backward-compatible channel-bullet renderer."""
        return self.render_grouped_digest(
            frozen_input, edition_name=edition_name, snapshot_at=snapshot_at
        )


def render_layered_short_read_telegram_html(
    *,
    edition_name: str = "Бердянск",
    snapshot_at: dt.datetime | None = None,
    situation_items: Sequence[Any] | None = None,
    situation_plan: Any | None = None,
    rubric_blocks: Sequence[Any] | None = None,
    rubrics_config: Any | None = None,
    statistics_text: str | None = None,
    use_emojis: bool = True,
) -> str:
    """Render high-density layered short-read digest formatted in Telegram HTML."""
    date_str = (snapshot_at or dt.datetime.now(dt.timezone.utc)).strftime("%d.%m.%Y")
    title = f"Дайджест: {edition_name} · {date_str}" if edition_name else f"Дайджест · {date_str}"

    sections: list[str] = [f"<b>{title}</b>"]

    # 1. City Situation Layer
    if situation_items:
        emoji = "🏙 " if use_emojis else ""
        sit_lines = [f"<b>{emoji}Городская обстановка</b>"]
        for s_item in situation_items:
            label = getattr(s_item, "label", "").strip()
            body = getattr(s_item, "body", "").strip()
            if label and body:
                sit_lines.append(f"• <b>{label}</b>: {body}")
        if len(sit_lines) > 1:
            sections.append("\n".join(sit_lines))
    elif situation_plan and getattr(situation_plan, "groups", None):
        emoji = "🏙 " if use_emojis else ""
        sit_lines = [f"<b>{emoji}Городская обстановка</b>"]
        for g in situation_plan.groups:
            label = getattr(g, "subject_label", "").strip()
            details = "; ".join(getattr(g, "detail_lines", ()))
            if label and details:
                sit_lines.append(f"• <b>{label}</b>: {details}")
        if len(sit_lines) > 1:
            sections.append("\n".join(sit_lines))

    # 2. Rubric blocks layer
    if rubric_blocks:
        rubrics_list = []
        if rubrics_config is not None and getattr(rubrics_config, "items", None):
            rubrics_list = [
                {"id": r.id, "title": r.name, "emoji": r.emoji} for r in rubrics_config.items
            ]
        else:
            rubrics_list = STANDARD_RUBRICS

        for block in rubric_blocks:
            rid = getattr(block, "rubric_id", "") or (
                getattr(block, "block_id", "").split(":")[1]
                if ":" in getattr(block, "block_id", "")
                else getattr(block, "block_id", "")
            )
            rubric = next((r for r in rubrics_list if r.get("id") == rid), None)
            emoji = f"{rubric['emoji']} " if (use_emojis and rubric and rubric.get("emoji")) else ""
            title_text = rubric["title"] if rubric else getattr(block, "rubric_title", "Разное")
            header = f"<b>{emoji}{title_text}</b>"

            items = getattr(block, "items", ()) or ()
            item_lines = []
            for it in items:
                hl = getattr(it, "headline", "").strip()
                bd = getattr(it, "body", "").strip()
                if hl and bd:
                    item_lines.append(f"• <b>{hl}</b>: {bd}")
            if item_lines:
                sections.append(f"{header}\n" + "\n".join(item_lines))

    # 3. Statistics footer
    if statistics_text:
        sections.append(f"<i>{statistics_text}</i>")

    return "\n\n".join(sections)
