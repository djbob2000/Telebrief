"""Deterministic Event-First article recovery composer for supplement and full fallback."""

from __future__ import annotations

from collections.abc import Sequence

from src.publication.article_claims import find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_writer_context import sanitize_writer_source_text

_SHORT_SECTION_HEADING = "Коротко о других событиях города"
_GENERIC_SECTION_HEADING = "Городские события"

_PROMINENCE_LIMITS: dict[str, int] = {
    "DEVELOP": 3,
    "WEAVE": 2,
    "BRIEF": 1,
}


def _render_support_sentence(support: ArticleSupport) -> str:
    text = sanitize_writer_source_text(support.text or support.source_text).strip()
    if support.evidence_kind in {"community_report", "community_observation", "quote_assertion"}:
        if not text.casefold().startswith(("по сообщениям", "жители сообщают", "по словам")):
            text = f"По сообщениям жителей, {text[:1].lower() + text[1:] if text else text}"
    if support.temporal_role == "HISTORICAL_CONTEXT" and text:
        text = f"Ранее {text[:1].lower() + text[1:]}"
    elif support.temporal_role == "FUTURE_SCHEDULED" and text:
        text = f"Запланировано: {text}"
    return text.rstrip(". ") + "."


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


def _build_story_paragraph(
    supports: Sequence[ArticleSupport],
    origin: str,
) -> ArticleParagraph:
    rendered_pairs: list[tuple[ArticleSupport, str]] = []
    for sup in supports:
        sent = _render_support_sentence(sup)
        if sent:
            rendered_pairs.append((sup, sent))

    if not rendered_pairs:
        return ArticleParagraph(text="", cited_support_ids=(), claims=(), generation_origin=origin)  # type: ignore[arg-type]

    para_text = " ".join(sent for _, sent in rendered_pairs)
    cited_ids = tuple(dict.fromkeys(sup.support_id for sup, _ in rendered_pairs))
    claims = tuple(
        ArticleClaimAtom(text=sent.rstrip("."), cited_support_ids=(sup.support_id,))
        for sup, sent in rendered_pairs
    )
    return ArticleParagraph(
        text=para_text,
        cited_support_ids=cited_ids,
        claims=claims,
        generation_origin=origin,  # type: ignore[arg-type]
    )


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

            para = _build_story_paragraph(story_sups, origin="SUPPLEMENT")
            if not para.text:
                continue

            if story.prominence == "DEVELOP":
                heading = _safe_heading_for_story(story, story_sups)
                sec = ArticleSection(
                    heading=heading,
                    heading_support_ids=para.cited_support_ids,
                    heading_claims=(),
                    paragraphs=(para,),
                    heading_generation_origin="SUPPLEMENT",
                )
                new_sections.append(sec)
            else:
                short_paragraphs.append(para)

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
    ) -> StructuredArticleDraft:
        """Render a complete deterministic Event-First article from the coverage plan."""
        if not plan.stories:
            raise ValueError("cannot render fallback from empty article coverage plan")

        # 1. Title and lead from the first planned story
        first_story = plan.stories[0]
        first_sups = _resolve_story_supports(first_story, context)
        if not first_sups:
            # Try any story with valid supports
            for s in plan.stories:
                sups = _resolve_story_supports(s, context)
                if sups:
                    first_story = s
                    first_sups = sups
                    break
        if not first_sups:
            raise ValueError("cannot render fallback: no planned story supports found in context")

        first_sup = first_sups[0]
        first_sentence = _render_support_sentence(first_sup)

        from src.publication.article_semantic_support import assess_semantic_support

        topic = first_story.topic.strip()
        support_texts = [s.text or s.source_text for s in first_sups if (s.text or s.source_text)]
        semantic_res = assess_semantic_support(topic, support_texts) if topic else None
        if (
            topic
            and semantic_res is not None
            and not semantic_res.blocking_critical_terms
            and not semantic_res.blocking_proper_names
            and not find_unsupported_claims(topic, support_texts)
        ):
            title = topic
        else:
            title = first_sentence.rstrip(".")

        title_support_ids = tuple(s.support_id for s in first_sups)
        title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_support_ids),)

        # Lead from first 1-2 supports of first story (or first two stories)
        lead_sups = list(first_sups[:2])
        if len(lead_sups) < 2 and len(plan.stories) > 1:
            second_sups = _resolve_story_supports(plan.stories[1], context)
            if second_sups:
                lead_sups.append(second_sups[0])

        lead_sentences = [_render_support_sentence(s) for s in lead_sups]
        lead = " ".join(lead_sentences)
        lead_support_ids = tuple(dict.fromkeys(s.support_id for s in lead_sups))
        lead_claims = tuple(
            ArticleClaimAtom(text=sent.rstrip("."), cited_support_ids=(s.support_id,))
            for s, sent in zip(lead_sups, lead_sentences)
        )

        # 2. Sections for all planned stories
        sections: list[ArticleSection] = []
        short_paragraphs: list[ArticleParagraph] = []

        for story in plan.stories:
            story_sups = _resolve_story_supports(story, context)
            if not story_sups:
                continue

            para = _build_story_paragraph(story_sups, origin="FALLBACK")
            if not para.text:
                continue

            if story.prominence == "DEVELOP":
                heading = _safe_heading_for_story(story, story_sups)
                sec = ArticleSection(
                    heading=heading,
                    heading_support_ids=para.cited_support_ids,
                    heading_claims=(),
                    paragraphs=(para,),
                    heading_generation_origin="FALLBACK",
                )
                sections.append(sec)
            else:
                short_paragraphs.append(para)

        if short_paragraphs:
            short_cited = tuple(
                dict.fromkeys(sid for p in short_paragraphs for sid in p.cited_support_ids)
            )
            short_sec = ArticleSection(
                heading=_SHORT_SECTION_HEADING,
                heading_support_ids=short_cited,
                heading_claims=(),
                paragraphs=tuple(short_paragraphs),
                heading_generation_origin="FALLBACK",
            )
            sections.append(short_sec)

        all_text = " ".join([title, lead] + [p.text for s in sections for p in s.paragraphs])
        word_count = len(all_text.split())

        return StructuredArticleDraft(
            title=title,
            title_support_ids=title_support_ids,
            title_claims=title_claims,
            lead=lead,
            lead_support_ids=lead_support_ids,
            lead_claims=lead_claims,
            sections=tuple(sections),
            cited_evidence_ids=(),
            word_count=word_count,
            title_generation_origin="FALLBACK",
            lead_generation_origin="FALLBACK",
        )
