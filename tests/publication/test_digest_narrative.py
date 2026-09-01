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
    DIGEST_NARRATIVE_PROMPT_VERSION,
    build_article_narrative_contract,
    build_digest_narrative_contract,
)


def test_narrative_contracts_epistemic_fidelity():
    assert DIGEST_NARRATIVE_PROMPT_VERSION == "event-digest-narrative-v5"

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
    assert "city situation" in digest_contract.lower()
    assert "microdetail" in digest_contract.lower()
    assert "fact-first" in digest_contract.lower()
    assert "attribution once" in digest_contract.lower()
    assert "resident questions" in digest_contract.lower()
    assert "not standalone news" in digest_contract.lower()


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


@pytest.mark.asyncio
async def test_digest_narrative_writer_with_situation_plan(mocker):
    import json

    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        DigestNarrativeWriter,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
    )

    sit_plan = CitySituationPresentationPlan(
        groups=(
            CitySituationPresentationGroup(
                group_id="situation:water:avail",
                group_kind="subject_status",
                subject_key="water",
                subject_label="Вода",
                state="UNAVAILABLE",
                source_refs=("ref-w-1",),
                detail_lines=("Центр: нет воды",),
            ),
        ),
        covered_source_refs=("ref-w-1",),
    )

    plan = DigestNarrativePlan(
        blocks=(
            DigestNarrativeBlock(
                block_id="block:utilities:0",
                rubric_id="utilities",
                rubric_title="ЖКХ",
                story_ids=("story:1",),
                support_ids=("sup:1",),
                canonical_notes=(),
                detail_support_ids_by_story=(("story:1", ("sup:1",)),),
                merge_group_by_story=(("story:1", "story:1"),),
            ),
        )
    )

    mock_provider = mocker.AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "situation_items": [
                {
                    "group_id": "situation:water:avail",
                    "label": "Вода",
                    "body": "Центр: нет воды.",
                    "cited_support_ids": ["ref-w-1"],
                }
            ],
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "headline": "Ремонт сетей",
                            "body": "Бригады работают на сетях.",
                            "cited_support_ids": ["sup:1"],
                            "covered_story_ids": ["story:1"],
                        }
                    ],
                }
            ],
        }
    )

    writer = DigestNarrativeWriter(provider=mock_provider)
    draft = await writer.generate_narrative_draft(
        plan=plan,
        cards=[],
        evidence={},
        situation_plan=sit_plan,
        language="Russian",
    )

    assert isinstance(draft, DigestNarrativeDraft)
    assert len(draft.situation_items) == 1
    assert draft.situation_items[0].group_id == "situation:water:avail"

    # Verify user prompt excludes situation_items
    call_args = mock_provider.chat_completion.call_args[1]
    messages = call_args["messages"]
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    user_data = json.loads(user_content)
    assert "situation_items" not in user_data
    assert "blocks" in user_data

    assert draft.blocks[0].block_id == "block:utilities:0"
    assert len(draft.blocks[0].items) == 1
    assert draft.blocks[0].items[0].headline == "Ремонт сетей"


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
            tags=["electricity"],
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


def test_validate_digest_narrative_rejects_unrelated_story_grouping():
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:100", "story:101"),
        support_ids=("sup:100", "sup:101"),
        canonical_notes=(),
        merge_group_by_story=(("story:100", "merge:100"), ("story:101", "merge:101")),
    )
    plan = DigestNarrativePlan(blocks=(block,))

    raw = {
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "Городские новости",
                        "body": "Назначение нового сотрудника и лаборатория проводит анализы.",
                        "covered_story_ids": ["story:100", "story:101"],
                        "cited_support_ids": ["sup:100", "sup:101"],
                    }
                ],
            }
        ]
    }
    draft = DigestNarrativeDraft.from_dict(raw)
    support_index = {
        "sup:100": "Назначение нового сотрудника",
        "sup:101": "Лаборатория проводит анализы",
    }
    result = validate_digest_narrative(draft, plan, support_index)
    assert not result.is_valid
    assert any("UNRELATED_STORY_GROUPING" in v for v in result.violations)


def test_digest_narrative_draft_parser_situation_items() -> None:
    from src.publication.digest_narrative import DigestNarrativeDraft, DigestSituationItemDraft

    data = {
        "situation_items": [
            {
                "group_id": "situation:water_supply:availability",
                "label": "Водоснабжение",
                "body": "Азмол: воды нет третий день; верхние этажи: слабое давление.",
                "cited_support_ids": ["ref-water-1", "ref-water-2"],
            }
        ],
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "Ремонт сетей",
                        "body": "Бригады работают на объектах.",
                        "covered_story_ids": ["story:1"],
                        "cited_support_ids": ["ref-1"],
                    }
                ],
            }
        ],
    }
    draft = DigestNarrativeDraft.from_dict(data)
    assert len(draft.situation_items) == 1
    assert isinstance(draft.situation_items[0], DigestSituationItemDraft)
    assert draft.situation_items[0].group_id == "situation:water_supply:availability"
    assert draft.situation_items[0].label == "Водоснабжение"
    assert "ref-water-1" in draft.situation_items[0].cited_support_ids


def test_digest_narrative_draft_parser_backward_compatible_no_situation() -> None:
    from src.publication.digest_narrative import DigestNarrativeDraft

    data = {
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "Ремонт сетей",
                        "body": "Бригады работают на объектах.",
                        "covered_story_ids": ["story:1"],
                        "cited_support_ids": ["ref-1"],
                    }
                ],
            }
        ]
    }
    draft = DigestNarrativeDraft.from_dict(data)
    assert draft.situation_items == ()


def test_validate_digest_narrative_checks_situation_group_set_mismatch() -> None:
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
    )

    sit_grp_1 = CitySituationPresentationGroup(
        group_id="situation:water:avail",
        group_kind="subject_status",
        subject_key="water",
        subject_label="Вода",
        state="UNAVAILABLE",
        source_refs=("ref-w-1",),
        detail_lines=("Центр: нет воды",),
    )
    sit_grp_2 = CitySituationPresentationGroup(
        group_id="situation:power:avail",
        group_kind="subject_status",
        subject_key="power",
        subject_label="Свет",
        state="DEGRADED",
        source_refs=("ref-p-1",),
        detail_lines=("АКЗ: скачки напряжения",),
    )
    sit_plan = CitySituationPresentationPlan(
        groups=(sit_grp_1, sit_grp_2),
        covered_source_refs=("ref-w-1", "ref-p-1"),
    )

    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:1",),
        support_ids=("ref-1",),
        canonical_notes=(),
    )
    plan = DigestNarrativePlan(blocks=(block,))

    # Draft omits sit_grp_2
    raw = {
        "situation_items": [
            {
                "group_id": "situation:water:avail",
                "label": "Вода",
                "body": "Центр: нет воды.",
                "cited_support_ids": ["ref-w-1"],
            }
        ],
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "Ремонт сетей",
                        "body": "Бригады работают на объектах.",
                        "covered_story_ids": ["story:1"],
                        "cited_support_ids": ["ref-1"],
                    }
                ],
            }
        ],
    }
    draft = DigestNarrativeDraft.from_dict(raw)
    support_index = {
        "ref-w-1": "Центр: нет воды",
        "ref-p-1": "АКЗ: скачки напряжения",
        "ref-1": "Бригады работают на объектах.",
    }
    result = validate_digest_narrative(draft, plan, support_index, situation_plan=sit_plan)
    assert not result.is_valid
    assert any("SITUATION_GROUP_SET_MISMATCH" in v for v in result.violations)


def test_validate_digest_narrative_checks_unsupported_situation_claims() -> None:
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
    )

    sit_grp = CitySituationPresentationGroup(
        group_id="situation:water:avail",
        group_kind="subject_status",
        subject_key="water",
        subject_label="Вода",
        state="UNAVAILABLE",
        source_refs=("ref-w-1",),
        detail_lines=("Центр: нет воды",),
    )
    sit_plan = CitySituationPresentationPlan(
        groups=(sit_grp,),
        covered_source_refs=("ref-w-1",),
    )
    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:1",),
        support_ids=("ref-1",),
        canonical_notes=(),
    )
    plan = DigestNarrativePlan(blocks=(block,))

    # Body claims invented deadline 18:30 not in ref-w-1
    raw = {
        "situation_items": [
            {
                "group_id": "situation:water:avail",
                "label": "Вода",
                "body": "Центр: воды не будет до 18:30.",
                "cited_support_ids": ["ref-w-1"],
            }
        ],
        "blocks": [
            {
                "block_id": "block:utilities:0",
                "items": [
                    {
                        "headline": "Ремонт сетей",
                        "body": "Бригады работают на объектах.",
                        "covered_story_ids": ["story:1"],
                        "cited_support_ids": ["ref-1"],
                    }
                ],
            }
        ],
    }
    draft = DigestNarrativeDraft.from_dict(raw)
    support_index = {
        "ref-w-1": "Центр: нет воды третий день.",
        "ref-1": "Бригады работают на объектах.",
    }
    result = validate_digest_narrative(draft, plan, support_index, situation_plan=sit_plan)
    assert not result.is_valid
    assert any("UNSUPPORTED_CONCRETE_CLAIM" in v for v in result.violations)


@pytest.mark.asyncio
async def test_digest_narrative_writer_prompt_excludes_situation_items() -> None:
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativePlan,
        DigestNarrativeWriter,
    )

    captured_messages = []

    class FakeProvider:
        async def chat_completion(self, messages, **kwargs):
            captured_messages.extend(messages)
            return '{"blocks": [{"block_id": "block:utilities:0", "items": [{"headline": "H", "body": "B", "covered_story_ids": ["story:1"], "cited_support_ids": ["ref-1"]}]}]}'

    writer = DigestNarrativeWriter(provider=FakeProvider())
    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:1",),
        support_ids=("ref-1",),
        canonical_notes=(),
    )
    plan = DigestNarrativePlan(blocks=(block,))
    evidence = {
        "ref-1": _make_evidence("ref-1", 1, "Ремонтные работы продолжаются"),
    }
    cards = [
        StoryCard(
            id="story:1",
            topic="ЖКХ",
            importance="high",
            summary="Ремонт",
            rubric_id="utilities",
        )
    ]
    await writer.generate_narrative_draft(
        plan=plan,
        cards=cards,
        evidence=evidence,
    )
    assert len(captured_messages) == 2
    user_prompt = captured_messages[1]["content"]
    assert '"blocks"' in user_prompt
    assert '"situation_items"' not in user_prompt


def test_plan_digest_narrative_blocks_captures_detail_roles() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_narrative import plan_digest_narrative_blocks
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentationHint,
    )

    card1 = StoryCard(
        id="story:elec",
        topic="Свет",
        importance="high",
        summary="Генераторы",
        rubric_id="utilities",
    )
    card2 = StoryCard(
        id="story:road",
        topic="Дороги",
        importance="low",
        summary="Асфальт",
        rubric_id="utilities",
    )

    presentation_plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        detail_story_ids=("story:elec", "story:road"),
        story_hints=(
            DigestStoryPresentationHint(
                story_id="story:elec",
                detail_support_ids=("sup:gen",),
                merge_group_id="story:elec",
                detail_role="DRILL_DOWN",
            ),
            DigestStoryPresentationHint(
                story_id="story:road",
                detail_support_ids=("sup:road",),
                merge_group_id="story:road",
                detail_role="NORMAL",
            ),
        ),
    )

    plan = plan_digest_narrative_blocks(
        cards=[card1, card2],
        evidence={},
        rubrics=[{"id": "utilities", "name": "ЖКХ"}],
        max_cards_per_block=6,
        presentation_plan=presentation_plan,
    )

    assert len(plan.blocks) == 1
    block = plan.blocks[0]
    roles_dict = dict(block.detail_roles_by_story)
    assert roles_dict.get("story:elec") == "DRILL_DOWN"
    assert roles_dict.get("story:road") == "NORMAL"


def test_validate_digest_narrative_enforces_drill_down_evidence_citation() -> None:
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:elec",),
        support_ids=("ref-status", "ref-workaround"),
        canonical_notes=(),
        detail_support_ids_by_story=(("story:elec", ("ref-workaround",)),),
        detail_roles_by_story=(("story:elec", "DRILL_DOWN"),),
    )
    plan = DigestNarrativePlan(blocks=(block,))

    support_index = {
        "ref-status": "На Горе нет света.",
        "ref-workaround": "Жильцы дома 12 скинулись по 300 рублей на генератор.",
    }

    # 1. Reject draft where DRILL_DOWN item only cites dashboard/status support
    draft_missing_detail = DigestNarrativeDraft.from_dict(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "headline": "Отключение света",
                            "body": "На Горе отсутствует электроэнергия.",
                            "covered_story_ids": ["story:elec"],
                            "cited_support_ids": ["ref-status"],
                        }
                    ],
                }
            ]
        }
    )
    res_bad = validate_digest_narrative(draft_missing_detail, plan, support_index)
    assert not res_bad.is_valid
    assert any("DRILL_DOWN" in v for v in res_bad.violations)

    # 2. Accept draft where DRILL_DOWN item cites the distinct detail support
    draft_with_detail = DigestNarrativeDraft.from_dict(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "headline": "Домовой генератор на Горе",
                            "body": "Жильцы дома 12 скинулись по 300 рублей на генератор.",
                            "covered_story_ids": ["story:elec"],
                            "cited_support_ids": ["ref-workaround"],
                        }
                    ],
                }
            ]
        }
    )
    res_good = validate_digest_narrative(draft_with_detail, plan, support_index)
    assert res_good.is_valid


def test_validate_digest_narrative_rejects_unsupported_causal_relations() -> None:
    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        validate_digest_narrative,
    )

    block = DigestNarrativeBlock(
        block_id="block:utilities:0",
        rubric_id="utilities",
        rubric_title="ЖКХ",
        story_ids=("story:elec",),
        support_ids=("ref-status",),
        canonical_notes=(),
        detail_support_ids_by_story=(("story:elec", ("ref-status",)),),
        detail_roles_by_story=(("story:elec", "NORMAL"),),
    )
    plan = DigestNarrativePlan(blocks=(block,))

    # Support only mentions outage, NOT the cause
    support_index = {
        "ref-status": "По сообщениям жителей, на Горе нет света.",
    }

    # Draft asserts invented cause "Авария на подстанции оставила Гору без света"
    draft_unsupported_cause = DigestNarrativeDraft.from_dict(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "headline": "Авария на подстанции оставила Гору без света",
                            "body": "По сообщениям жителей, на Горе нет света.",
                            "covered_story_ids": ["story:elec"],
                            "cited_support_ids": ["ref-status"],
                        }
                    ],
                }
            ]
        }
    )
    res = validate_digest_narrative(draft_unsupported_cause, plan, support_index)
    assert not res.is_valid
    assert any("UNSUPPORTED_DIGEST_RELATION" in v for v in res.violations)


def test_digest_narrative_planning_and_validation_with_presentation_modes() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_narrative import (
        DigestNarrativeDraft,
        plan_digest_narrative_blocks,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)

    card_a = StoryCard(id="story:a", topic="A", importance="medium", summary="A", rubric_id="r1")
    card_b = StoryCard(id="story:b", topic="B", importance="high", summary="B", rubric_id="r1")
    card_c = StoryCard(id="story:c", topic="C", importance="low", summary="C", rubric_id="r1")

    evi_a = PublicationEvidence(
        evidence_id="support:a:detail",
        story_id=1,
        text="Детали истории А",
        source_text="Детали истории А",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref-a",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    evi_b_dash = PublicationEvidence(
        evidence_id="support:b:dashboard",
        story_id=2,
        text="Служба Б доступна",
        source_text="Служба Б доступна",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref-b-dash",
        source_id=2,
        source_item_id=2,
        source_role="official",
        observed_at=now,
    )
    evi_b_detail = PublicationEvidence(
        evidence_id="support:b:detail",
        story_id=2,
        text="Подробности работы службы Б",
        source_text="Подробности работы службы Б",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=3,
        source_ref="ref-b-detail",
        source_id=2,
        source_item_id=3,
        source_role="community",
        observed_at=now,
    )

    sit_group = CitySituationPresentationGroup(
        group_id="sit:b",
        group_kind="subject_status",
        subject_key="b",
        subject_label="Служба Б",
        state="AVAILABLE",
        source_refs=("ref-b-dash",),
        detail_lines=("Служба Б доступна",),
        covered_story_ids=("story:b", "story:c"),
        cited_support_ids=("support:b:dashboard",),
    )

    pres_plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(sit_group,),
            covered_source_refs=("ref-b-dash",),
        ),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:a",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("support:a:detail",),
                merge_group_id="story:a",
            ),
            DigestStoryPresentation(
                story_id="story:b",
                mode="DASHBOARD_AND_DRILLDOWN",
                city_situation_group_ids=("sit:b",),
                detail_support_ids=("support:b:detail",),
                merge_group_id="story:b",
            ),
            DigestStoryPresentation(
                story_id="story:c",
                mode="DASHBOARD_ONLY",
                city_situation_group_ids=("sit:b",),
                detail_support_ids=(),
                merge_group_id="story:c",
            ),
        ),
    )

    detail_cards = [c for c in [card_a, card_b, card_c] if c.id in pres_plan.detail_story_ids]
    narr_plan = plan_digest_narrative_blocks(
        cards=detail_cards,
        evidence={
            "support:a:detail": evi_a,
            "support:b:dashboard": evi_b_dash,
            "support:b:detail": evi_b_detail,
        },
        rubrics=[{"id": "r1", "name": "Рубрика 1"}],
        presentation_plan=pres_plan,
    )

    assert len(narr_plan.blocks) == 1
    assert narr_plan.blocks[0].story_ids == ("story:a", "story:b")
    assert "story:c" not in narr_plan.blocks[0].story_ids

    support_index = {
        "support:a:detail": "Детали истории А",
        "support:b:dashboard": "Служба Б доступна",
        "support:b:detail": "Подробности работы службы Б",
    }

    # Drilldown citing only dashboard support fails validation
    invalid_draft = DigestNarrativeDraft.from_dict(
        {
            "blocks": [
                {
                    "block_id": narr_plan.blocks[0].block_id,
                    "items": [
                        {
                            "headline": "История А",
                            "body": "Детали истории А",
                            "covered_story_ids": ["story:a"],
                            "cited_support_ids": ["support:a:detail"],
                        },
                        {
                            "headline": "История Б",
                            "body": "Служба Б доступна",
                            "covered_story_ids": ["story:b"],
                            "cited_support_ids": ["support:b:dashboard"],
                        },
                    ],
                }
            ]
        }
    )
    res_invalid = validate_digest_narrative(invalid_draft, narr_plan, support_index)
    assert not res_invalid.is_valid
    assert any("DRILL_DOWN_MISSING_DISTINCT_SUPPORT" in v for v in res_invalid.violations)

    # Valid draft citing detail support passes
    valid_draft = DigestNarrativeDraft.from_dict(
        {
            "blocks": [
                {
                    "block_id": narr_plan.blocks[0].block_id,
                    "items": [
                        {
                            "headline": "История А",
                            "body": "Детали истории А",
                            "covered_story_ids": ["story:a"],
                            "cited_support_ids": ["support:a:detail"],
                        },
                        {
                            "headline": "История Б",
                            "body": "Подробности работы службы Б",
                            "covered_story_ids": ["story:b"],
                            "cited_support_ids": ["support:b:detail"],
                        },
                    ],
                }
            ]
        }
    )
    res_valid = validate_digest_narrative(valid_draft, narr_plan, support_index)
    assert res_valid.is_valid


def test_build_deterministic_digest_draft_with_all_modes_and_attribution() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_narrative import (
        build_deterministic_digest_draft,
        plan_digest_narrative_blocks,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)

    card_1 = StoryCard(
        id="story:1", topic="Свет", importance="high", summary="Нет света", rubric_id="utilities"
    )
    card_2 = StoryCard(
        id="story:2",
        topic="Спорт",
        importance="medium",
        summary="Набор в секцию",
        rubric_id="society",
    )
    card_3 = StoryCard(
        id="story:3",
        topic="Водоснабжение",
        importance="high",
        summary="Воды нет, жильцы скидываются на подвоз",
        rubric_id="utilities",
    )

    evi_1 = PublicationEvidence(
        evidence_id="sup:1:dash",
        story_id=1,
        text="Света нет в центре",
        source_text="Света нет в центре",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref-1",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=now,
    )
    evi_2 = PublicationEvidence(
        evidence_id="sup:2:detail",
        story_id=2,
        text="Открыт бесплатный набор детей на футбол",
        source_text="Открыт бесплатный набор детей на футбол",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref-2",
        source_id=2,
        source_item_id=2,
        source_role="community",
        observed_at=now,
    )
    evi_3_dash = PublicationEvidence(
        evidence_id="sup:3:dash",
        story_id=3,
        text="Воды нет в районе",
        source_text="Воды нет в районе",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=3,
        source_ref="ref-3",
        source_id=3,
        source_item_id=3,
        source_role="official",
        observed_at=now,
    )
    evi_3_detail = PublicationEvidence(
        evidence_id="sup:3:detail",
        story_id=3,
        text="Жильцы дома скинулись по 300 рублей на подвоз воды",
        source_text="Жильцы дома скинулись по 300 рублей на подвоз воды",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=4,
        source_ref="ref-4",
        source_id=3,
        source_item_id=4,
        source_role="community",
        observed_at=now,
    )

    evidence_dict = {
        "sup:1:dash": evi_1,
        "sup:2:detail": evi_2,
        "sup:3:dash": evi_3_dash,
        "sup:3:detail": evi_3_detail,
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

    draft = build_deterministic_digest_draft(
        cards=[card_1, card_2, card_3],
        evidence=evidence_dict,
        rubrics=rubrics,
        presentation_plan=plan,
    )

    items = [item for block in draft.blocks for item in block.items]
    covered = {story_id for item in items for story_id in item.covered_story_ids}
    assert covered == {"story:2", "story:3"}
    assert "story:1" not in covered

    item_by_story = {item.covered_story_ids[0]: item for item in items}
    assert item_by_story["story:2"].cited_support_ids == ("sup:2:detail",)
    assert "По сообщениям жителей," in item_by_story["story:2"].body

    assert item_by_story["story:3"].cited_support_ids == ("sup:3:detail",)
    assert "sup:3:dash" not in item_by_story["story:3"].cited_support_ids
    assert "По сообщениям жителей," in item_by_story["story:3"].body

    # Validate with validator
    narr_plan = plan_digest_narrative_blocks(
        cards=[card_2, card_3],
        evidence=evidence_dict,
        rubrics=rubrics,
        presentation_plan=plan,
    )
    support_index = {eid: evi.text for eid, evi in evidence_dict.items()}
    val_res = validate_digest_narrative(draft, narr_plan, support_index)
    assert val_res.is_valid, f"Validation failed: {val_res.violations}"


def test_narrative_plan_turns_merge_group_into_required_story_group() -> None:
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )

    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Ремонт трубы {i}",
            importance="high",
            summary=f"Ремонт трубы {i}",
            rubric_id="utilities",
        )
        for i in range(1, 5)
    ]
    evidence = {f"sup:{i}": _make_evidence(f"sup:{i}", i, f"Факт {i}") for i in range(1, 5)}
    presentation_plan = DigestPresentationPlan(
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
            DigestStoryPresentation(
                story_id="story:3",
                mode="DETAIL_ONLY",
                detail_support_ids=("sup:3",),
                merge_group_id="merge:1",
            ),
            DigestStoryPresentation(
                story_id="story:4",
                mode="DETAIL_ONLY",
                detail_support_ids=("sup:4",),
                merge_group_id="story:4",
            ),
        ),
    )

    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence,
        rubrics=[_RUBRIC_UTIL, _RUBRIC_OTHER],
        max_cards_per_block=6,
        presentation_plan=presentation_plan,
    )

    assert len(plan.blocks) == 1
    assert plan.blocks[0].required_story_groups == (
        ("story:1", "story:2", "story:3"),
        ("story:4",),
    )


def test_narrative_plan_chunks_large_merge_group_into_max_3() -> None:
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )

    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Ремонт трубы {i}",
            importance="high",
            summary=f"Ремонт трубы {i}",
            rubric_id="utilities",
        )
        for i in range(1, 8)
    ]
    evidence = {f"sup:{i}": _make_evidence(f"sup:{i}", i, f"Факт {i}") for i in range(1, 8)}
    presentation_plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        story_presentations=tuple(
            DigestStoryPresentation(
                story_id=f"story:{i}",
                mode="DETAIL_ONLY",
                detail_support_ids=(f"sup:{i}",),
                merge_group_id="merge:1",
            )
            for i in range(1, 8)
        ),
    )

    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence,
        rubrics=[_RUBRIC_UTIL, _RUBRIC_OTHER],
        max_cards_per_block=6,
        presentation_plan=presentation_plan,
    )

    # 7 stories in chunks (3, 3, 1): block 0 gets (1,2,3) + (4,5,6) = 6 cards, block 1 gets (7,) = 1 card
    assert len(plan.blocks) == 2
    assert plan.blocks[0].required_story_groups == (
        ("story:1", "story:2", "story:3"),
        ("story:4", "story:5", "story:6"),
    )
    assert plan.blocks[1].required_story_groups == (("story:7",),)


def test_validate_digest_narrative_synthesis_group_partition_mismatch() -> None:
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
                rubric_title="ЖКХ",
                story_ids=("story:1", "story:2", "story:3"),
                support_ids=("sup:1", "sup:2", "sup:3"),
                canonical_notes=(),
                required_story_groups=(("story:1", "story:2", "story:3"),),
                support_ids_by_story=(
                    ("story:1", ("sup:1",)),
                    ("story:2", ("sup:2",)),
                    ("story:3", ("sup:3",)),
                ),
            ),
        )
    )

    # Writer splits the group of 3 into 3 separate items
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Headline 1",
                        body="Body 1",
                        covered_story_ids=("story:1",),
                        cited_support_ids=("sup:1",),
                    ),
                    DigestEditorialItemDraft(
                        headline="Headline 2",
                        body="Body 2",
                        covered_story_ids=("story:2",),
                        cited_support_ids=("sup:2",),
                    ),
                    DigestEditorialItemDraft(
                        headline="Headline 3",
                        body="Body 3",
                        covered_story_ids=("story:3",),
                        cited_support_ids=("sup:3",),
                    ),
                ),
            ),
        )
    )

    support_map = {"sup:1": "Fact 1", "sup:2": "Fact 2", "sup:3": "Fact 3"}
    res = validate_digest_narrative(draft, plan, support_map)
    assert not res.is_valid
    assert any("SYNTHESIS_GROUP_PARTITION_MISMATCH" in v for v in res.violations)


def test_validate_digest_narrative_story_support_missing() -> None:
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

    # Merged item only cites support belonging to story:1
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Headline 1 and 2",
                        body="Body 1 and 2",
                        covered_story_ids=("story:1", "story:2"),
                        cited_support_ids=("sup:1",),
                    ),
                ),
            ),
        )
    )

    support_map = {"sup:1": "Fact 1", "sup:2": "Fact 2"}
    res = validate_digest_narrative(draft, plan, support_map)
    assert not res.is_valid
    assert any("STORY_SUPPORT_MISSING" in v for v in res.violations)


def test_validate_digest_narrative_valid_merged_synthesis() -> None:
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

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Headline 1 and 2",
                        body="Body 1 and 2",
                        covered_story_ids=("story:1", "story:2"),
                        cited_support_ids=("sup:1", "sup:2"),
                    ),
                ),
            ),
        )
    )

    support_map = {"sup:1": "Fact 1", "sup:2": "Fact 2"}
    res = validate_digest_narrative(draft, plan, support_map)
    assert res.is_valid
