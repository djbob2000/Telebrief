"""Faithful journalistic digest product contract tests.

Spec: AGENTS.md §0.1, §0.2, §0.6, §0.9.
Guarantees:
- Single-source community reports are legitimate publication material when faithfully attributed.
- Lack of corroboration must never drop a legitimate report.
- Unsupported causes/mechanisms/numbers (fabrication) are strictly rejected.
- Multi-story synthesis produces a cohesive reader digest without dashboard/traffic-light sections.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.editorial_models import EditorialAnalysis, PreparedBundle, StoryCard, StoryElement
from src.publication.digest_coverage import build_digest_coverage_trace
from src.publication.digest_narrative import (
    DigestClaimAtom,
    DigestEditorialItemDraft,
    DigestNarrativeBlock,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    DigestNarrativePlan,
    validate_digest_narrative,
)
from src.publication.digest_presentation import (
    CitySituationPresentationPlan,
    DigestPresentationPlan,
    DigestStoryPresentation,
    RequiredDigestFact,
)
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.evidence import PublicationEvidence
from src.publication.renderers import PublicationDigestRenderer

pytestmark = pytest.mark.unit

_NOW = dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.timezone.utc)


def _make_evidence(
    *,
    evidence_id: str,
    story_id: int = 1,
    text: str,
    source_text: str | None = None,
    kind: str = "community_report",
    publication_use: str = "PUBLISH",
    fragment_id: int = 1,
    source_ref: str = "telegram:1",
    source_id: int = 1,
    source_item_id: int = 1,
    source_role: str = "primary",
    observed_at: dt.datetime | None = None,
) -> PublicationEvidence:
    return PublicationEvidence(
        evidence_id=evidence_id,
        story_id=story_id,
        text=text,
        source_text=source_text or text,
        kind=kind,
        publication_use=publication_use,  # type: ignore[arg-type]
        fragment_id=fragment_id,
        source_ref=source_ref,
        source_id=source_id,
        source_item_id=source_item_id,
        source_role=source_role,
        observed_at=observed_at or _NOW,
    )


def _make_block(
    *,
    block_id: str,
    rubric_id: str,
    rubric_title: str = "",
    story_ids: tuple[str, ...],
    support_ids: tuple[str, ...],
    canonical_notes: tuple[str, ...] = (),
    required_story_groups: tuple[tuple[str, ...], ...] = (),
    support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = (),
    required_facts: tuple[RequiredDigestFact, ...] = (),
) -> DigestNarrativeBlock:
    return DigestNarrativeBlock(
        block_id=block_id,
        rubric_id=rubric_id,
        rubric_title=rubric_title or rubric_id,
        story_ids=story_ids,
        support_ids=support_ids,
        canonical_notes=canonical_notes,
        required_facts=required_facts,
        required_story_groups=required_story_groups,
        support_ids_by_story=support_ids_by_story,
    )


def test_single_source_community_report_regression() -> None:
    """A single-source community report must pass validation and achieve 100% coverage with attribution."""
    evidence = _make_evidence(
        evidence_id="story:1:evidence:1",
        story_id=1,
        text="На Лисках света нет с утра",
        source_text="На Лисках света нет с утра",
        kind="community_report",
        publication_use="PUBLISH",
    )

    plan_block = _make_block(
        block_id="block:utilities:0",
        rubric_id="utilities",
        story_ids=("story:1",),
        support_ids=("story:1:evidence:1",),
        support_ids_by_story=(("story:1", ("story:1:evidence:1",)),),
        required_story_groups=(("story:1",),),
    )
    narrative_plan = DigestNarrativePlan(blocks=(plan_block,))

    claim = DigestClaimAtom(
        text="На Лисках света нет с утра.",
        covered_story_ids=("story:1",),
        cited_support_ids=("story:1:evidence:1",),
    )
    item = DigestEditorialItemDraft(
        headline="Перебои со светом на Лисках",
        body="По сообщениям жителей района, электричества нет с утра.",
        emoji="⚡️",
        covered_story_ids=("story:1",),
        cited_support_ids=("story:1:evidence:1",),
        claims=(claim,),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(item,),
            ),
        )
    )

    support_map = {"story:1:evidence:1": evidence.text}
    validation = validate_digest_narrative(
        draft,
        narrative_plan,
        support_index=support_map,
        all_known_draft_supports=("story:1:evidence:1",),
    )

    presentation_plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DETAIL_ONLY",
                city_situation_group_ids=(),
                detail_support_ids=("story:1:evidence:1",),
                merge_group_id="story:1",
            ),
        ),
    )
    trace = build_digest_coverage_trace(
        presentation_plan,
        draft,
        narrative_plan=narrative_plan,
    )

    renderer = PublicationDigestRenderer(include_statistics=False)
    card = StoryCard(
        id="story:1",
        topic="Электроснабжение на Лисках",
        importance="medium",
        summary="Отсутствие света на Лисках",
        rubric_id="utilities",
    )
    dummy_frozen = FrozenEditorialInput(
        run_id=1,
        analysis=EditorialAnalysis(
            cards=[card],
            evidence={"story:1:evidence:1": evidence},
        ),
        writer_bundle=PreparedBundle(
            records={},
            prompt_text="",
            total_messages=0,
            candidate_count=0,
        ),
    )
    _, _, rendered = renderer.render_grouped_digest(
        dummy_frozen,
        narrative_draft=draft,
        snapshot_at=_NOW,
    )

    assert validation.is_valid, f"Validation failed: {validation.violations}"
    assert trace.story_coverage == 1.0
    assert "По сообщениям жителей" in rendered


def test_anti_fabrication_pair() -> None:
    """Faithful community report is allowed, but invented cause/substation failure is rejected."""
    evidence = _make_evidence(
        evidence_id="story:1:evidence:1",
        story_id=1,
        text="На Лисках света нет с утра",
        source_text="На Лисках света нет с утра",
        kind="community_report",
        publication_use="PUBLISH",
    )
    support_map = {"story:1:evidence:1": evidence.text}

    plan_block = _make_block(
        block_id="block:utilities:0",
        rubric_id="utilities",
        story_ids=("story:1",),
        support_ids=("story:1:evidence:1",),
        support_ids_by_story=(("story:1", ("story:1:evidence:1",)),),
        required_story_groups=(("story:1",),),
    )
    narrative_plan = DigestNarrativePlan(blocks=(plan_block,))

    # 1. Allowed faithful variant
    allowed_item = DigestEditorialItemDraft(
        headline="Отсутствие света на Лисках",
        body="По сообщениям жителей, на Лисках света нет с утра.",
        emoji="⚡️",
        covered_story_ids=("story:1",),
        cited_support_ids=("story:1:evidence:1",),
        claims=(
            DigestClaimAtom(
                text="На Лисках света нет с утра.",
                covered_story_ids=("story:1",),
                cited_support_ids=("story:1:evidence:1",),
            ),
        ),
    )
    allowed_draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(allowed_item,),
            ),
        )
    )
    allowed_val = validate_digest_narrative(
        allowed_draft,
        narrative_plan,
        support_index=support_map,
        all_known_draft_supports=("story:1:evidence:1",),
    )
    assert allowed_val.is_valid, f"Allowed draft failed: {allowed_val.violations}"

    # 2. Fabricated variant inventing substation cause
    fabricated_item = DigestEditorialItemDraft(
        headline="Авария на подстанции",
        body="Авария на подстанции оставила Лиски без света с утра.",
        emoji="⚡️",
        covered_story_ids=("story:1",),
        cited_support_ids=("story:1:evidence:1",),
        claims=(
            DigestClaimAtom(
                text="Авария на подстанции оставила Лиски без света с утра.",
                covered_story_ids=("story:1",),
                cited_support_ids=("story:1:evidence:1",),
            ),
        ),
    )
    fabricated_draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(fabricated_item,),
            ),
        )
    )
    fabricated_val = validate_digest_narrative(
        fabricated_draft,
        narrative_plan,
        support_index=support_map,
        all_known_draft_supports=("story:1:evidence:1",),
    )
    assert not fabricated_val.is_valid
    assert any(
        "UNSUPPORTED_DIGEST_RELATION" in v or "UNSUPPORTED_CONCRETE_CLAIM" in v
        for v in fabricated_val.violations
    )


def test_multi_story_editorial_synthesis_fixture() -> None:
    """12 stories across categories synthesize into dense reader items without dashboard."""
    stories_data = [
        # 4 electricity stories
        ("story:el:akz", "utilities", "sup:el:1", "На АКЗ нет света с утра"),
        ("story:el:liski", "utilities", "sup:el:2", "На Лисках отключили электричество"),
        ("story:el:kolonia", "utilities", "sup:el:3", "В Колонии перебои со светом"),
        ("story:el:center", "utilities", "sup:el:4", "В центре низкое напряжение около 160 вольт"),
        # 2 water stories
        ("story:w:akz", "utilities", "sup:w:1", "На АКЗ слабый напор воды на верхних этажах"),
        (
            "story:w:nagornaya",
            "utilities",
            "sup:w:2",
            "На Нагорной временно отключили водоснабжение",
        ),
        # 2 security events
        (
            "story:sec:night",
            "security",
            "sup:sec:1",
            "Ночью жители сообщали о громких звуках со стороны моря",
        ),
        (
            "story:sec:morning",
            "security",
            "sup:sec:2",
            "Утром в городе сохранялась спокойная обстановка",
        ),
        # connectivity
        (
            "story:tel:mobile",
            "telecom",
            "sup:tel:1",
            "Мобильная связь оператора Миранда работает с перебоями",
        ),
        # transport
        ("story:tr:bus4", "transport", "sup:tr:1", "Автобус №4 ходит по временному расписанию"),
        # social
        (
            "story:soc:pension",
            "social",
            "sup:soc:1",
            "Пенсионный фонд ведёт приём граждан по записи",
        ),
        # city-life
        (
            "story:life:school",
            "city_life",
            "sup:life:1",
            "Спортивная школа открыла набор детей на новый учебный год",
        ),
    ]

    planned_story_count = len(stories_data)
    cards = []
    evidence_dict = {}
    support_map = {}
    for sid, rubric, supid, text in stories_data:
        cards.append(
            StoryCard(
                id=sid,
                topic=text,
                importance="medium",
                summary=text,
                rubric_id=rubric,
                hard_facts=[StoryElement(text=text, source_refs=[supid])],
            )
        )
        evi = _make_evidence(
            evidence_id=supid,
            story_id=int(sid.split(":")[-1]) if sid.split(":")[-1].isdigit() else 1,
            text=text,
            source_text=text,
            kind="community_report",
            publication_use="PUBLISH",
        )
        evidence_dict[supid] = evi
        support_map[supid] = text

    # Synthesis into 6 items across 5 rubrics
    blocks = [
        _make_block(
            block_id="block:utilities:0",
            rubric_id="utilities",
            story_ids=(
                "story:el:akz",
                "story:el:liski",
                "story:el:kolonia",
                "story:el:center",
                "story:w:akz",
                "story:w:nagornaya",
            ),
            support_ids=("sup:el:1", "sup:el:2", "sup:el:3", "sup:el:4", "sup:w:1", "sup:w:2"),
            support_ids_by_story=(
                ("story:el:akz", ("sup:el:1",)),
                ("story:el:liski", ("sup:el:2",)),
                ("story:el:kolonia", ("sup:el:3",)),
                ("story:el:center", ("sup:el:4",)),
                ("story:w:akz", ("sup:w:1",)),
                ("story:w:nagornaya", ("sup:w:2",)),
            ),
            required_story_groups=(
                ("story:el:akz", "story:el:liski", "story:el:kolonia", "story:el:center"),
                ("story:w:akz", "story:w:nagornaya"),
            ),
        ),
        _make_block(
            block_id="block:security:0",
            rubric_id="security",
            story_ids=("story:sec:night", "story:sec:morning"),
            support_ids=("sup:sec:1", "sup:sec:2"),
            support_ids_by_story=(
                ("story:sec:night", ("sup:sec:1",)),
                ("story:sec:morning", ("sup:sec:2",)),
            ),
            required_story_groups=(("story:sec:night", "story:sec:morning"),),
        ),
        _make_block(
            block_id="block:telecom:0",
            rubric_id="telecom",
            story_ids=("story:tel:mobile",),
            support_ids=("sup:tel:1",),
            support_ids_by_story=(("story:tel:mobile", ("sup:tel:1",)),),
            required_story_groups=(("story:tel:mobile",),),
        ),
        _make_block(
            block_id="block:transport:0",
            rubric_id="transport",
            story_ids=("story:tr:bus4",),
            support_ids=("sup:tr:1",),
            support_ids_by_story=(("story:tr:bus4", ("sup:tr:1",)),),
            required_story_groups=(("story:tr:bus4",),),
        ),
        _make_block(
            block_id="block:society:0",
            rubric_id="society",
            story_ids=("story:soc:pension", "story:life:school"),
            support_ids=("sup:soc:1", "sup:life:1"),
            support_ids_by_story=(
                ("story:soc:pension", ("sup:soc:1",)),
                ("story:life:school", ("sup:life:1",)),
            ),
            required_story_groups=(("story:soc:pension", "story:life:school"),),
        ),
    ]
    narrative_plan = DigestNarrativePlan(blocks=tuple(blocks))

    # Draft items
    power_item = DigestEditorialItemDraft(
        headline="Перебои со светом в нескольких районах",
        body="По сообщениям жителей, электричества нет на АКЗ и Лисках, в Колонии фиксируют сбои, а в центре города наблюдается низкое напряжение около 160 вольт.",
        emoji="⚡️",
        covered_story_ids=("story:el:akz", "story:el:liski", "story:el:kolonia", "story:el:center"),
        cited_support_ids=("sup:el:1", "sup:el:2", "sup:el:3", "sup:el:4"),
        claims=(
            DigestClaimAtom(
                text="На АКЗ нет света с утра.",
                covered_story_ids=("story:el:akz",),
                cited_support_ids=("sup:el:1",),
            ),
            DigestClaimAtom(
                text="На Лисках отключили электричество.",
                covered_story_ids=("story:el:liski",),
                cited_support_ids=("sup:el:2",),
            ),
            DigestClaimAtom(
                text="В Колонии перебои со светом.",
                covered_story_ids=("story:el:kolonia",),
                cited_support_ids=("sup:el:3",),
            ),
            DigestClaimAtom(
                text="В центре низкое напряжение около 160 вольт.",
                covered_story_ids=("story:el:center",),
                cited_support_ids=("sup:el:4",),
            ),
        ),
    )
    water_item = DigestEditorialItemDraft(
        headline="Обстановка с водоснабжением",
        body="Жители АКЗ отмечают слабый напор воды на верхних этажах, а на Нагорной подача воды временно приостановлена.",
        emoji="💧",
        covered_story_ids=("story:w:akz", "story:w:nagornaya"),
        cited_support_ids=("sup:w:1", "sup:w:2"),
        claims=(
            DigestClaimAtom(
                text="На АКЗ слабый напор воды на верхних этажах.",
                covered_story_ids=("story:w:akz",),
                cited_support_ids=("sup:w:1",),
            ),
            DigestClaimAtom(
                text="На Нагорной временно отключили водоснабжение.",
                covered_story_ids=("story:w:nagornaya",),
                cited_support_ids=("sup:w:2",),
            ),
        ),
    )
    sec_item = DigestEditorialItemDraft(
        headline="Обстановка в городе и ночные звуки",
        body="Ночью горожане сообщали о громких звуках со стороны моря, к утру ситуация в городе оставалась спокойной.",
        emoji="💥",
        covered_story_ids=("story:sec:night", "story:sec:morning"),
        cited_support_ids=("sup:sec:1", "sup:sec:2"),
        claims=(
            DigestClaimAtom(
                text="Ночью жители сообщали о громких звуках со стороны моря.",
                covered_story_ids=("story:sec:night",),
                cited_support_ids=("sup:sec:1",),
            ),
            DigestClaimAtom(
                text="Утром в городе сохранялась спокойная обстановка.",
                covered_story_ids=("story:sec:morning",),
                cited_support_ids=("sup:sec:2",),
            ),
        ),
    )
    tel_item = DigestEditorialItemDraft(
        headline="Работа мобильной связи",
        body="Абоненты оператора Миранда сообщают о перебоях в работе мобильной связи.",
        emoji="📶",
        covered_story_ids=("story:tel:mobile",),
        cited_support_ids=("sup:tel:1",),
        claims=(
            DigestClaimAtom(
                text="Мобильная связь оператора Миранда работает с перебоями.",
                covered_story_ids=("story:tel:mobile",),
                cited_support_ids=("sup:tel:1",),
            ),
        ),
    )
    tr_item = DigestEditorialItemDraft(
        headline="Движение общественного транспорта",
        body="Автобус №4 курсирует по временному расписанию.",
        emoji="🚌",
        covered_story_ids=("story:tr:bus4",),
        cited_support_ids=("sup:tr:1",),
        claims=(
            DigestClaimAtom(
                text="Автобус №4 ходит по временному расписанию.",
                covered_story_ids=("story:tr:bus4",),
                cited_support_ids=("sup:tr:1",),
            ),
        ),
    )
    soc_item = DigestEditorialItemDraft(
        headline="Городские службы и секции",
        body="Пенсионный фонд ведёт приём граждан по предварительной записи. Спортивная школа объявила об открытии набора детей на новый учебный год.",
        emoji="🏢",
        covered_story_ids=("story:soc:pension", "story:life:school"),
        cited_support_ids=("sup:soc:1", "sup:life:1"),
        claims=(
            DigestClaimAtom(
                text="Пенсионный фонд ведёт приём граждан по записи.",
                covered_story_ids=("story:soc:pension",),
                cited_support_ids=("sup:soc:1",),
            ),
            DigestClaimAtom(
                text="Спортивная школа открыла набор детей на новый учебный год.",
                covered_story_ids=("story:life:school",),
                cited_support_ids=("sup:life:1",),
            ),
        ),
    )

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(block_id="block:utilities:0", items=(power_item, water_item)),
            DigestNarrativeBlockDraft(block_id="block:security:0", items=(sec_item,)),
            DigestNarrativeBlockDraft(block_id="block:telecom:0", items=(tel_item,)),
            DigestNarrativeBlockDraft(block_id="block:transport:0", items=(tr_item,)),
            DigestNarrativeBlockDraft(block_id="block:society:0", items=(soc_item,)),
        )
    )

    # Presentations
    story_presentations = tuple(
        DigestStoryPresentation(
            story_id=sid,
            mode="DETAIL_ONLY",
            city_situation_group_ids=(),
            detail_support_ids=(supid,),
            merge_group_id=sid,
        )
        for sid, _, supid, _ in stories_data
    )
    presentation_plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(groups=(), covered_source_refs=()),
        story_presentations=story_presentations,
    )

    trace = build_digest_coverage_trace(
        presentation_plan,
        draft,
        narrative_plan=narrative_plan,
    )

    all_items = [it for b in draft.blocks for it in b.items]
    rendered_item_count = len(all_items)
    multi_story_item_count = sum(1 for it in all_items if len(it.covered_story_ids) > 1)

    renderer = PublicationDigestRenderer(include_statistics=False)
    dummy_frozen = FrozenEditorialInput(
        run_id=1,
        analysis=EditorialAnalysis(
            cards=cards,
            evidence=evidence_dict,
        ),
        writer_bundle=PreparedBundle(
            records={},
            prompt_text="",
            total_messages=0,
            candidate_count=0,
        ),
    )
    _, _, rendered = renderer.render_grouped_digest(
        dummy_frozen,
        narrative_draft=draft,
        snapshot_at=_NOW,
    )

    assert trace.story_coverage == 1.0
    assert trace.material_fact_coverage == 1.0
    assert rendered_item_count < planned_story_count
    assert multi_story_item_count >= 2
    assert "City Situation" not in rendered
    assert "Ситуация в городе" not in rendered
