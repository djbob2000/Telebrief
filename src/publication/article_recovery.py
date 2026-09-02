"""Deterministic Event-First article recovery composer for supplement and full fallback."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_claims import find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)

ArticleTheme = Literal[
    "infrastructure",
    "mobility",
    "communications",
    "civic_services",
    "city_life",
]

THEME_HEADINGS: dict[str, str] = {
    "infrastructure": "Коммунальная инфраструктура",
    "mobility": "Городской и междугородний транспорт",
    "communications": "Связь и интернет",
    "civic_services": "Социальная сфера и городские службы",
    "city_life": "Городские события",
}

_SHORT_SECTION_HEADING = "Коротко о других событиях города"
_GENERIC_SECTION_HEADING = "Городские события"

_PROMINENCE_LIMITS: dict[str, int] = {
    "DEVELOP": 3,
    "WEAVE": 2,
    "BRIEF": 1,
}


def resolve_article_theme(
    card: StoryCard,
    supports: Sequence[ArticleSupport] = (),
) -> ArticleTheme:
    """Classify a story into one of five canonical thematic sections.

    Mapping:
    - electricity/water/gas/heating/ЖКХ -> infrastructure
    - urban/intercity transport/routes -> mobility
    - internet/mobile/telecom -> communications
    - healthcare/social/humanitarian/education/sport/banking -> civic_services
    - fallback -> city_life
    """
    cat = (card.category or "").casefold().strip()
    tags = [t.casefold().strip() for t in card.tags]
    topic = (card.topic or "").casefold().strip()
    summary = (card.summary or "").casefold().strip()

    sup_texts = [s.text.casefold() for s in supports if s.text]
    sup_ids = [s.support_id.casefold() for s in supports if s.support_id]

    all_tokens = [cat, topic, summary] + tags + sup_texts + sup_ids
    corpus = " ".join(all_tokens)

    # 1. Infrastructure / ЖКХ
    if cat in {
        "utilities",
        "infrastructure",
        "power",
        "water",
        "gas",
        "heating",
        "energy",
        "жкх",
    }:
        return "infrastructure"
    infra_markers = (
        "жкх",
        "электр",
        "свет",
        "вод",
        "газ",
        "отоплен",
        "котельн",
        "энерг",
        "подстанци",
        "водоканал",
        "аварийн",
        "труб",
        "порыв",
        "водовод",
    )
    if any(m in corpus for m in infra_markers) or any(
        "power" in s or "water" in s or "gas" in s or "heating" in s for s in sup_ids
    ):
        return "infrastructure"

    # 2. Mobility / Transport
    if cat in {"transport", "mobility", "traffic"}:
        return "mobility"
    mob_markers = (
        "транспорт",
        "автобус",
        "маршрут",
        "рейс",
        "проезд",
        "дорог",
        "трасс",
        "такси",
        "автовокзал",
        "жд",
        "поезд",
    )
    if any(m in corpus for m in mob_markers):
        return "mobility"

    # 3. Communications / Telecom
    if cat in {"telecom", "communications", "internet"}:
        return "communications"
    comm_markers = (
        "связь",
        "интернет",
        "провайдер",
        "мобильн",
        "роутер",
        "wi-fi",
        "wifi",
        "оператор",
        "телеком",
        "сеть",
    )
    if any(m in corpus for m in comm_markers):
        return "communications"

    # 4. Civic services / Municipal / Social / Banking / Healthcare / Education / Sport
    if cat in {
        "healthcare",
        "social",
        "humanitarian",
        "education",
        "sport",
        "banking",
        "services",
    }:
        return "civic_services"
    civic_markers = (
        "банк",
        "пенсионн",
        "выплат",
        "пособи",
        "соц",
        "гуманитарн",
        "больниц",
        "поликлиник",
        "врач",
        "медицин",
        "школ",
        "детсад",
        "спорт",
        "секци",
        "мфц",
        "паспорт",
        "администраци",
    )
    if any(m in corpus for m in civic_markers):
        return "civic_services"

    # 5. Fallback -> city_life
    return "city_life"


def _normalize_for_dedup(text: str) -> str:
    """Normalize text for conservative exact deduplication."""
    t = unicodedata.normalize("NFC", text).strip().casefold()
    for prefix in (
        "по сообщениям жителей,",
        "по сообщениям жителей",
        "жители сообщают,",
        "жители сообщают",
        "по словам жителей,",
        "по словам жителей",
        "как сообщили,",
        "как сообщили",
        "ранее,",
        "ранее",
        "запланировано:",
        "запланировано",
    ):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def _render_support_sentence(
    support: ArticleSupport,
    already_attributed: bool = False,
) -> tuple[str, bool]:
    """Render a single support into natural reader prose, preventing duplicate attribution."""
    from src.publication.article_writer_context import sanitize_writer_source_text

    raw_text = (support.text or support.source_text).strip()
    text = sanitize_writer_source_text(raw_text).strip()
    is_community = support.evidence_kind in {
        "community_report",
        "community_observation",
        "quote_assertion",
    }
    has_own_attr = text.casefold().startswith(
        ("по сообщениям", "жители сообщают", "по словам", "как сообщают")
    )

    if is_community:
        if not has_own_attr:
            if not already_attributed:
                text = f"По сообщениям жителей, {text[:1].lower() + text[1:] if text else text}"
                new_attributed = True
            else:
                text = f"Также {text[:1].lower() + text[1:] if text else text}"
                new_attributed = True
        else:
            new_attributed = True
    else:
        new_attributed = False

    if (
        support.temporal_role == "HISTORICAL_CONTEXT"
        and text
        and not text.casefold().startswith("ранее")
    ):
        text = f"Ранее {text[:1].lower() + text[1:]}"
    elif (
        support.temporal_role == "FUTURE_SCHEDULED"
        and text
        and not text.casefold().startswith("запланировано")
    ):
        text = f"Запланировано: {text}"

    return text.rstrip(". ") + ".", new_attributed


def _resolve_story_supports(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
) -> tuple[ArticleSupport, ...]:
    support_map = context.support_by_id
    limit = _PROMINENCE_LIMITS.get(story.prominence, 1)

    preferred_ids: list[str] = []
    for sid in story.detail_support_ids:
        if sid not in preferred_ids and sid in support_map:
            preferred_ids.append(sid)
    for sid in story.support_ids:
        if sid not in preferred_ids and sid in support_map:
            preferred_ids.append(sid)

    selected_ids = preferred_ids[:limit]
    return tuple(support_map[sid] for sid in selected_ids if sid in support_map)


def _build_theme_paragraphs(
    supports: Sequence[ArticleSupport],
    origin: str,
) -> tuple[ArticleParagraph, ...]:
    if not supports:
        return ()

    seen_dedup_keys: dict[str, int] = {}
    rendered_sentences: list[str] = []
    claim_atoms: list[ArticleClaimAtom] = []
    para_cited_ids: list[str] = []

    already_attributed = False
    for sup in supports:
        norm_key = _normalize_for_dedup(sup.text or sup.source_text)
        if not norm_key:
            continue

        if norm_key in seen_dedup_keys:
            # Merge support ID into existing claim atom
            idx = seen_dedup_keys[norm_key]
            existing = claim_atoms[idx]
            new_cited = tuple(dict.fromkeys(existing.cited_support_ids + (sup.support_id,)))
            claim_atoms[idx] = ArticleClaimAtom(text=existing.text, cited_support_ids=new_cited)
            if sup.support_id not in para_cited_ids:
                para_cited_ids.append(sup.support_id)
            continue

        sent, already_attributed = _render_support_sentence(
            sup, already_attributed=already_attributed
        )
        if not sent:
            continue

        seen_dedup_keys[norm_key] = len(claim_atoms)
        rendered_sentences.append(sent)
        claim_atoms.append(
            ArticleClaimAtom(text=sent.rstrip("."), cited_support_ids=(sup.support_id,))
        )
        if sup.support_id not in para_cited_ids:
            para_cited_ids.append(sup.support_id)

    if not rendered_sentences:
        return ()

    para = ArticleParagraph(
        text=" ".join(rendered_sentences),
        cited_support_ids=tuple(dict.fromkeys(para_cited_ids)),
        claims=tuple(claim_atoms),
        generation_origin=origin,  # type: ignore[arg-type]
    )
    return (para,)


def _safe_heading_for_story(
    story: ArticleStoryCoverage,
    supports: Sequence[ArticleSupport],
) -> str:
    topic = story.topic.strip()
    if not topic:
        return _GENERIC_SECTION_HEADING

    support_texts = [s.text or s.source_text for s in supports if (s.text or s.source_text)]
    if find_unsupported_claims(topic, support_texts):
        return _GENERIC_SECTION_HEADING
    return topic


class ArticleDeterministicComposer:
    """Deterministic, source-close Event-First article recovery composer."""

    def supplement_safe_draft(
        self,
        draft: StructuredArticleDraft,
        uncovered_story_ids: Sequence[str],
        context: ArticleEditorialContext,
        plan: ArticleCoveragePlan,
    ) -> StructuredArticleDraft:
        """Supplement a safe but incomplete AI draft with deterministic paragraphs."""
        if not uncovered_story_ids:
            return draft

        plan_by_id = plan.by_story_id
        new_sections: list[ArticleSection] = []
        short_paragraphs: list[ArticleParagraph] = []

        for story_id in plan.story_ids:
            if story_id not in uncovered_story_ids:
                continue
            story = plan_by_id.get(story_id)
            if story is None:
                continue

            story_sups = _resolve_story_supports(story, context)
            if not story_sups:
                continue

            paras = _build_theme_paragraphs(story_sups, origin="SUPPLEMENT")
            if not paras:
                continue

            if story.prominence == "DEVELOP":
                heading = _safe_heading_for_story(story, story_sups)
                sec_cited = tuple(dict.fromkeys(sid for p in paras for sid in p.cited_support_ids))
                sec = ArticleSection(
                    heading=heading,
                    heading_support_ids=sec_cited,
                    heading_claims=(),
                    paragraphs=paras,
                    heading_generation_origin="SUPPLEMENT",
                )
                new_sections.append(sec)
            else:
                short_paragraphs.extend(paras)

        if short_paragraphs:
            short_cited = tuple(
                dict.fromkeys(sid for p in short_paragraphs for sid in p.cited_support_ids)
            )
            short_sec = ArticleSection(
                heading=_SHORT_SECTION_HEADING,
                heading_support_ids=short_cited,
                heading_claims=(),
                paragraphs=tuple(short_paragraphs),
                heading_generation_origin="SUPPLEMENT",
            )
            new_sections.append(short_sec)

        combined_sections = draft.sections + tuple(new_sections)
        all_text = " ".join(
            [draft.title, draft.lead] + [p.text for s in combined_sections for p in s.paragraphs]
        )
        word_count = len(all_text.split())

        return StructuredArticleDraft(
            title=draft.title,
            title_support_ids=draft.title_support_ids,
            title_claims=draft.title_claims,
            lead=draft.lead,
            lead_support_ids=draft.lead_support_ids,
            lead_claims=draft.lead_claims,
            sections=combined_sections,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=word_count,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )

    def render_full_fallback(
        self,
        context: ArticleEditorialContext,
        plan: ArticleCoveragePlan,
        *,
        max_sections: int = 8,
    ) -> StructuredArticleDraft:
        """Render a complete deterministic Event-First article from the coverage plan."""
        if not plan.stories:
            raise ValueError("cannot render fallback from empty article coverage plan")

        card_by_id = {c.id: c for c in context.story_cards}
        stories_by_theme: dict[str, list[ArticleStoryCoverage]] = defaultdict(list)
        for story in plan.stories:
            card = card_by_id.get(story.story_id) or StoryCard(
                id=story.story_id, topic=story.topic, importance="medium", summary=story.topic
            )
            sups = _resolve_story_supports(story, context)
            theme = resolve_article_theme(card, sups)
            stories_by_theme[theme].append(story)

        sorted_themes = sorted(
            stories_by_theme.keys(),
            key=lambda t: min(s.rank for s in stories_by_theme[t]),
        )

        if len(sorted_themes) > max_sections:
            kept_themes = sorted_themes[: max(1, max_sections - 1)]
            extra_stories: list[ArticleStoryCoverage] = []
            for t in sorted_themes[max(1, max_sections - 1) :]:
                extra_stories.extend(stories_by_theme[t])
            stories_by_theme["city_life"].extend(extra_stories)
            if "city_life" not in kept_themes:
                kept_themes.append("city_life")
            sorted_themes = kept_themes

        # 1. Title and lead from top stories / thematic axes
        top_story = plan.stories[0]
        top_sups = _resolve_story_supports(top_story, context)
        if not top_sups:
            for s in plan.stories:
                sups = _resolve_story_supports(s, context)
                if sups:
                    top_story = s
                    top_sups = sups
                    break
        if not top_sups:
            raise ValueError("cannot render fallback: no planned story supports found in context")

        top_sup = top_sups[0]
        clean_title = top_sup.text.strip().rstrip(".")
        for prefix in (
            "По сообщениям жителей, ",
            "Жители сообщают, ",
            "По словам жителей, ",
        ):
            if clean_title.startswith(prefix):
                clean_title = clean_title[len(prefix) :]
        title = clean_title
        title_support_ids = (top_sup.support_id,)
        title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_support_ids),)

        lead_sups: list[ArticleSupport] = []
        for t in sorted_themes[: min(3, len(sorted_themes))]:
            t_story = stories_by_theme[t][0]
            t_sups = _resolve_story_supports(t_story, context)
            if t_sups:
                lead_sups.append(t_sups[0])

        if len(lead_sups) < 2 and len(top_sups) > 1:
            lead_sups.append(top_sups[1])

        lead_sentences: list[str] = []
        lead_claims: list[ArticleClaimAtom] = []
        lead_attr = False
        for lead_sup in lead_sups:
            sent, lead_attr = _render_support_sentence(lead_sup, already_attributed=lead_attr)
            lead_sentences.append(sent)
            lead_claims.append(
                ArticleClaimAtom(text=sent.rstrip("."), cited_support_ids=(lead_sup.support_id,))
            )

        lead = " ".join(lead_sentences)
        lead_support_ids = tuple(dict.fromkeys(ls.support_id for ls in lead_sups))

        # 2. Build theme sections
        sections: list[ArticleSection] = []
        for th_key in sorted_themes:
            theme_stories = stories_by_theme[th_key]
            theme_sups: list[ArticleSupport] = []
            for st in theme_stories:
                theme_sups.extend(_resolve_story_supports(st, context))

            paras = _build_theme_paragraphs(theme_sups, origin="FALLBACK")
            if not paras:
                continue

            sec_cited = tuple(dict.fromkeys(sid for p in paras for sid in p.cited_support_ids))
            heading = THEME_HEADINGS.get(th_key, _GENERIC_SECTION_HEADING)
            sections.append(
                ArticleSection(
                    heading=heading,
                    heading_support_ids=sec_cited,
                    heading_claims=(),
                    paragraphs=paras,
                    heading_generation_origin="FALLBACK",
                )
            )

        all_text = " ".join([title, lead] + [p.text for s in sections for p in s.paragraphs])
        word_count = len(all_text.split())

        return StructuredArticleDraft(
            title=title,
            title_support_ids=title_support_ids,
            title_claims=title_claims,
            lead=lead,
            lead_support_ids=lead_support_ids,
            lead_claims=tuple(lead_claims),
            sections=tuple(sections),
            cited_evidence_ids=(),
            word_count=word_count,
            title_generation_origin="FALLBACK",
            lead_generation_origin="FALLBACK",
        )
