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

pytestmark = pytest.mark.unit


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
                                "headline": "h1",
                                "body": "",
                                "covered_story_ids": ["s1"],
                                "cited_support_ids": ["sup1"],
                            }
                        ],
                    }
                ]
            },
            "digest editorial item requires body, stories and supports",
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
            "digest editorial item requires body, stories and supports",
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
            "digest editorial item requires body, stories and supports",
        ),
    ],
)
def test_digest_narrative_draft_parser_rejections(invalid_data, error):
    from src.publication.digest_narrative import DigestNarrativeDraft

    with pytest.raises(ValueError, match=error):
        DigestNarrativeDraft.from_dict(invalid_data)


def test_validate_digest_narrative_valid():
    from src.publication.digest_narrative import (
        DigestClaimAtom,
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
                        claims=(
                            DigestClaimAtom(
                                text="В центральной части города устранили аварию на водоводе.",
                                covered_story_ids=("story:1",),
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                    DigestEditorialItemDraft(
                        headline="На подстанции продолжается ремонт",
                        body="На подстанции продолжается ремонт сетей.",
                        cited_support_ids=("sup:2",),
                        covered_story_ids=("story:2",),
                        claims=(
                            DigestClaimAtom(
                                text="На подстанции продолжается ремонт сетей.",
                                covered_story_ids=("story:2",),
                                cited_support_ids=("sup:2",),
                            ),
                        ),
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
    # situation_items are no longer stored (backward compat: ignored by from_dict)
    assert draft.situation_items == ()

    # Verify user prompt excludes situation_items
    call_args = mock_provider.chat_completion.call_args[1]
    messages = call_args["messages"]
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    user_data = json.loads(user_content)
    assert "situation_items" not in user_data
    assert "situation_groups" in user_data
    assert len(user_data["situation_groups"]) == 1
    assert user_data["situation_groups"][0]["group_id"] == "situation:water:avail"
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
    from src.publication.digest_narrative import DigestNarrativeDraft

    # Legacy situation_items data is silently accepted for backward compatibility;
    # from_dict no longer stores or validates them — the draft parses cleanly.
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
    # situation_items are ignored; the draft is valid with 0 situation items
    assert draft.situation_items == ()
    assert len(draft.blocks) == 1


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
    """Per plan §3, situation group set validation is removed from the thematic digest validator.
    Providing a situation_plan argument is accepted but no longer enforces group coverage."""
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

    # Draft omits sit_grp_2 — previously this raised SITUATION_GROUP_SET_MISMATCH,
    # but that validation has been removed (situation data is thematic-only now).
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
    # Situation group set mismatch is no longer enforced; draft is valid
    assert result.is_valid
    assert not any("SITUATION_GROUP_SET_MISMATCH" in v for v in result.violations)


def test_validate_digest_narrative_checks_unsupported_situation_claims() -> None:
    """Per plan §3, situation-level claim validation is removed. The thematic block
    validator checks evidence at the item level, not situation item level."""
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

    # The body in situation_items claims an invented deadline (18:30)
    # not supported by ref-w-1. Previously this triggered UNSUPPORTED_CONCRETE_CLAIM
    # at the situation level. Now situation_items are ignored by the validator;
    # the draft (which has a valid thematic block item) passes validation.
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
    # Situation-level validation removed; only thematic block item is validated
    assert result.is_valid


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


def test_deterministic_digest_hides_internal_reply_annotations() -> None:
    from src.publication.digest_narrative import (
        _sanitize_digest_support_text,
        build_digest_support_text_index,
    )

    assert _sanitize_digest_support_text('Света нет (in_reply_to: "А когда дадут?")') == "Света нет"
    evidence = {
        "sup:reply": _make_evidence("sup:reply", 1, 'Света нет (in_reply_to: "А когда дадут?")')
    }
    assert build_digest_support_text_index(evidence=evidence, cards=[]) == {
        "sup:reply": "Света нет"
    }


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


def test_narrative_plan_uses_compression_units_when_presentations_are_implicit() -> None:
    """Compatibility presentation defaults must not disable deterministic synthesis."""
    from src.publication.digest_presentation import DigestPresentationPlan

    cards = [
        StoryCard(
            id="story:1",
            topic="Электроснабжение",
            importance="high",
            summary="На улице Гайдара нет света",
            rubric_id="utilities",
        ),
        StoryCard(
            id="story:2",
            topic="Электроснабжение",
            importance="medium",
            summary="В районе РТС свет появляется по ночам",
            rubric_id="utilities",
        ),
    ]

    # DigestPresentationPlan creates legacy per-story descriptors when only story_ids
    # are supplied. Those implicit descriptors carry no editorial merge decision.
    presentation_plan = DigestPresentationPlan(story_ids=("story:1", "story:2"))

    plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence={},
        rubrics=[_RUBRIC_UTIL],
        presentation_plan=presentation_plan,
    )

    assert plan.blocks[0].required_story_groups == (("story:1", "story:2"),)


def test_narrative_plan_chunks_large_merge_group_into_max_6() -> None:
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

    # 7 stories in chunks (6, 1): block 0 gets (1..6) = 6 cards, block 1 gets (7,) = 1 card
    assert len(plan.blocks) == 2
    assert plan.blocks[0].required_story_groups == (
        ("story:1", "story:2", "story:3", "story:4", "story:5", "story:6"),
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
    # Per plan §3, SYNTHESIS_GROUP_PARTITION_MISMATCH is removed;
    # the writer is free to split a required_story_group into multiple items.
    # Story coverage is the enforced invariant, not grouping.
    assert res.is_valid
    assert not any("SYNTHESIS_GROUP_PARTITION_MISMATCH" in v for v in res.violations)


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


def test_multi_story_digest_item_requires_claim_coverage_for_each_story() -> None:
    from src.publication.digest_narrative import (
        DigestClaimAtom,
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

    # Item claims to cover story:1 and story:2, but its only claim atom belongs to story:1
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
                        claims=(
                            DigestClaimAtom(
                                text="Claim for story 1",
                                covered_story_ids=("story:1",),
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )

    support_map = {"sup:1": "Claim for story 1", "sup:2": "Fact 2"}
    res = validate_digest_narrative(draft, plan, support_map)
    assert res.is_valid is False
    assert any("STORY_CLAIM_COVERAGE_MISSING:story:2" in v for v in res.violations)


def test_one_claim_can_cover_multiple_equivalent_stories_with_union_supports() -> None:
    from src.publication.digest_narrative import (
        DigestClaimAtom,
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
                        claims=(
                            DigestClaimAtom(
                                text="Claim for both story 1 and story 2",
                                covered_story_ids=("story:1", "story:2"),
                                cited_support_ids=("sup:1", "sup:2"),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )

    support_map = {
        "sup:1": "Claim for both story 1 and story 2",
        "sup:2": "Claim for both story 1 and story 2",
    }
    res = validate_digest_narrative(draft, plan, support_map)
    assert res.is_valid is True


@pytest.mark.asyncio
async def test_generate_journalistic_digest_strips_dividers_and_rubric_asterisks():
    from unittest.mock import AsyncMock

    from src.publication.digest_narrative import DigestNarrativeWriter

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = """Дайджест · 07 сентября 2026

Вот ежедневный дайджест новостей Бердянска за 7 сентября 2026 года, составленный строго на основе предоставленных материалов.

---

**⚡ Коммунальная обстановка**

Бердянск остаётся без электричества уже 37-й день.

---

**💥 Безопасность и чрезвычайные ситуации**

Вечером в городе гремели взрывы.
"""
    writer = DigestNarrativeWriter(provider=mock_provider)
    clean_text, draft = await writer.generate_journalistic_digest(
        city="Бердянск",
        date_str="07 сентября 2026",
        cards=[],
    )

    assert "---" not in clean_text
    assert "**" not in clean_text
    assert "Вот ежедневный дайджест" not in clean_text
    assert "⚡ Коммунальная обстановка" in clean_text
    assert "💥 Безопасность и чрезвычайные ситуации" in clean_text
    assert clean_text.startswith("⚡ Коммунальная обстановка")


def test_digest_prompt_template_has_no_hardcoded_news_examples():
    from src.publication.digest_narrative import DIGEST_PROMPT_TEMPLATE

    # Ensure no hardcoded news items that LLM could copy
    assert "Ремонт магистральных интернет-сетей" not in DIGEST_PROMPT_TEMPLATE
    assert "Провайдер приступил к утренним восстановительным работам" not in DIGEST_PROMPT_TEMPLATE
    assert "АКЗ, РТС, Слободка, Центр, Колония, 8 Марта" not in DIGEST_PROMPT_TEMPLATE
    assert "ул. Шаумяна" not in DIGEST_PROMPT_TEMPLATE

    # Ensure anti-hallucination and topic synthesis rules are present
    assert "КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ И КОПИРОВАНИЕ ШАБЛОНА" in DIGEST_PROMPT_TEMPLATE
    assert "СИНТЕЗ СВЯЗАННЫХ СООБЩЕНИЙ" in DIGEST_PROMPT_TEMPLATE
    assert "Не превращайте дайджест в каталог" in DIGEST_PROMPT_TEMPLATE


def test_sanitize_digest_terminology():
    from src.publication.digest_narrative import sanitize_digest_terminology

    text = (
        "По данным оккупационной администрации, газ подадут завтра. "
        "Оккупационные власти подтвердили проведение восстановительных работ. "
        "Жители обратились к оккупантам с просьбой о помощи."
    )
    sanitized = sanitize_digest_terminology(text)
    assert "оккупационн" not in sanitized.lower()
    assert "оккупант" not in sanitized.lower()
    assert "По данным городской администрации" in sanitized
    assert "Городские власти подтвердили" in sanitized or "Местные власти подтвердили" in sanitized
    assert "к местным властям" in sanitized or "к представителям администрации" in sanitized


def test_digest_prompt_template_has_neutrality_and_advice_rules():
    from src.publication.digest_narrative import DIGEST_PROMPT_TEMPLATE

    assert "НЕЙТРАЛЬНАЯ ТЕРМИНОЛОГИЯ" in DIGEST_PROMPT_TEMPLATE
    assert "оккупанты" in DIGEST_PROMPT_TEMPLATE.lower()
    assert "городская администрация" in DIGEST_PROMPT_TEMPLATE.lower()
    assert "СОДЕРЖАТЕЛЬНОСТЬ" in DIGEST_PROMPT_TEMPLATE
    assert "ВЫДУМЫВАНИЕ СОВЕТОВ" in DIGEST_PROMPT_TEMPLATE


def test_find_and_strip_unsupported_digest_recommendations():
    from src.publication.digest_narrative import (
        find_unsupported_digest_recommendations,
        strip_unsupported_recommendations,
    )

    source_text = (
        "РЭС проводит ремонтные работы на подстанции в Центре. Света не будет с 9:00 до 17:00."
    )

    # Text with ungrounded advice invented by writer
    text_with_advice = (
        "В Центре проводятся ремонтные работы на подстанции, электроэнергии не будет до 17:00. "
        "Стоит заранее позаботиться о запасах воды и альтернативных источниках питания."
    )

    issues = find_unsupported_digest_recommendations(text_with_advice, source_text)
    assert len(issues) == 1
    assert "Стоит заранее позаботиться о запасах воды" in issues[0]

    # Stripping removes only the advice sentence
    cleaned = strip_unsupported_recommendations(text_with_advice, source_text)
    assert "Стоит заранее позаботиться" not in cleaned
    assert "В Центре проводятся ремонтные работы" in cleaned
    assert "электроэнергии не будет до 17:00." in cleaned

    # Legitimate advice present in source is NOT flagged or stripped
    source_with_official_advice = "МЧС: рекомендуется оставаться в укрытиях во время тревоги."
    text_with_grounded_advice = "По сообщению МЧС, рекомендуется оставаться в укрытиях."
    assert (
        find_unsupported_digest_recommendations(
            text_with_grounded_advice, source_with_official_advice
        )
        == []
    )
    assert (
        strip_unsupported_recommendations(text_with_grounded_advice, source_with_official_advice)
        == text_with_grounded_advice
    )


def test_recommendation_requires_modality_not_mere_word_overlap():
    from src.publication.digest_narrative import (
        find_unsupported_digest_recommendations,
        strip_unsupported_recommendations,
    )

    # Source discussing topic without advice/instruction modality must NOT support reader advice
    source_discussion = "Жители активно обсуждают запасы воды и альтернативные источники питания."
    text_with_advice = (
        "В районе перебои. Стоит заранее позаботиться о запасах воды и альтернативных источниках."
    )

    issues = find_unsupported_digest_recommendations(text_with_advice, [source_discussion])
    assert len(issues) == 1
    assert "Стоит заранее позаботиться" in issues[0]
    cleaned = strip_unsupported_recommendations(text_with_advice, source_discussion)
    assert "Стоит заранее позаботиться" not in cleaned


def test_expanded_advice_detection_forms():
    from src.publication.digest_narrative import find_unsupported_digest_recommendations

    source_plain = "В городе объявлена воздушная тревога."
    forms = [
        "Нужно оставаться в безопасных местах.",
        "Жителям советуют не выходить из дома.",
        "Постарайтесь ограничить поездки.",
        "Запаситесь питьевой водой.",
        "Зарядите свои телефоны и павербанки.",
        "Не выходите на улицу во время тревоги.",
        "Воздержитесь от поездок по городу.",
    ]
    for form in forms:
        text = f"В городе тревога. {form}"
        issues = find_unsupported_digest_recommendations(text, [source_plain])
        assert len(issues) >= 1, f"Failed to detect advice in: {form}"


def test_recommendation_requires_modality_and_subject_in_same_support():
    from src.publication.digest_narrative import find_unsupported_digest_recommendations

    # Support 1: Modality only, different subject
    sup1 = "Администрация города: рекомендуется сохранять спокойствие и доверять официальным источникам."
    # Support 2: Subject only, no recommendation modality
    sup2 = "В магазинах микрорайона наблюдается дефицит питьевой воды и свечей."

    text_advice = "Рекомендуется сделать запасы питьевой воды на несколько дней вперед."

    # When modality and subject come from separate supports, it must be rejected!
    issues = find_unsupported_digest_recommendations(text_advice, [sup1, sup2])
    assert len(issues) == 1
    assert "Рекомендуется сделать запасы питьевой воды" in issues[0]

    # When both are co-present in one single support, it is accepted
    sup_valid = (
        "Водоканал обратился к жителям: рекомендуется сделать запасы питьевой воды перед ремонтом."
    )
    issues_valid = find_unsupported_digest_recommendations(text_advice, [sup1, sup2, sup_valid])
    assert len(issues_valid) == 0


def test_recommendation_modality_and_generic_stopwords_overlap_rejected():
    from src.publication.digest_narrative import find_unsupported_digest_recommendations

    # Support has recommendation modality and generic stopwords ('заранее'), but subject is 'закрыть окна'
    support = "МЧС: Рекомендуется заранее закрыть окна при сильном ветре."
    # Generated has recommendation modality and 'заранее', but subject is 'сделать запас питьевой воды'
    generated = "Рекомендуется заранее сделать запас питьевой воды."

    issues = find_unsupported_digest_recommendations(generated, [support])
    assert len(issues) == 1
    assert "Рекомендуется заранее сделать запас питьевой воды" in issues[0]


def test_recommendation_action_clause_level_grounding_rejects_unsupported_bundled_action():
    from src.publication.digest_narrative import (
        find_unsupported_digest_recommendations,
        strip_unsupported_recommendations,
    )

    # Support grounds ONLY water supply: "Водоканал: рекомендуется запастись питьевой водой"
    support = "Водоканал: рекомендуется заранее запастись питьевой водой перед ремонтом."

    # Generated bundles a grounded action ("запастись питьевой водой") with an ungrounded action ("не выходить на улицу")
    bundled_text = "Рекомендуется запастись питьевой водой и не выходить на улицу."

    # Must detect violation because "не выходить на улицу" is ungrounded
    violations = find_unsupported_digest_recommendations(bundled_text, [support])
    assert len(violations) == 1
    assert "Рекомендуется запастись питьевой водой и не выходить на улицу" in violations[0]

    # Must be stripped
    stripped = strip_unsupported_recommendations(bundled_text, support)
    assert "не выходить на улицу" not in stripped
    assert "Рекомендуется" not in stripped

    # When both actions are grounded in supports with modality, it passes
    support_both = "Водоканал: рекомендуется запастись питьевой водой. МЧС: рекомендуется не выходить на улицу."
    violations_ok = find_unsupported_digest_recommendations(bundled_text, [support_both])
    assert len(violations_ok) == 0


@pytest.mark.asyncio
async def test_generate_narrative_draft_topic_bundle_unhashable_fix(mocker) -> None:
    import json

    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeDraft,
        DigestNarrativePlan,
        DigestNarrativeWriter,
    )
    from src.publication.digest_presentation import RequiredDigestFact, TopicBundle

    # LLM returns an item with empty claim cited_support_ids or nested list
    mock_provider = mocker.AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:communications:0",
                    "items": [
                        {
                            "headline": "Связь восстановлена",
                            "body": "Провайдер восстановил интернет.",
                            "covered_story_ids": ["story:1", "story:2"],
                            "cited_support_ids": ["sup-comm-1"],
                            "claims": [
                                {
                                    "text": "Связь восстановлена в полном объеме.",
                                    "covered_story_ids": ["story:1"],
                                    # cited_support_ids missing or empty
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    tb = TopicBundle(
        bundle_id="bundle:communications:telecom",
        rubric_id="communications",
        topic_key="telecom",
        topic_label="Связь",
        emoji="📶",
        story_ids=("story:1", "story:2"),
        support_ids=("sup-comm-1", "sup-comm-2"),
        fact_ledger=("Провайдер восстановил интернет.",),
        required_facts=(
            RequiredDigestFact(
                fact_id="fact:comm:1",
                rubric_id="communications",
                subject_key="telecom",
                subject_label="Связь",
                story_ids=("story:1",),
                support_ids=("sup-comm-1",),
                text="Связь восстановлена.",
            ),
        ),
    )

    block = DigestNarrativeBlock(
        block_id="block:communications:0",
        rubric_id="communications",
        rubric_title="Связь",
        story_ids=("story:1", "story:2"),
        support_ids=("sup-comm-1", "sup-comm-2"),
        canonical_notes=(),
        topic_bundles=(tb,),
        required_facts=tb.required_facts,
    )
    plan = DigestNarrativePlan(blocks=(block,))

    writer = DigestNarrativeWriter(provider=mock_provider)
    # Must succeed without TypeError: cannot use 'list' as a dict key
    draft = await writer.generate_narrative_draft(
        plan=plan,
        cards=[],
        evidence={},
    )
    assert isinstance(draft, DigestNarrativeDraft)
    assert len(draft.blocks) == 1
    assert len(draft.blocks[0].items) == 1
    item = draft.blocks[0].items[0]
    assert all(isinstance(s, str) for s in item.cited_support_ids)


@pytest.mark.asyncio
async def test_generate_narrative_draft_builds_missing_block_fallback_once(mocker) -> None:
    import json
    from unittest.mock import AsyncMock

    from src.publication.digest_narrative import (
        DigestNarrativeBlock,
        DigestNarrativeWriter,
    )

    plan = DigestNarrativePlan(
        blocks=tuple(
            DigestNarrativeBlock(
                block_id=f"block:utilities:{idx}",
                rubric_id="utilities",
                rubric_title="ЖКХ",
                story_ids=(),
                support_ids=(),
                canonical_notes=(),
            )
            for idx in range(2)
        )
    )

    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps({"blocks": []})

    with pytest.raises(ValueError, match="must contain at least one item"):
        await DigestNarrativeWriter(provider).generate_narrative_draft(
            plan=plan,
            cards=[],
            evidence={},
        )


def test_build_deterministic_digest_draft_multi_story_topic_bundle_grounding() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_narrative import (
        build_deterministic_digest_draft,
        build_digest_support_text_index,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
        RequiredDigestFact,
    )

    # Story 1 has detail_support_ids and number 27 in topic
    # Story 2 has «Юпитер» in useful_details and facts
    card1 = StoryCard(
        id="story:1",
        topic="Маршрут 27 работает по графику",
        importance="medium",
        summary="Автобусы 27 вышли на линию",
        rubric_id="transport",
        useful_details=(),
        hard_facts=(),
    )
    card2 = StoryCard(
        id="story:2",
        topic="Провайдер Юпитер обновил сеть",
        importance="medium",
        summary="Провайдер «Юпитер» провел модернизацию оборудования",
        rubric_id="transport",
        useful_details=(),
        hard_facts=(),
    )

    evidence = {
        "sup-1": _make_evidence("sup-1", 1, "Маршрут 27 курсирует в штатном режиме."),
        "sup-2": _make_evidence("sup-2", 2, "Провайдер «Юпитер» закончил ремонтные работы."),
    }

    pres1 = DigestStoryPresentation(
        story_id="story:1",
        mode="DETAIL_ONLY",
        detail_support_ids=("sup-1",),
    )
    pres2 = DigestStoryPresentation(
        story_id="story:2",
        mode="DETAIL_ONLY",
        detail_support_ids=("sup-2",),
    )

    pres_plan = DigestPresentationPlan(
        story_presentations=(pres1, pres2),
        city_situation=CitySituationPresentationPlan(),
        required_facts=(
            RequiredDigestFact(
                fact_id="rf:1",
                rubric_id="transport",
                subject_key="bus",
                subject_label="Транспорт",
                story_ids=("story:1",),
                support_ids=("sup-1",),
                text="Маршрут 27 курсирует.",
            ),
        ),
    )

    rubrics = [{"id": "transport", "title": "Транспорт и связь"}]
    cards = [card1, card2]
    support_index = build_digest_support_text_index(evidence=evidence, cards=cards)

    draft = build_deterministic_digest_draft(
        cards=cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=pres_plan,
        support_text_by_id=support_index,
    )

    from src.publication.digest_narrative import plan_digest_narrative_blocks

    narrative_plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=pres_plan,
    )

    # Must pass validation without UNSUPPORTED_CONCRETE_CLAIM for '27' or '«Юпитер»'
    val_res = validate_digest_narrative(
        draft,
        narrative_plan,
        support_text_by_id=support_index,
        all_known_draft_supports=list(support_index.values()),
    )
    assert val_res.is_valid, f"Validation failed: {val_res.violations}"


@pytest.mark.asyncio
async def test_digest_editor_repairs_violations_without_deterministic_fallback():
    import json
    from unittest.mock import AsyncMock

    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import (
        DigestClaimAtom,
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
        build_digest_support_text_index,
        plan_digest_narrative_blocks,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )

    card1 = StoryCard(
        id="story:1",
        topic="Водоснабжение",
        importance="high",
        summary="Водоснабжение в Лисках отключено",
        rubric_id="utilities",
        useful_details=(),
        hard_facts=(),
    )
    evidence = {
        "sup-1": _make_evidence("sup-1", 1, "Водоснабжение в Лисках отключено."),
    }
    pres_plan = DigestPresentationPlan(
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1", mode="DETAIL_ONLY", detail_support_ids=("sup-1",)
            ),
        ),
        city_situation=CitySituationPresentationPlan(),
    )
    rubrics = [{"id": "utilities", "title": "Коммунальная сфера"}]
    cards = [card1]
    support_index = build_digest_support_text_index(evidence=evidence, cards=cards)
    narrative_plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=pres_plan,
    )

    bad_item = DigestEditorialItemDraft(
        headline="Водоснабжение",
        body="Воды нет из-за аварии на 500 километрах труб.",
        covered_story_ids=("story:1",),
        cited_support_ids=("sup-1",),
        claims=(
            DigestClaimAtom(
                text="Воды нет из-за аварии на 500 километрах труб",
                covered_story_ids=("story:1",),
                cited_support_ids=("sup-1",),
            ),
        ),
        emoji="💧",
    )
    draft_with_failure = DigestNarrativeDraft(
        blocks=(DigestNarrativeBlockDraft(block_id="block:utilities:0", items=(bad_item,)),)
    )

    val_res = validate_digest_narrative(
        draft_with_failure,
        narrative_plan,
        support_text_by_id=support_index,
        all_known_draft_supports=list(support_index.values()),
    )
    assert not val_res.is_valid

    # Mock provider that fixes the draft using LLM repair
    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "item_index": 0,
                            "emoji": "💧",
                            "headline": "Водоснабжение",
                            "body": "В Лисках водоснабжение временно отключено по сообщениям жителей.",
                        }
                    ],
                }
            ]
        }
    )

    editor = DigestEditor(provider=mock_provider)
    repaired_draft = await editor.polish_and_compress(
        draft_with_failure,
        evidence=evidence,
        violations=val_res.violations,
    )
    assert repaired_draft.blocks[0].items[0].headline == "Водоснабжение"
    assert "Лисках" in repaired_draft.blocks[0].items[0].body
    assert "500 километрах" not in repaired_draft.blocks[0].items[0].body
    # Ensure violations were passed in prompt
    prompt_sent = mock_provider.chat_completion.call_args[1]["messages"][0]["content"]
    assert "CRITICAL VALIDATION REPAIRS REQUIRED" in prompt_sent


@pytest.mark.asyncio
async def test_digest_editor_repairs_missing_required_facts_claims():
    import json
    from unittest.mock import AsyncMock

    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
    )
    from src.publication.digest_presentation import RequiredDigestFact

    rf = RequiredDigestFact(
        fact_id="на_пионерской_дмитрова_ее_нет",
        rubric_id="utilities",
        subject_key="water",
        subject_label="Водоснабжение",
        story_ids=("story:1",),
        support_ids=("sup-1",),
        text="На Пионерской воды нет уже неделю",
    )

    class MockPlanBlock:
        block_id = "block:utilities:0"
        required_facts = (rf,)
        support_ids = ("sup-1",)
        support_ids_by_story = (("story:1", ("sup-1",)),)

    class MockPlan:
        blocks = (MockPlanBlock(),)
        required_facts = (rf,)
        required_fact_by_id = {rf.fact_id: rf}

    orig_item = DigestEditorialItemDraft(
        headline="Водоснабжение",
        body="Водоснабжение в городе остаётся на контроле.",
        covered_story_ids=("story:1",),
        cited_support_ids=("sup-1",),
        claims=(),
        emoji="💧",
    )
    draft = DigestNarrativeDraft(
        blocks=(DigestNarrativeBlockDraft(block_id="block:utilities:0", items=(orig_item,)),)
    )

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:utilities:0",
                    "items": [
                        {
                            "item_index": 0,
                            "emoji": "💧",
                            "headline": "Водоснабжение",
                            "body": "На Пионерской воды нет уже неделю по сообщениям жителей.",
                        }
                    ],
                }
            ]
        }
    )

    editor = DigestEditor(provider=mock_provider)
    repaired_draft = await editor.polish_and_compress(
        draft,
        plan=MockPlan(),
        violations=[
            "DIGEST_FACT_COVERAGE_MISSING:на_пионерской_дмитрова_ее_нет in block block:utilities:0"
        ],
    )

    # Check that required fact claim was added to claims and fact is covered
    repaired_item = repaired_draft.blocks[0].items[0]
    assert any("на_пионерской_дмитрова_ее_нет" in c.covered_fact_ids for c in repaired_item.claims)
    assert "sup-1" in repaired_item.cited_support_ids


@pytest.mark.asyncio
async def test_generate_narrative_draft_minimal_bundle_items_schema():
    import json
    from unittest.mock import AsyncMock

    from src.publication.digest_narrative import (
        DigestNarrativeWriter,
        build_digest_support_text_index,
        plan_digest_narrative_blocks,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
        RequiredDigestFact,
    )

    card = StoryCard(
        id="story:10",
        topic="Водоснабжение",
        importance="high",
        summary="В Лисках переподключение водопровода",
        rubric_id="utilities",
        useful_details=(),
        hard_facts=(),
    )
    evidence = {
        "sup:10": _make_evidence(
            "sup:10", 10, "В Лисках переподключение водопровода на новый трубопровод."
        ),
    }
    pres_plan = DigestPresentationPlan(
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:10", mode="DETAIL_ONLY", detail_support_ids=("sup:10",)
            ),
        ),
        city_situation=CitySituationPresentationPlan(),
        required_facts=(
            RequiredDigestFact(
                fact_id="rf:water",
                rubric_id="utilities",
                subject_key="water",
                subject_label="Водоснабжение",
                story_ids=("story:10",),
                support_ids=("sup:10",),
                text="В Лисках переподключение водопровода.",
            ),
        ),
    )
    rubrics = [{"id": "utilities", "title": "ЖКХ"}]
    cards = [card]
    support_index = build_digest_support_text_index(evidence=evidence, cards=cards)

    narrative_plan = plan_digest_narrative_blocks(
        cards=cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=pres_plan,
    )
    bundle_id = narrative_plan.blocks[0].topic_bundles[0].bundle_id

    # Simulated lean LLM response returning minimal schema {"items": [...]}
    llm_payload = {
        "items": [
            {
                "bundle_id": bundle_id,
                "emoji": "💧",
                "headline": "В Лисках переподключают магистральный водопровод",
                "body": "По информации коммунальных служб, в микрорайоне Лиски ведутся работы по переподключению водопровода.",
                "covered_fact_ids": ["rf:water"],
            }
        ]
    }

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(llm_payload)

    writer = DigestNarrativeWriter(mock_provider)
    draft = await writer.generate_narrative_draft(
        plan=narrative_plan,
        cards=cards,
        evidence=evidence,
    )

    assert len(draft.blocks) == 1
    assert len(draft.blocks[0].items) == 1
    item = draft.blocks[0].items[0]

    # Programmatic provenance assertions
    assert item.covered_story_ids == ("story:10",)
    assert "sup:10" in item.cited_support_ids
    assert item.emoji == "💧"
    assert any("rf:water" in c.covered_fact_ids for c in item.claims)

    val_res = validate_digest_narrative(
        draft,
        narrative_plan,
        support_text_by_id=support_index,
        all_known_draft_supports=list(support_index.values()),
    )
    assert val_res.is_valid, f"Validation failed: {val_res.violations}"

    # A required fact omitted from the model's covered_fact_ids must not be
    # silently reattached by normalization and treated as covered.
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "items": [
                {
                    "bundle_id": bundle_id,
                    "emoji": "💧",
                    "headline": "В Лисках переподключают магистральный водопровод",
                    "body": "По информации коммунальных служб, в микрорайоне Лиски ведутся работы по переподключению водопровода.",
                    "covered_fact_ids": [],
                }
            ]
        }
    )
    draft_without_fact_coverage = await writer.generate_narrative_draft(
        plan=narrative_plan,
        cards=cards,
        evidence=evidence,
    )
    missing_fact_val = validate_digest_narrative(
        draft_without_fact_coverage,
        narrative_plan,
        support_text_by_id=support_index,
        all_known_draft_supports=list(support_index.values()),
    )
    assert not missing_fact_val.is_valid
    assert any("DIGEST_FACT_COVERAGE_MISSING:rf:water" in v for v in missing_fact_val.violations)

    # Unknown bundle IDs must fail closed instead of being silently assigned to
    # the first available bundle and receiving the wrong provenance.
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "items": [
                {
                    "bundle_id": "bundle:utilities:unknown",
                    "emoji": "💧",
                    "headline": "Неизвестная тема",
                    "body": "В тексте указан пакет, которого нет во входном плане.",
                    "covered_fact_ids": [],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="unknown bundle_id"):
        await writer.generate_narrative_draft(
            plan=narrative_plan,
            cards=cards,
            evidence=evidence,
        )

    # Prefix bundle ID (e.g. model returned bundle:utilities:water omitting trailing parts)
    prefix_bid = ":".join(bundle_id.split(":")[:3]) if len(bundle_id.split(":")) >= 3 else bundle_id
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "items": [
                {
                    "bundle_id": prefix_bid,
                    "emoji": "💧",
                    "headline": "В Лисках переподключают водопровод",
                    "body": "По информации коммунальных служб, в микрорайоне Лиски ведутся работы по переподключению водопровода.",
                    "covered_fact_ids": ["rf:water"],
                }
            ]
        }
    )
    draft_prefix = await writer.generate_narrative_draft(
        plan=narrative_plan,
        cards=cards,
        evidence=evidence,
    )
    assert len(draft_prefix.blocks) == 1
    assert len(draft_prefix.blocks[0].items) == 1
    assert draft_prefix.blocks[0].items[0].covered_story_ids == ("story:10",)

    # Cross-rubric / synonym bundle ID hallucination (e.g. bundle:infrastructure:internet for communications)
    comm_card = StoryCard(
        id="story:20",
        topic="Связь и интернет",
        importance="medium",
        summary="В Бердянске наблюдаются перебои с мобильным интернетом",
        rubric_id="communications",
        useful_details=(),
        hard_facts=(),
    )
    evidence_comm = {
        **evidence,
        "sup:20": _make_evidence(
            "sup:20", 20, "В Бердянске наблюдаются перебои с мобильным интернетом."
        ),
    }
    pres_plan_multi = DigestPresentationPlan(
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:10", mode="DETAIL_ONLY", detail_support_ids=("sup:10",)
            ),
            DigestStoryPresentation(
                story_id="story:20", mode="DETAIL_ONLY", detail_support_ids=("sup:20",)
            ),
        ),
        city_situation=CitySituationPresentationPlan(),
        required_facts=(
            RequiredDigestFact(
                fact_id="rf:water",
                rubric_id="utilities",
                subject_key="water",
                subject_label="Водоснабжение",
                story_ids=("story:10",),
                support_ids=("sup:10",),
                text="В Лисках переподключение водопровода.",
            ),
            RequiredDigestFact(
                fact_id="rf:internet",
                rubric_id="communications",
                subject_key="connectivity",
                subject_label="Связь и интернет",
                story_ids=("story:20",),
                support_ids=("sup:20",),
                text="В Бердянске перебои со связью и интернетом.",
            ),
        ),
    )
    narrative_plan_multi = plan_digest_narrative_blocks(
        cards=[card, comm_card],
        evidence=evidence_comm,
        rubrics=[
            {"id": "utilities", "title": "ЖКХ"},
            {"id": "communications", "title": "Связь и интернет"},
        ],
        presentation_plan=pres_plan_multi,
    )
    # Model hallucinated "bundle:infrastructure:internet" instead of "bundle:communications:connectivity"
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "items": [
                {
                    "bundle_id": bundle_id,
                    "emoji": "💧",
                    "headline": "В Лисках переподключают водопровод",
                    "body": "По информации коммунальных служб, в микрорайоне Лиски ведутся работы по переподключению водопровода.",
                    "covered_fact_ids": ["rf:water"],
                },
                {
                    "bundle_id": "bundle:infrastructure:internet",
                    "emoji": "🌐",
                    "headline": "Перебои со связью и мобильным интернетом",
                    "body": "Горожане сообщают о нестабильной работе мобильного интернета в ряде районов.",
                    "covered_fact_ids": ["rf:internet"],
                },
            ]
        }
    )
    draft_resilient = await writer.generate_narrative_draft(
        plan=narrative_plan_multi,
        cards=[card, comm_card],
        evidence=evidence_comm,
    )
    assert len(draft_resilient.blocks) == 2
    # Verify the internet item was correctly placed in the communications block
    comm_block = next(
        b for b in draft_resilient.blocks if b.block_id.startswith("block:communications:")
    )
    assert len(comm_block.items) == 1
    assert comm_block.items[0].covered_story_ids == ("story:20",)


@pytest.mark.asyncio
async def test_generate_narrative_draft_roads_synonym_resolution():
    """Verify that hallucinated bundle:infrastructure:roads correctly resolves to transport."""
    import json
    from unittest.mock import AsyncMock

    from src.publication.digest_narrative import DigestNarrativeWriter
    from src.publication.digest_presentation import (
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
        RequiredDigestFact,
    )

    mock_provider = AsyncMock()
    writer = DigestNarrativeWriter(provider=mock_provider)

    road_card = StoryCard(
        id="story:30",
        topic="Ремонт дорожного покрытия на ул. Шевченко",
        importance="medium",
        summary="Дорожные службы приступили к укладке асфальта.",
        rubric_id="mobility",
        useful_details=(),
        hard_facts=(),
    )
    evidence_road = {
        "sup:30": _make_evidence("sup:30", 30, "Ремонт дороги на ул. Шевченко."),
    }
    pres_plan = DigestPresentationPlan(
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:30",
                mode="DETAIL_ONLY",
                detail_support_ids=("sup:30",),
            ),
        ),
        city_situation=CitySituationPresentationPlan(),
        required_facts=(
            RequiredDigestFact(
                fact_id="rf:roads",
                rubric_id="mobility",
                subject_key="transport",
                subject_label="Транспорт и дороги",
                story_ids=("story:30",),
                support_ids=("sup:30",),
                text="Ремонт дороги на ул. Шевченко.",
            ),
        ),
    )
    narrative_plan = plan_digest_narrative_blocks(
        cards=[road_card],
        evidence=evidence_road,
        rubrics=[{"id": "mobility", "title": "Транспорт и дороги"}],
        presentation_plan=pres_plan,
    )
    # Model generates bundle:infrastructure:roads
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "items": [
                {
                    "bundle_id": "bundle:infrastructure:roads",
                    "emoji": "🚌",
                    "headline": "Ремонт дороги на ул. Шевченко",
                    "body": "Дорожные службы приступили к укладке асфальта на улице Шевченко.",
                    "covered_fact_ids": ["rf:roads"],
                }
            ]
        }
    )
    draft = await writer.generate_narrative_draft(
        plan=narrative_plan,
        cards=[road_card],
        evidence=evidence_road,
    )
    assert len(draft.blocks) == 1
    assert draft.blocks[0].items[0].headline == "Ремонт дороги на ул. Шевченко"


def test_usable_fact_line_keeps_concrete_report_with_conversational_prefix():
    from src.publication.digest_presentation import _clean_fact_sentence, _is_usable_fact_line

    assert _is_usable_fact_line("Да, с 12.09 свет отключили.")
    assert _clean_fact_sentence("Да, с 12.09 свет отключили.") == "С 12.09 свет отключили."

    # Verify chatter and lost & found filtering
    assert not _is_usable_fact_line("внизу, район 16 школы")
    assert not _is_usable_fact_line("Кто-то нашёл рюкзак в автобусе")
    assert not _is_usable_fact_line("Нашли собаку в районе набережной")
    assert not _is_usable_fact_line("Сообщение от местного жителя: добрый вечер всем")
    assert (
        _clean_fact_sentence("Сообщение от местного жителя: на улице Ленина нет воды")
        == "На улице Ленина нет воды."
    )
    assert (
        _clean_fact_sentence(
            "Жители района АКЗ интересуются, есть ли электричество. Один отвечает, что есть."
        )
        == ""
    )
    assert (
        _clean_fact_sentence(
            "[Электроснабжение] UNAVAILABLE — На улице Ленина нет света. "
            "Подробности уточняйте в официальных источниках."
        )
        == "На улице Ленина нет света."
    )
    assert (
        _clean_fact_sentence(
            "Сообщения сообщества о выполнении работ в Бердянске. "
            "На улице Ленина восстановили подачу воды."
        )
        == "На улице Ленина восстановили подачу воды."
    )
    assert not _is_usable_fact_line(
        "В Бердянске планируется мероприятие, создающее атмосферу красоты для маленьких леди."
    )


def test_usable_fact_line_rejects_unresolved_chat_reactions_and_directory_payload():
    from src.publication.digest_presentation import _clean_fact_sentence, _is_usable_fact_line

    rejected = (
        "Что в центре Бердянска не тихо, но.",
        "Движуха с вечера началась, а я сразу даже не заметил.",
        "О повторяющихся громких звуках в Бердянске. Требуется уточнение источника.",
        "Завтра — день города.",
        "Опубликован контактный телефон скорой помощи для вызова врача: +79901428214.",
        "У меня на Димитрова не работает.",
    )
    for line in rejected:
        assert not _is_usable_fact_line(line), line

    assert _is_usable_fact_line("В Бердянске слышны громкие звуки, возможно взрывы.")
    assert _is_usable_fact_line("На улице Гайдара нет света с 1 августа.")
    assert not _is_usable_fact_line("Поехали и он всё оформил за 20 минут.")
    assert not _is_usable_fact_line("Мы с приятелем не звонили, дозвониться не получится.")
    assert (
        _clean_fact_sentence("Жительница сообщает, что на улице Гагарина нет воды.")
        == "На улице Гагарина нет воды."
    )
    assert (
        _clean_fact_sentence("Жительница Бердянска сообщает, что воду не отключали 3 дня.")
        == "Воду не отключали 3 дня."
    )


def test_topic_bundle_drops_filtered_metadata_summary():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    card = StoryCard(
        id="story:metadata",
        topic="Водоснабжение",
        importance="medium",
        summary="Жители публикуют сообщения с эмодзи воды",
        rubric_id="utilities",
        useful_details=(),
        hard_facts=(),
    )

    bundles = build_thematic_topic_bundles([card])

    assert bundles == ()


def test_topic_bundle_does_not_absorb_filtered_story_into_other_topic():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    substantive = StoryCard(
        id="story:water",
        topic="Водоснабжение",
        importance="high",
        summary="На улице Ленина восстановили подачу воды.",
        rubric_id="utilities",
        useful_details=(),
        hard_facts=(),
    )
    chatter = StoryCard(
        id="story:chatter",
        topic="Обсуждение в чате",
        importance="low",
        summary="Жители публикуют сообщения с эмодзи воды",
        rubric_id="utilities",
        useful_details=(),
        hard_facts=(),
    )

    bundles = build_thematic_topic_bundles([substantive, chatter])

    assert bundles
    assert "story:chatter" not in {story_id for bundle in bundles for story_id in bundle.story_ids}


def test_topic_bundles_do_not_merge_unrecognized_general_topics():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        StoryCard(
            id="story:lighting",
            topic="Городское событие",
            importance="medium",
            summary="На улице Ленина заменили фонари.",
            rubric_id="other",
            useful_details=(),
            hard_facts=(),
        ),
        StoryCard(
            id="story:plaza",
            topic="Городское событие",
            importance="medium",
            summary="На площади завершили работы по благоустройству.",
            rubric_id="other",
            useful_details=(),
            hard_facts=(),
        ),
    ]

    bundles = build_thematic_topic_bundles(cards)

    assert {bundle.story_ids for bundle in bundles} == {
        ("story:lighting",),
        ("story:plaza",),
    }


def test_topic_bundles_merge_exact_duplicate_unrecognized_facts():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        StoryCard(
            id="story:city-day-a",
            topic="Завтра в Бердянске — день города",
            importance="medium",
            summary="Жители Бердянска настраиваются на позитив в преддверии дня города, который состоится завтра.",
            rubric_id="focus",
            useful_details=(),
            hard_facts=(),
            community_observations=(
                StoryElement(
                    text="Завтра в Бердянске состоится День города.", source_refs=["test:a"]
                ),
            ),
        ),
        StoryCard(
            id="story:city-day-b",
            topic="Завтра в Бердянске — день города",
            importance="medium",
            summary="Жители Бердянска настраиваются на позитив в преддверии дня города, который состоится завтра.",
            rubric_id="focus",
            useful_details=(),
            hard_facts=(),
            community_observations=(
                StoryElement(
                    text="Завтра в Бердянске состоится День города.", source_refs=["test:b"]
                ),
            ),
        ),
    ]

    bundles = build_thematic_topic_bundles(cards)

    assert len(bundles) == 1
    assert bundles[0].story_ids == ("story:city-day-a", "story:city-day-b")


def test_clean_fact_sentence_removes_chat_sources_and_artifacts():
    from src.publication.digest_presentation import _clean_fact_sentence

    raw_1 = "в чате Бердянска сообщают, что части домов свет дают по 4–5 дней без перерыва"
    assert "в чате" not in _clean_fact_sentence(raw_1).lower()
    assert _clean_fact_sentence(raw_1).startswith("Части домов свет дают")

    raw_2 = "На 4-й линии свет есть, возможно не на всей улице (со слов «Лёшки»)."
    cleaned_2 = _clean_fact_sentence(raw_2)
    assert "со слов" not in cleaned_2
    assert "лёшки" not in cleaned_2.lower()
    assert cleaned_2 == "На 4-й линии свет есть, возможно не на всей улице."

    raw_3 = ", свет на ул. Павлова сохранился"
    assert not _clean_fact_sentence(raw_3).startswith(",")
    assert _clean_fact_sentence(raw_3).startswith("Свет на ул. Павлова")

    raw_4 = 'Жду звонка, не знаю, дали им свет или нет.").'
    assert not _clean_fact_sentence(raw_4).endswith('").')
    assert _clean_fact_sentence(raw_4).endswith(".")


def test_topic_bundles_merge_shared_distinctive_entities():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        StoryCard(
            id="story:equator-fire",
            topic="Пожар на складе ТРЦ «Экватор»",
            importance="high",
            summary="В результате ночного удара горит склад ТРЦ «Экватор».",
            rubric_id="safety",
            useful_details=(),
            hard_facts=(),
        ),
        StoryCard(
            id="story:semya-damage",
            topic="Магазин «Семья» пострадал в ТРЦ «Экватор»",
            importance="medium",
            summary="Магазин «Семья» сообщил о повреждениях в ТРЦ «Экватор».",
            rubric_id="safety",
            useful_details=(),
            hard_facts=(),
        ),
        StoryCard(
            id="story:uley-relocation",
            topic="Супермаркет «Улей» переносит склад из ТРЦ «Экватор»",
            importance="medium",
            summary="Супермаркет «Улей» в «Экваторе» переносит склад на резервную площадку.",
            rubric_id="safety",
            useful_details=(),
            hard_facts=(),
        ),
    ]

    bundles = build_thematic_topic_bundles(cards)

    assert len(bundles) == 1
    assert set(bundles[0].story_ids) == {
        "story:equator-fire",
        "story:semya-damage",
        "story:uley-relocation",
    }


def test_topic_bundles_merge_unified_banking_stories():
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        StoryCard(
            id="story:sber",
            topic="Работа отделений Сбербанка",
            importance="medium",
            summary="Отделения Сбера работают по графику буднего дня.",
            rubric_id="civic_services",
            useful_details=(),
            hard_facts=(),
        ),
        StoryCard(
            id="story:psb-atms",
            topic="Банкоматы ПСБ и наличные",
            importance="medium",
            summary="В банкоматах ПСБ на проспекте Ленина доступно снятие наличных.",
            rubric_id="civic_services",
            useful_details=(),
            hard_facts=(),
        ),
    ]

    bundles = build_thematic_topic_bundles(cards)

    assert len(bundles) == 1
    assert set(bundles[0].story_ids) == {"story:sber", "story:psb-atms"}
    assert bundles[0].topic_key == "banking"
