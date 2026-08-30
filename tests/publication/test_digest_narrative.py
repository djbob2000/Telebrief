"""Tests for deterministic digest narrative block planner, models, and validation."""

from __future__ import annotations

import datetime as dt

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
