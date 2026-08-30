from __future__ import annotations

import datetime as dt

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
)
from src.publication.article_coverage_diagnostics import (
    diagnose_article_coverage,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft


def _make_dummy_draft(
    cited_ids: tuple[str, ...],
    text_content: str = "Обычный текст статьи без контактов.",
) -> StructuredArticleDraft:
    p_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids),) if cited_ids else ()
    )
    p = ArticleParagraph(text=text_content, cited_support_ids=cited_ids, claims=p_claims)
    sec = ArticleSection(
        heading="Раздел",
        heading_support_ids=cited_ids[:1],
        paragraphs=(p,),
    )
    words = len(f"Заголовок Лид статьи Раздел {text_content}".split())
    t_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids[:1]),) if cited_ids else ()
    )
    l_claims = (
        (ArticleClaimAtom(text=text_content, cited_support_ids=cited_ids[:1]),) if cited_ids else ()
    )
    return StructuredArticleDraft(
        title=text_content,
        title_support_ids=cited_ids[:1],
        title_claims=t_claims,
        lead=text_content,
        lead_support_ids=cited_ids[:1],
        lead_claims=l_claims,
        sections=(sec,),
        word_count=words,
    )


def test_diagnose_article_coverage_prominence_and_detail():
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=("story:power:1", "story:power:2"),
                detail_support_ids=("story:power:1", "story:power:2"),
            ),
            ArticleStoryCoverage(
                story_id="story:telecom",
                topic="Связь",
                rank=2,
                prominence="WEAVE",
                support_ids=("story:telecom:1", "story:telecom:2"),
                detail_support_ids=("story:telecom:1",),
            ),
            ArticleStoryCoverage(
                story_id="story:sport",
                topic="Спорт",
                rank=3,
                prominence="BRIEF",
                support_ids=("story:sport:1",),
                detail_support_ids=("story:sport:1",),
            ),
        )
    )

    # Draft cites power:1 (DEVELOP) and sport:1 (BRIEF); telecom (WEAVE) is omitted
    draft = _make_dummy_draft(cited_ids=("story:power:1", "story:sport:1"))

    diag = diagnose_article_coverage(draft, plan)
    assert diag.planned_story_count == 3
    assert diag.covered_story_count == 2
    assert diag.uncovered_story_ids == ("story:telecom",)
    assert diag.develop_story_coverage == 1.0
    assert diag.weave_story_coverage == 0.0
    assert diag.brief_story_coverage == 1.0

    # Detail supports: planned = power:1, power:2, telecom:1, sport:1 (4 total)
    # covered = power:1, sport:1 (2 total)
    assert diag.planned_detail_support_count == 4
    assert diag.covered_detail_support_count == 2
    assert diag.detail_support_coverage == 0.5
    assert set(diag.uncovered_detail_support_ids) == {"story:power:2", "story:telecom:1"}


def test_diagnose_article_coverage_detects_contact_leaks():
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:route",
                topic="Транспорт",
                rank=1,
                prominence="BRIEF",
                support_ids=("story:route:1",),
            ),
        )
    )
    draft = _make_dummy_draft(
        cited_ids=("story:route:1",),
        text_content="Автобус ходит каждый день. Звоните +79901234567 или переходите на https://example.com",
    )
    diag = diagnose_article_coverage(draft, plan)
    assert len(diag.leaked_contact_payloads) >= 2
    assert any("+79901234567" in leak for leak in diag.leaked_contact_payloads)
    assert any("https://example.com" in leak for leak in diag.leaked_contact_payloads)


def test_diagnostics_are_non_blocking_on_validation():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s1 = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="Света нет",
        source_text="Света нет",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        story_id="story:power",
    )
    s2 = ArticleSupport(
        support_id="story:power:evidence:1:frag:2",
        text="Генератор 300 рублей",
        source_text="Генератор 300 рублей",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        story_id="story:power",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Свет",),
        support_index=(s1, s2),
        support_by_id={s.support_id: s for s in (s1, s2)},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(s1.support_id, s2.support_id),
                detail_support_ids=(s1.support_id, s2.support_id),
            ),
        )
    )
    # Draft only cites s1, omitting detail s2
    draft = _make_dummy_draft(
        cited_ids=(s1.support_id,),
        text_content="Света нет",
    )

    validation = validate_article_draft(
        draft,
        context,
        PublicationEditorialConfig(article_min_sections=1, article_min_words=5),
    )
    assert validation.is_valid is True

    diag = diagnose_article_coverage(draft, plan)
    assert diag.detail_support_coverage < 1.0
