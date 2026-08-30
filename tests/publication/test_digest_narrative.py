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
from src.publication.narrative_contract import (
    build_article_narrative_contract,
    build_digest_narrative_contract,
)


def test_narrative_contracts_epistemic_fidelity():
    article_contract = build_article_narrative_contract(output_language="Russian")
    assert "single-source" in article_contract.lower()
    assert "community" in article_contract.lower()
    assert "not a reason to omit" in article_contract.lower()
    assert "do not upgrade" in article_contract.lower()
    assert "preserve source date granularity" in article_contract.lower()
    assert "bare day number" in article_contract.lower()
    assert "do not infer a missing month or year" in article_contract.lower()
    assert "resident questions" in article_contract.lower()
    assert "question_context" in article_contract.lower()
    assert "background context" in article_contract.lower()

    digest_contract = build_digest_narrative_contract(output_language="Russian")
    assert "single-source" in digest_contract.lower()
    assert "community" in digest_contract.lower()


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
        f"story:{i}:evidence:0:frag:{100 + i}": _make_evidence(
            f"story:{i}:evidence:0:frag:{100 + i}", i, f"Факт {i}"
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


def test_plan_digest_narrative_blocks_excludes_context_evidence():
    cards = [
        StoryCard(
            id="story:1",
            topic="Справка",
            importance="medium",
            summary="Вопрос",
            rubric_id="utilities",
        )
    ]
    evi_publish = _make_evidence("story:1:evi:1", 1, "Ремонт завершен")
    evi_context = PublicationEvidence(
        evidence_id="story:1:evi:2",
        story_id=1,
        text="Работает ли учреждение?",
        source_text="Работает ли учреждение?",
        kind="resident_question",
        publication_use="CONTEXT",
        fragment_id=102,
        source_ref="ref-1",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=_NOW,
    )
    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence={"story:1:evi:1": evi_publish, "story:1:evi:2": evi_context},
        rubrics=[_RUBRIC_UTIL],
    )
    assert len(plan.blocks) == 1
    assert "story:1:evi:1" in plan.blocks[0].support_ids
    assert "story:1:evi:2" not in plan.blocks[0].support_ids


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
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
    )

    data = {
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "В центре устранили порыв водовода",
                        "body": "Водоснабжение центральной части города полностью восстановлено к полудню.",
                        "covered_story_ids": ["story:101", "story:102"],
                        "cited_support_ids": ["support:1", "support:2"],
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
    assert len(b.items) == 1
    item = b.items[0]
    assert isinstance(item, DigestEditorialItemDraft)
    assert item.headline == "В центре устранили порыв водовода"
    assert item.body == "Водоснабжение центральной части города полностью восстановлено к полудню."
    assert item.covered_story_ids == ("story:101", "story:102")
    assert item.cited_support_ids == ("support:1", "support:2")


@pytest.mark.parametrize(
    "invalid_data,error",
    [
        ("not_a_dict", "root must be a mapping"),
        ({}, "missing 'blocks' list"),
        ({"blocks": "not_a_list"}, "'blocks' must be a list"),
        ({"blocks": [{"items": []}]}, "missing or empty 'block_id'"),
        (
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "h1",
                                "body": "b1",
                                "covered_story_ids": ["s1"],
                                "cited_support_ids": ["sup1"],
                            }
                        ],
                    },
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "h2",
                                "body": "b2",
                                "covered_story_ids": ["s2"],
                                "cited_support_ids": ["sup2"],
                            }
                        ],
                    },
                ]
            },
            "duplicate block_id",
        ),
        ({"blocks": [{"block_id": "b1", "items": []}]}, "must contain at least one item"),
        (
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "",
                                "body": "b1",
                                "covered_story_ids": ["s1"],
                                "cited_support_ids": ["sup1"],
                            }
                        ],
                    }
                ]
            },
            "digest editorial item requires headline, body, stories and supports",
        ),
        (
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "h1",
                                "body": "",
                                "covered_story_ids": ["s1"],
                                "cited_support_ids": ["sup1"],
                            }
                        ],
                    }
                ]
            },
            "digest editorial item requires headline, body, stories and supports",
        ),
        (
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "h1",
                                "body": "b1",
                                "covered_story_ids": [],
                                "cited_support_ids": ["sup1"],
                            }
                        ],
                    }
                ]
            },
            "digest editorial item requires headline, body, stories and supports",
        ),
        (
            {
                "blocks": [
                    {
                        "block_id": "b1",
                        "items": [
                            {
                                "headline": "h1",
                                "body": "b1",
                                "covered_story_ids": ["s1"],
                                "cited_support_ids": [],
                            }
                        ],
                    }
                ]
            },
            "digest editorial item requires headline, body, stories and supports",
        ),
    ],
)
def test_digest_narrative_draft_parser_rejections(invalid_data, error):
    from src.publication.digest_narrative import DigestNarrativeDraft

    with pytest.raises(ValueError, match=error):
        DigestNarrativeDraft.from_dict(invalid_data)


def test_validate_digest_narrative_valid():
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
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
                items=(
                    DigestEditorialItemDraft(
                        headline="В центре восстановили водоснабжение",
                        body="В центральной части города устранили аварию на водоводе.",
                        cited_support_ids=("sup:1",),
                        covered_story_ids=("story:1",),
                    ),
                    DigestEditorialItemDraft(
                        headline="На подстанции продолжается ремонт",
                        body="На подстанции продолжается ремонт сетей.",
                        cited_support_ids=("sup:2",),
                        covered_story_ids=("story:2",),
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
        DigestEditorialItemDraft,
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
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

    # 1. Uncovered story:2 (STORY_PARTITION_MISMATCH) + disallowed support sup:99 + unsupported concrete number 500
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Устранили аварию в городе",
                        body="Устранили аварию, 500 домов без воды [story:1].",
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
    assert any("STORY_PARTITION_MISMATCH" in v for v in res.violations)
    assert any("SUPPORT_OUTSIDE_BLOCK" in v for v in res.violations)
    assert any("INTERNAL_ID_LEAK" in v for v in res.violations)
    assert len(res.unsupported_claims) >= 1


def test_validate_digest_narrative_duplicate_story_and_length_limits():
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
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

    # Duplicate story:1 in two items, story:2 covered as well, headline > 140 chars
    long_headline = "Очень длинный заголовок новости " * 10
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline=long_headline,
                        body="Устранили аварию на сетях водоснабжения.",
                        cited_support_ids=("sup:1",),
                        covered_story_ids=("story:1",),
                    ),
                    DigestEditorialItemDraft(
                        headline="Второй заголовок",
                        body="Второе сообщение о ремонте сетей водоснабжения.",
                        cited_support_ids=("sup:1",),
                        covered_story_ids=("story:1", "story:2"),
                    ),
                ),
            ),
        )
    )

    support_texts = {"sup:1": "Устранили аварию на сетях водоснабжения."}

    res = validate_digest_narrative(draft, plan, support_text_by_id=support_texts)
    assert res.is_valid is False
    assert any("DUPLICATE_STORY_COVERAGE" in v for v in res.violations)
    assert any("HEADLINE_TOO_LONG" in v for v in res.violations)


def test_validate_digest_narrative_headline_unsupported_claim():
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlock,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ и город",
                story_ids=("story:1",),
                support_ids=("sup:1",),
                canonical_notes=(),
            ),
        )
    )

    # Headline contains unsupported number "3 дня"
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Свет восстановят через 3 дня",
                        body="Жители обсуждают несколько неподтвержденных сроков восстановления.",
                        cited_support_ids=("sup:1",),
                        covered_story_ids=("story:1",),
                    ),
                ),
            ),
        )
    )

    support_texts = {"sup:1": "Жители обсуждают несколько неподтвержденных сроков восстановления."}

    res = validate_digest_narrative(draft, plan, support_text_by_id=support_texts)
    assert res.is_valid is False
    assert any("UNSUPPORTED_CONCRETE_CLAIM" in v for v in res.violations)


@pytest.mark.asyncio
async def test_digest_narrative_writer_single_call_success(mocker):
    import json

    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        DigestNarrativeWriter,
    )

    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ и город",
                story_ids=("story:1",),
                support_ids=("sup:1",),
                canonical_notes=("Водоканал завершил ремонт",),
            ),
        )
    )

    mock_provider = mocker.AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "headline": "Водоканал завершил ремонтные работы",
                            "body": "Водоканал завершил ремонтные работы на сетях водопровода.",
                            "cited_support_ids": ["sup:1"],
                            "covered_story_ids": ["story:1"],
                        }
                    ],
                }
            ]
        }
    )

    writer = DigestNarrativeWriter(provider=mock_provider)
    draft = await writer.generate_narrative_draft(
        plan=plan,
        cards=[],
        evidence={},
        situation_rollup=None,
        language="Russian",
    )

    assert isinstance(draft, DigestNarrativeDraft)
    assert mock_provider.chat_completion.call_count == 1
    assert len(draft.blocks) == 1
    assert draft.blocks[0].block_id == "block:utilities:0"
    assert len(draft.blocks[0].items) == 1
    assert draft.blocks[0].items[0].headline == "Водоканал завершил ремонтные работы"


def test_build_digest_support_text_index():
    from src.publication.digest_narrative import build_digest_support_text_index

    evi = _make_evidence("story:1:evidence:0:frag:101", 1, "Авария на водоводе в центре города")
    card = StoryCard(
        id="story:1",
        topic="Водоснабжение",
        importance="high",
        summary="Ремонт завершен",
        rubric_id="utilities",
        hard_facts=[
            StoryElement(text="Давление восстановлено", source_refs=["ref-1"], status="established")
        ],
    )

    index = build_digest_support_text_index(
        evidence={"story:1:evidence:0:frag:101": evi},
        cards=[card],
    )

    assert "story:1:evidence:0:frag:101" in index
    assert "Авария на водоводе" in index["story:1:evidence:0:frag:101"]
    assert any("Давление восстановлено" in v for v in index.values())


def test_digest_narrative_item_grouping_three_stories():
    from src.publication.digest_narrative import (
        DigestNarrativeDraft,
        build_digest_support_text_index,
        validate_digest_narrative,
    )

    rubrics = [_RUBRIC_UTIL]
    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Авария на электросетях {i}",
            importance="high",
            summary=f"Отключение {i}",
            rubric_id="utilities",
        )
        for i in (101, 102, 103)
    ]
    evi = {
        f"sup:power:{i}": _make_evidence(f"sup:power:{i}", i, f"Отключение света {i}")
        for i in (101, 102, 103)
    }
    plan = plan_digest_narrative_blocks(cards=cards, evidence=evi, rubrics=rubrics)
    assert len(plan.blocks) == 1
    block = plan.blocks[0]

    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "headline": "Подтвержденных сроков восстановления света пока нет",
                        "body": (
                            "Отключение света 101. Отключение света 102. Отключение света 103."
                        ),
                        "covered_story_ids": list(block.story_ids),
                        "cited_support_ids": list(block.support_ids),
                    }
                ],
            }
        ]
    }

    draft = DigestNarrativeDraft.from_dict(raw)
    support_index = build_digest_support_text_index(evidence=evi, cards=cards)
    result = validate_digest_narrative(draft, plan, support_index)
    assert result.is_valid
    assert draft.blocks[0].items[0].covered_story_ids == block.story_ids
