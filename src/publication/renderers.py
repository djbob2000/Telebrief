"""Digest renderers generating formatted Telegram digests from frozen Story Cards and inputs."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Sequence

from src.config_loader import DigestGroupConfig
from src.editorial_models import StoryCard
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
        custom_rubrics: Sequence[DigestGroupConfig] | None = None,
    ) -> None:
        self.output_language = output_language
        self.use_emojis = use_emojis
        self.include_statistics = include_statistics
        self.rubrics = _build_rubrics_from_config(custom_rubrics)

    def render_grouped_digest(
        self,
        frozen_input: FrozenEditorialInput,
        *,
        edition_name: str = "Бердянск",
        snapshot_at: dt.datetime | None = None,
    ) -> tuple[str, str, str]:
        date_str = (snapshot_at or dt.datetime.now(dt.timezone.utc)).strftime("%d.%m.%Y")
        title = (
            f"Дайджест: {edition_name} · {date_str}" if edition_name else f"Дайджест · {date_str}"
        )

        cards = frozen_input.analysis.cards
        if not cards:
            body = f"*{title}*\n\n📭 Нет актуальных событий за отчетный период."
            return title, "", body

        # Group cards by classified rubric
        grouped_cards: dict[str, list[StoryCard]] = {}
        for card in cards:
            rubric_id = classify_card_rubric(card, self.rubrics)
            grouped_cards.setdefault(rubric_id, []).append(card)

        sections: list[str] = [f"*{title}*"]

        for rubric in self.rubrics:
            rubric_id = rubric["id"]
            if rubric_id not in grouped_cards:
                continue

            rubric_cards = grouped_cards[rubric_id]
            emoji = f"{rubric['emoji']} " if self.use_emojis else ""
            header = f"\n*{emoji}{rubric['title']}*\n"
            sections.append(header)

            for card in rubric_cards:
                bullet_lines: list[str] = []
                summary = card.summary.strip().rstrip(".")

                # Extract clean additional hard facts not already in summary
                extra_facts: list[str] = []
                for fact in card.hard_facts:
                    f_text = fact.text.strip().rstrip(".")
                    if f_text and f_text.lower() not in summary.lower() and len(f_text) > 10:
                        extra_facts.append(f_text)

                # Extract useful details (working hours, contacts, exact addresses)
                extra_details: list[str] = []
                for detail in card.useful_details:
                    d_text = detail.text.strip().rstrip(".")
                    if d_text and d_text.lower() not in summary.lower():
                        extra_details.append(d_text)

                combined_details = " ".join(extra_facts[:2] + extra_details[:2]).strip()
                if combined_details:
                    bullet_text = f"• **{card.topic}**: {summary}. {combined_details}."
                else:
                    bullet_text = f"• **{card.topic}**: {summary}."

                bullet_lines.append(bullet_text)

                # If there are resident observations / quotes, add as a subtle sub-point
                if card.community_observations and len(card.community_observations) <= 2:
                    obs_text = "; ".join(o.text.strip() for o in card.community_observations)
                    if obs_text and obs_text.lower() not in bullet_text.lower():
                        bullet_lines.append(f"  _По сообщениям жителей: {obs_text}_")

                sections.append("\n".join(bullet_lines))

        if self.include_statistics:
            stat_emoji = "📊 " if self.use_emojis else ""
            sections.append(
                f"\n_{stat_emoji}Статистика: обработано {len(frozen_input.writer_bundle.records)} источников, {len(cards)} тем._"
            )

        body = "\n".join(sections).strip()
        lead = cards[0].summary if cards else ""
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
