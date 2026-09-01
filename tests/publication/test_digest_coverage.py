"""Tests for canonical DigestCoverageTrace models and audit."""

from __future__ import annotations

import datetime as dt

import pytest

from src.editorial_models import StoryCard
from src.publication.digest_coverage import build_digest_coverage_trace
from src.publication.digest_narrative import (
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    build_deterministic_digest_draft,
)
from src.publication.digest_presentation import (
    CitySituationPresentationGroup,
    CitySituationPresentationPlan,
    DigestPresentationPlan,
    DigestStoryPresentation,
)
from src.publication.errors import DigestCoverageInvariantError
from src.publication.evidence import PublicationEvidence


def test_digest_coverage_trace_reader_visible_texts() -> None:
    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(),
            covered_source_refs=(),
        ),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:charging",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("sup:charging:1",),
                merge_group_id="story:charging",
            ),
        ),
    )
    final_draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:society:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Бесплатная зарядка",
                        body="На Гагарина, 1 жители могут бесплатно зарядить телефоны.",
                        covered_story_ids=("story:charging",),
                        cited_support_ids=("sup:charging:1",),
                    ),
                ),
            ),
        )
    )

    trace = build_digest_coverage_trace(plan, final_draft)
    story = trace.stories[0]
    assert story.detail_texts == (
        "Бесплатная зарядка: На Гагарина, 1 жители могут бесплатно зарядить телефоны.",
    )
    assert trace.to_dict()[0]["detail_texts"] == [
        "Бесплатная зарядка: На Гагарина, 1 жители могут бесплатно зарядить телефоны.",
    ]


def test_build_digest_coverage_trace_for_ai_draft() -> None:
    sit_group = CitySituationPresentationGroup(
        group_id="sit:1",
        group_kind="subject_status",
        subject_key="power",
        subject_label="Электросеть",
        state="UNAVAILABLE",
        source_refs=("ref-1",),
        detail_lines=("Света нет",),
        covered_story_ids=("story:1", "story:3"),
        cited_support_ids=("sup:1:dash", "sup:3:dash"),
    )

    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(sit_group,),
            covered_source_refs=("ref-1",),
        ),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DASHBOARD_ONLY",
                city_situation_group_ids=("sit:1",),
                detail_support_ids=(),
                merge_group_id="story:1",
            ),
            DigestStoryPresentation(
                story_id="story:2",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("sup:2:detail",),
                merge_group_id="story:2",
            ),
            DigestStoryPresentation(
                story_id="story:3",
                mode="DASHBOARD_AND_DRILLDOWN",
                city_situation_group_ids=("sit:1",),
                detail_support_ids=("sup:3:detail",),
                merge_group_id="story:3",
            ),
        ),
    )

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:society:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Спорт",
                        body="Набор детей.",
                        covered_story_ids=("story:2",),
                        cited_support_ids=("sup:2:detail",),
                    ),
                ),
            ),
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Водоснабжение",
                        body="Жильцы скинулись.",
                        covered_story_ids=("story:3",),
                        cited_support_ids=("sup:3:detail",),
                    ),
                ),
            ),
        )
    )

    trace = build_digest_coverage_trace(plan, draft)
    assert set(trace.story_ids) == {"story:1", "story:2", "story:3"}
    assert trace.story_coverage == 1.0

    by_sid = {s.story_id: s for s in trace.stories}
    assert by_sid["story:1"].mode == "DASHBOARD_ONLY"
    assert by_sid["story:1"].city_situation_group_ids == ("sit:1",)
    assert by_sid["story:1"].dashboard_support_ids == ("sup:1:dash", "sup:3:dash")
    assert by_sid["story:1"].detail_item_ids == ()

    assert by_sid["story:2"].mode == "DETAIL_ONLY"
    assert by_sid["story:2"].detail_item_ids == ("block:society:0:item:0",)
    assert by_sid["story:2"].detail_support_ids == ("sup:2:detail",)

    assert by_sid["story:3"].mode == "DASHBOARD_AND_DRILLDOWN"
    assert by_sid["story:3"].city_situation_group_ids == ("sit:1",)
    assert by_sid["story:3"].detail_item_ids == ("block:utilities:0:item:0",)


def test_build_digest_coverage_trace_for_deterministic_draft() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    card_1 = StoryCard(
        id="story:1", topic="Свет", importance="high", summary="Нет", rubric_id="utilities"
    )
    card_2 = StoryCard(
        id="story:2", topic="Спорт", importance="medium", summary="Футбол", rubric_id="society"
    )
    card_3 = StoryCard(
        id="story:3", topic="Вода", importance="high", summary="Вода", rubric_id="utilities"
    )

    evidence_dict = {
        "sup:1:dash": PublicationEvidence(
            evidence_id="sup:1:dash",
            story_id=1,
            text="Света нет",
            source_text="Света нет",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=1,
            source_ref="ref-1",
            source_id=1,
            source_item_id=1,
            source_role="official",
            observed_at=now,
        ),
        "sup:2:detail": PublicationEvidence(
            evidence_id="sup:2:detail",
            story_id=2,
            text="Набор на футбол",
            source_text="Набор на футбол",
            kind="community_report",
            publication_use="PUBLISH",
            fragment_id=2,
            source_ref="ref-2",
            source_id=2,
            source_item_id=2,
            source_role="community",
            observed_at=now,
        ),
        "sup:3:dash": PublicationEvidence(
            evidence_id="sup:3:dash",
            story_id=3,
            text="Воды нет",
            source_text="Воды нет",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=3,
            source_ref="ref-3",
            source_id=3,
            source_item_id=3,
            source_role="official",
            observed_at=now,
        ),
        "sup:3:detail": PublicationEvidence(
            evidence_id="sup:3:detail",
            story_id=3,
            text="Скинулись на воду",
            source_text="Скинулись на воду",
            kind="community_report",
            publication_use="PUBLISH",
            fragment_id=4,
            source_ref="ref-4",
            source_id=3,
            source_item_id=4,
            source_role="community",
            observed_at=now,
        ),
    }

    sit_group = CitySituationPresentationGroup(
        group_id="sit:1",
        group_kind="subject_status",
        subject_key="power",
        subject_label="Электросеть",
        state="UNAVAILABLE",
        source_refs=("ref-1", "ref-3"),
        detail_lines=("Света нет", "Воды нет"),
        covered_story_ids=("story:1", "story:3"),
        cited_support_ids=("sup:1:dash", "sup:3:dash"),
    )

    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(sit_group,),
            covered_source_refs=("ref-1", "ref-3"),
        ),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DASHBOARD_ONLY",
                city_situation_group_ids=("sit:1",),
                detail_support_ids=(),
                merge_group_id="story:1",
            ),
            DigestStoryPresentation(
                story_id="story:2",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("sup:2:detail",),
                merge_group_id="story:2",
            ),
            DigestStoryPresentation(
                story_id="story:3",
                mode="DASHBOARD_AND_DRILLDOWN",
                city_situation_group_ids=("sit:1",),
                detail_support_ids=("sup:3:detail",),
                merge_group_id="story:3",
            ),
        ),
    )

    rubrics = [
        {"id": "utilities", "name": "ЖКХ"},
        {"id": "society", "name": "Общество"},
    ]

    deterministic_draft = build_deterministic_digest_draft(
        cards=[card_1, card_2, card_3],
        evidence=evidence_dict,
        rubrics=rubrics,
        presentation_plan=plan,
    )

    trace = build_digest_coverage_trace(plan, deterministic_draft)
    assert trace.story_coverage == 1.0
    assert set(trace.story_ids) == {"story:1", "story:2", "story:3"}


def test_build_digest_coverage_trace_raises_invariant_on_missing_coverage() -> None:
    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("sup:1",),
                merge_group_id="story:1",
            ),
        ),
    )

    empty_draft = DigestNarrativeDraft(blocks=())

    with pytest.raises(DigestCoverageInvariantError, match="missing detail coverage for story:1"):
        build_digest_coverage_trace(plan, empty_draft)


def test_build_digest_coverage_trace_merged_item_per_story_provenance() -> None:
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        DigestNarrativePlan,
    )

    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DETAIL_ONLY",
                detail_support_ids=("sup:1",),
                merge_group_id="merge:1",
            ),
            DigestStoryPresentation(
                story_id="story:2",
                mode="DETAIL_ONLY",
                detail_support_ids=("sup:2",),
                merge_group_id="merge:1",
            ),
        ),
    )

    narrative_plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ",
                story_ids=("story:1", "story:2"),
                support_ids=("sup:1", "sup:2"),
                canonical_notes=(),
                required_story_groups=(("story:1", "story:2"),),
                support_ids_by_story=(
                    ("story:1", ("sup:1",)),
                    ("story:2", ("sup:2",)),
                ),
            ),
        )
    )

    # Merged item covering story:1 and story:2 citing both supports
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Merged Headline",
                        body="Merged Body",
                        covered_story_ids=("story:1", "story:2"),
                        cited_support_ids=("sup:1", "sup:2"),
                    ),
                ),
            ),
        )
    )

    trace = build_digest_coverage_trace(plan, draft, narrative_plan)
    trace_by_story = {s.story_id: s for s in trace.stories}

    assert trace_by_story["story:1"].detail_support_ids == ("sup:1",)
    assert trace_by_story["story:2"].detail_support_ids == ("sup:2",)
