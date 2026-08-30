"""Tests for reader UX narrative quality metrics."""

from __future__ import annotations

from src.publication.article_length import ArticleLengthProfile
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.digest_narrative import (
    DigestEditorialItemDraft,
    DigestNarrativeBlock,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    DigestNarrativePlan,
)
from src.publication.narrative_quality import (
    ArticleReaderMetrics,
    DigestReaderMetrics,
    measure_article_reader_metrics,
    measure_digest_reader_metrics,
)


def test_measure_digest_reader_metrics():
    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:util:0",
                rubric_id="utilities",
                rubric_title="ЖКХ",
                story_ids=("story:1", "story:2", "story:3"),
                support_ids=("sup:1", "sup:2", "sup:3"),
                canonical_notes=(),
            ),
        )
    )

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:util:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Сроки восстановления света неизвестны",
                        body="Жители сообщают о продолжении работ на подстанции.",
                        cited_support_ids=("sup:1", "sup:2"),
                        covered_story_ids=("story:1", "story:2"),
                    ),
                    DigestEditorialItemDraft(
                        headline="Водоканал завершил ремонт",
                        body="Водоснабжение восстановлено во всем микрорайоне.",
                        cited_support_ids=("sup:3",),
                        covered_story_ids=("story:3",),
                    ),
                ),
            ),
        )
    )

    metrics = measure_digest_reader_metrics(draft, plan)
    assert isinstance(metrics, DigestReaderMetrics)
    assert metrics.total_blocks == 1
    assert metrics.total_items == 2
    assert metrics.grouped_item_count == 1
    assert metrics.single_item_count == 1
    assert metrics.giant_paragraph_block_count == 0
    assert metrics.avg_headline_words > 0
    assert metrics.avg_body_words > 0


def test_measure_article_reader_metrics():
    sup_id = "story:1:evidence:0:frag:101"
    section = ArticleSection(
        heading="Энергоснабжение",
        heading_support_ids=(sup_id,),
        heading_claims=(),
        paragraphs=(
            ArticleParagraph(
                text="Ремонтная бригада завершила аварийные работы на сетях центрального района.",
                cited_support_ids=(sup_id,),
                claims=(
                    ArticleClaimAtom(
                        text="Ремонтная бригада завершила аварийные работы",
                        cited_support_ids=(sup_id,),
                    ),
                ),
            ),
        ),
    )
    draft = StructuredArticleDraft(
        title="Обстановка в городе",
        title_support_ids=(sup_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Обстановка в городе",
                cited_support_ids=(sup_id,),
            ),
        ),
        lead="Краткий обзор ситуации в городе.",
        lead_support_ids=(sup_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Краткий обзор ситуации в городе",
                cited_support_ids=(sup_id,),
            ),
        ),
        sections=(section,),
        word_count=450,
    )

    profile = ArticleLengthProfile(
        richness="thin",
        target_min_words=300,
        target_max_words=700,
        target_min_sections=2,
        target_max_sections=3,
        hard_min_words=180,
        hard_max_words=1600,
    )

    metrics = measure_article_reader_metrics(draft, profile, unsupported_claim_count=0)
    assert isinstance(metrics, ArticleReaderMetrics)
    assert metrics.word_count == 450
    assert metrics.section_count == 1
    assert metrics.richness == "thin"
    assert metrics.is_within_target is True
    assert metrics.is_above_hard_floor is True
    assert metrics.unsupported_claim_count == 0
