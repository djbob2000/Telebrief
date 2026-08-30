"""Tests for deterministic digest narrative block planner, models, and validation."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import DigestRubricConfig
from src.editorial_models import StoryCard, StoryElement
from src.publication.digest_narrative import (
    DigestNarrativePlan,
    plan_digest_narrative_blocks,
)
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)

_RUBRIC_UTIL = DigestRubricConfig(id="utilities", name="ЖКХ и город", description="ЖКХ", emoji="⚡")
_RUBRIC_TRANS = DigestRubricConfig(
    id="transport", name="Транспорт", description="Транспорт", emoji="🚌"
)
_RUBRIC_OTHER = DigestRubricConfig(
    id="other", name="Другое", description="Разное", emoji="📌", fallback=True
)


def _make_evidence(eid: str, sid: int, text: str) -> PublicationEvidence:
    return PublicationEvidence(
        evidence_id=eid,
        story_id=sid,
        text=text,
        source_text=text,
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=100 + sid,
        source_ref=f"ref-{sid}",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_NOW,
    )


def test_plan_digest_narrative_blocks_single_rubric_under_limit():
    rubrics = [_RUBRIC_UTIL, _RUBRIC_TRANS, _RUBRIC_OTHER]
    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Авария на водоводе {i}",
            importance="high",
            summary=f"Ремонт трубы {i}",
            rubric_id="utilities",
            hard_facts=[
                StoryElement(text=f"Факт {i}", source_refs=[f"ref-{i}"], status="established")
            ],
        )
        for i in range(1, 6)
    ]
    evidence_map = {
        f"story:{i}:evidence:0:frag:{100+i}": _make_evidence(
            f"story:{i}:evidence:0:frag:{100+i}", i, f"Факт {i}"
        )
        for i in range(1, 6)
    }

    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence_map,
        rubrics=rubrics,
        max_cards_per_block=6,
    )

    assert isinstance(plan, DigestNarrativePlan)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    assert block.block_id == "block:utilities:0"
    assert block.rubric_id == "utilities"
    assert block.rubric_title == "ЖКХ и город"
    assert len(block.story_ids) == 5
    assert len(block.canonical_notes) >= 5


def test_plan_digest_narrative_blocks_splits_by_max_bound():
    rubrics = [_RUBRIC_UTIL, _RUBRIC_OTHER]
    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Событие {i}",
            importance="medium",
            summary=f"Сводка {i}",
            rubric_id="utilities",
        )
        for i in range(1, 8)
    ]
    evidence_map = {}

    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence_map,
        rubrics=rubrics,
        max_cards_per_block=6,
    )

    assert len(plan.blocks) == 2
    assert plan.blocks[0].block_id == "block:utilities:0"
    assert len(plan.blocks[0].story_ids) == 6
    assert plan.blocks[1].block_id == "block:utilities:1"
    assert len(plan.blocks[1].story_ids) == 1


def test_plan_digest_narrative_blocks_multiple_rubrics_preserves_order():
    rubrics = [_RUBRIC_UTIL, _RUBRIC_TRANS, _RUBRIC_OTHER]
    cards = [
        StoryCard(
            id="story:1", topic="Вода", importance="high", summary="Вода", rubric_id="utilities"
        ),
        StoryCard(
            id="story:2",
            topic="Автобусы",
            importance="medium",
            summary="Автобусы",
            rubric_id="transport",
        ),
        StoryCard(
            id="story:3", topic="Свет", importance="high", summary="Свет", rubric_id="utilities"
        ),
        StoryCard(
            id="story:4",
            topic="Маршрутка",
            importance="low",
            summary="Маршрутка",
            rubric_id="transport",
        ),
    ]
    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence={},
        rubrics=rubrics,
        max_cards_per_block=6,
    )

    assert len(plan.blocks) == 2
    assert plan.blocks[0].rubric_id == "utilities"
    assert plan.blocks[0].story_ids == ("story:1", "story:3")
    assert plan.blocks[1].rubric_id == "transport"
    assert plan.blocks[1].story_ids == ("story:2", "story:4")


def test_plan_digest_narrative_blocks_empty():
    plan = plan_digest_narrative_blocks(
        cards=[],
        evidence={},
        rubrics=[_RUBRIC_OTHER],
        max_cards_per_block=6,
    )
    assert len(plan.blocks) == 0


def test_digest_narrative_draft_parser_valid():
    from src.publication.digest_narrative import (
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        DigestNarrativeParagraph,
    )

    data = {
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "heading": "Городское хозяйство",
                "paragraphs": [
                    {
                        "text": "В центральной части города устранили аварию на водоводе.",
                        "cited_support_ids": ["sup:1", "sup:2"],
                        "covered_story_ids": ["story:1", "story:2"],
                    }
                ],
            }
        ]
    }
    draft = DigestNarrativeDraft.from_dict(data)
    assert isinstance(draft, DigestNarrativeDraft)
    assert len(draft.blocks) == 1
    b = draft.blocks[0]
    assert isinstance(b, DigestNarrativeBlockDraft)
    assert b.block_id == "block:utilities:0"
    assert b.heading == "Городское хозяйство"
    assert len(b.paragraphs) == 1
    p = b.paragraphs[0]
    assert isinstance(p, DigestNarrativeParagraph)
    assert p.text == "В центральной части города устранили аварию на водоводе."
    assert p.cited_support_ids == ("sup:1", "sup:2")
    assert p.covered_story_ids == ("story:1", "story:2")


@pytest.mark.parametrize(
    "invalid_data,error",
    [
        ("not_a_dict", "root must be a mapping"),
        ({}, "missing 'blocks' list"),
        ({"blocks": "not_a_list"}, "'blocks' must be a list"),
        ({"blocks": [{"heading": "no id"}]}, "missing or empty 'block_id'"),
        (
            {
                "blocks": [
                    {"block_id": "b1", "paragraphs": [{"text": "t", "cited_support_ids": ["s1"]}]},
                    {"block_id": "b1", "paragraphs": [{"text": "t2", "cited_support_ids": ["s1"]}]},
                ]
            },
            "duplicate block_id",
        ),
        ({"blocks": [{"block_id": "b1", "paragraphs": []}]}, "must contain at least one paragraph"),
        (
            {"blocks": [{"block_id": "b1", "paragraphs": [{"text": ""}]}]},
            "paragraph text cannot be empty",
        ),
    ],
)
def test_digest_narrative_draft_parser_rejections(invalid_data, error):
    from src.publication.digest_narrative import DigestNarrativeDraft

    with pytest.raises(ValueError, match=error):
        DigestNarrativeDraft.from_dict(invalid_data)


def test_validate_digest_narrative_valid():
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        DigestNarrativeParagraph,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ и город",
                story_ids=("story:1", "story:2"),
                support_ids=("sup:1", "sup:2"),
                canonical_notes=("Водоканал: ремонт трубы", "Свет: подстанция"),
            ),
        )
    )

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                heading="Городское хозяйство",
                paragraphs=(
                    DigestNarrativeParagraph(
                        text="В центральной части города устранили аварию на водоводе, тогда как на подстанции продолжается ремонт.",
                        cited_support_ids=("sup:1", "sup:2"),
                        covered_story_ids=("story:1", "story:2"),
                    ),
                ),
            ),
        )
    )

    support_texts = {
        "sup:1": "В центральной части города устранили аварию на водоводе.",
        "sup:2": "На подстанции продолжается ремонт сетей.",
    }

    res = validate_digest_narrative(draft, plan, support_text_by_id=support_texts)
    assert res.is_valid is True
    assert len(res.violations) == 0
    assert len(res.unsupported_claims) == 0


def test_validate_digest_narrative_detects_block_mismatch_and_unsupported_claims():
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        DigestNarrativeParagraph,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ и город",
                story_ids=("story:1", "story:2"),
                support_ids=("sup:1",),
                canonical_notes=(),
            ),
        )
    )

    # 1. Uncovered story:2 + unknown support sup:99 + unsupported concrete number 500
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                heading="Городское хозяйство",
                paragraphs=(
                    DigestNarrativeParagraph(
                        text="Устранили аварию, 500 домов без воды [story:1].",
                        cited_support_ids=("sup:99",),
                        covered_story_ids=("story:1",),
                    ),
                ),
            ),
        )
    )

    support_texts = {"sup:1": "Устранили аварию на водоводе."}

    res = validate_digest_narrative(draft, plan, support_text_by_id=support_texts)
    assert res.is_valid is False
    assert any("UNCOVERED_STORY" in v for v in res.violations)
    assert any("DISALLOWED_SUPPORT_ID" in v for v in res.violations)
    assert any("INTERNAL_LEAKAGE" in v for v in res.violations)
    assert len(res.unsupported_claims) >= 1
