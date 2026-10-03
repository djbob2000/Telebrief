"""Reader synthesis keeps exact fact membership while joining related reports."""

from __future__ import annotations

# ruff: noqa: S101
import datetime as dt

import pytest

from src.editorial_models import StoryCard
from src.publication.digest_composition import build_digest_composition
from src.publication.digest_coverage import build_digest_coverage_trace
from src.publication.digest_narrative import (
    _composition_narrative_plan,
    _composition_writer_payload,
    _parse_composition_writer_output,
    validate_digest_narrative,
)
from src.publication.digest_presentation import DigestPresentationPlan, RequiredDigestFact
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 10, 3, 15, tzinfo=dt.timezone.utc)


def _fixture(*extra_reports):
    reports = (
        ("electricity", "АКЗ", "На АКЗ свет включили на 15 минут после 62 дней без электричества."),
        ("electricity", "Крылова", "На Крылова больше 10 дней нет света."),
        (
            "electricity",
            "Центр, возле городской поликлиники",
            "В центре возле городской поликлиники вторые сутки нет света.",
        ),
        ("water", "", "Вода на верхние этажи поступает ночью раз в 5–6 дней."),
    ) + extra_reports
    cards = []
    facts = []
    evidence = {}
    for index, (service, location, text) in enumerate(reports, start=1):
        sid = f"story:{index}"
        ref = f"telegram:source:1:item:{index}:rev:{index}:frag:{index}"
        eid = f"{sid}:evidence:0:frag:{index}"
        cards.append(
            StoryCard(
                id=sid,
                topic=text,
                summary=text,
                importance="high",
                rubric_id="infrastructure",
                representative_source_refs=[ref],
            )
        )
        facts.append(
            RequiredDigestFact(
                fact_id=f"fact:{index}",
                rubric_id="infrastructure",
                subject_key=service,
                subject_label=service,
                story_ids=(sid,),
                support_ids=(eid,),
                text=text,
                original_location=location,
                observed_at=_NOW,
                epistemic_kind="service_access",
            )
        )
        evidence[eid] = PublicationEvidence(
            evidence_id=eid,
            story_id=index,
            text=text,
            source_text=text,
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=index,
            source_ref=ref,
            source_id=1,
            source_item_id=index,
            source_role="community",
            observed_at=_NOW,
        )
    presentation = DigestPresentationPlan(
        story_ids=tuple(c.id for c in cards), required_facts=tuple(facts)
    )
    composition = build_digest_composition(
        presentation,
        cards,
        evidence,
        edition_slug="",
        snapshot_at=_NOW,
        max_chars=4096,
        reserved_chars=70,
        include_statistics=False,
        rubric_labels={"infrastructure": "Коммунальная обстановка"},
    )
    presentation = presentation.with_composition(composition)
    plan = _composition_narrative_plan(
        cards=cards,
        rubrics=({"id": "infrastructure", "title": "Коммунальная обстановка"},),
        presentation_plan=presentation,
    )
    return cards, evidence, presentation, plan


def test_writer_receives_service_synthesis_navigation_without_merging_geography() -> None:
    cards, evidence, presentation, plan = _fixture()
    block = _composition_writer_payload(plan=plan, evidence=evidence, cards=cards)[0]

    groups = block["reader_synthesis_groups"]
    power = next(group for group in groups if group["service_topics"] == ["power"])
    water = next(group for group in groups if group["service_topics"] == ["water"])
    assert set(power["fact_ids"]) == {"fact:1", "fact:2", "fact:3"}
    assert water["fact_ids"] == ["fact:4"]
    assert power["navigation_only"] is True
    assert "canonical_area" not in power
    assert "service_state" not in power
    assert len(power["composition_unit_ids"]) == 3
    assert presentation.story_ids == tuple(card.id for card in cards)
    fact_rows = {f["fact_id"]: f for u in block["composition_units"] for f in u["facts"]}
    assert fact_rows["fact:1"]["original_location"] == "АКЗ"
    assert fact_rows["fact:2"]["original_location"] == "Крылова"
    assert fact_rows["fact:4"]["original_location"] == ""
    assert all(f["observed_time"] == _NOW.isoformat() for f in fact_rows.values())


def test_overlapping_place_wording_is_navigation_and_preserves_different_facts() -> None:
    cards, evidence, _, plan = _fixture(
        (
            "electricity",
            "",
            "Возле городской поликлиники перед отключением свет был сутки с перебоями.",
        ),
        ("electricity", "РТС", "На РТС света нет неделю."),
        ("electricity", "", "На АКЗ дали свет, а на РТС его нет неделю."),
    )
    block = _composition_writer_payload(plan=plan, evidence=evidence, cards=cards)[0]
    related = block["related_reporting_sets"]
    clinic = next(row for row in related if row["text_anchor"] == "возле городской поликлиники")
    rts = next(row for row in related if row["text_anchor"] == "ртс")
    assert set(clinic["fact_ids"]) == {"fact:3", "fact:5"}
    assert set(rts["fact_ids"]) == {"fact:6", "fact:7"}
    assert clinic["navigation_only"] is True
    assert "same_fact" not in clinic
    facts = {f["fact_id"] for unit in block["composition_units"] for f in unit["facts"]}
    assert facts == {f"fact:{i}" for i in range(1, 8)}


def test_synthesis_of_separate_street_units_keeps_full_coverage_and_grounding() -> None:
    cards, evidence, presentation, plan = _fixture()
    block = plan.blocks[0]
    unit_for_fact = {fid: unit.unit_id for unit in block.composition_units for fid in unit.fact_ids}
    texts = {fact.fact_id: fact.text for fact in block.required_facts}
    power_body = (
        "По сообщениям жителей, на АКЗ свет включили на 15 минут после 62 дней без электричества; "
        "на Крылова его нет больше 10 дней, а в центре возле поликлиники — вторые сутки."
    )
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [unit_for_fact[f"fact:{i}"] for i in (1, 2, 3)],
                        "covered_fact_ids": ["fact:1", "fact:2", "fact:3"],
                        "headline": "Свет",
                        "body": power_body,
                        "claims": [
                            {"text": texts[f"fact:{i}"], "covered_fact_ids": [f"fact:{i}"]}
                            for i in (1, 2, 3)
                        ],
                    },
                    {
                        "composition_unit_ids": [unit_for_fact["fact:4"]],
                        "covered_fact_ids": ["fact:4"],
                        "headline": "Вода",
                        "body": f"По сообщению жителя, {texts['fact:4'].lower()}",
                        "claims": [{"text": texts["fact:4"], "covered_fact_ids": ["fact:4"]}],
                    },
                ],
            }
        ],
    }
    draft = _parse_composition_writer_output(raw, plan=plan)
    supports = {
        ref: item.text
        for item in evidence.values()
        for ref in (item.evidence_id, item.source_ref, f"fragment:{item.fragment_id}")
    }
    validation = validate_digest_narrative(draft, plan, supports)
    assert validation.is_valid, validation.violations
    coverage = build_digest_coverage_trace(presentation, draft, plan)
    assert coverage.story_coverage == 1.0
    assert coverage.material_fact_coverage == 1.0
    assert len(draft.blocks[0].items) == 2
    assert set(draft.blocks[0].items[0].covered_story_ids) == {card.id for card in cards[:3]}

    raw["blocks"][0]["items"][0]["covered_fact_ids"].remove("fact:3")
    with pytest.raises(ValueError):
        _parse_composition_writer_output(raw, plan=plan)


def test_incomplete_model_claim_atoms_are_derived_from_fixed_facts() -> None:
    cards, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    unit_for_fact = {
        fact_id: unit.unit_id for unit in block.composition_units for fact_id in unit.fact_ids
    }
    fact_text = {fact.fact_id: fact.text for fact in block.required_facts}
    result = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            # Model omitted two units from this synthesized item's map;
                            # Python derives all three from its exact covered facts.
                            "composition_unit_ids": [unit_for_fact["fact:1"]],
                            "covered_fact_ids": ["fact:1", "fact:2", "fact:3"],
                            "headline": "Свет",
                            "body": "На АКЗ и Крылова перебои, а возле поликлиники света нет.",
                            # The writer omitted all three atom rows. Python owns the
                            # fixed fact-to-support mapping, so the parser derives them.
                            "claims": [],
                        },
                        {
                            "composition_unit_ids": [unit_for_fact["fact:4"]],
                            "covered_fact_ids": ["fact:4"],
                            "headline": "Вода",
                            "body": "По сообщению жителя, " + fact_text["fact:4"].lower(),
                            "claims": [
                                {
                                    "text": fact_text["fact:4"],
                                    "covered_fact_ids": ["fact:4"],
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        plan=plan,
    )
    power = result.blocks[0].items[0]
    assert set(power.covered_fact_ids) == {"fact:1", "fact:2", "fact:3"}
    assert {claim.text for claim in power.claims} == {
        fact_text["fact:1"],
        fact_text["fact:2"],
        fact_text["fact:3"],
    }
    assert {story_id for item in result.blocks[0].items for story_id in item.covered_story_ids} == {
        card.id for card in cards
    }


@pytest.mark.parametrize("invalid", [False, True])
def test_editor_recomposition_preserves_all_facts_or_rolls_back(invalid: bool) -> None:
    import asyncio
    import json
    from dataclasses import replace

    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import DigestNarrativeBlockDraft, DigestNarrativeDraft
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    _, evidence, _, plan = _fixture()
    original_plan = plan
    block = plan.blocks[0]
    untouched_block = replace(
        block,
        block_id="block:untouched",
        rubric_id="untouched",
        story_ids=(),
        support_ids=(),
        canonical_notes=(),
        required_facts=(),
        detail_support_ids_by_story=(),
        merge_group_by_story=(),
        detail_roles_by_story=(),
        presentation_modes_by_story=(),
        dashboard_support_ids_by_story=(),
        required_story_groups=(),
        support_ids_by_story=(),
        topic_bundles=(),
        composition_units=(),
        composition_fact_records=(),
        composition_relations=(),
    )
    plan = replace(plan, blocks=(*plan.blocks, untouched_block))
    units = {fid: unit.unit_id for unit in block.composition_units for fid in unit.fact_ids}
    texts = {fact.fact_id: fact.text for fact in block.required_facts}

    def item(fids):
        return {
            "composition_unit_ids": list(dict.fromkeys(units[fid] for fid in fids)),
            "covered_fact_ids": fids,
            "headline": "",
            "body": " ".join(texts[fid] for fid in fids),
            "claims": [{"text": texts[fid], "covered_fact_ids": [fid]} for fid in fids],
        }

    original = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [item(list(texts))],
                }
            ]
        },
        plan=original_plan,
    )
    long_item = replace(original.blocks[0].items[0], body="Подробное сообщение. " * 40)
    original = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id=block.block_id,
                items=(long_item,),
            ),
            DigestNarrativeBlockDraft(block_id=untouched_block.block_id, items=()),
        )
    )
    audit = audit_digest_prose_quality(original, evidence)
    assert "OVERLONG_SYNTHESIS" in {w.code for w in audit.warnings}

    captured_system_prompt = ""

    class Provider:
        async def chat_completion(self, **kwargs):
            nonlocal captured_system_prompt
            captured_system_prompt = kwargs["messages"][0]["content"]
            replacement = [item(["fact:1", "fact:2", "fact:3"]), item(["fact:4"])]
            replacement[0]["composition_unit_ids"] = [replacement[0]["composition_unit_ids"][0]]
            replacement[0]["claims"][0]["text"] = "3 октября жители сообщают об отключении света."
            if invalid:
                replacement.pop()
            return json.dumps(
                {"blocks": [{"block_id": block.block_id, "recomposed_items": replacement}]}
            )

    result = asyncio.run(
        DigestEditor(provider=Provider()).polish_and_compress(
            original,
            plan=plan,
            evidence=evidence,
            recompose_block_ids=(block.block_id,),
        )
    )
    assert "600 characters" in captured_system_prompt
    assert "source messages" in captured_system_prompt
    if invalid:
        assert result == original
    else:
        assert len(result.blocks[0].items) == 2
        assert {fid for i in result.blocks[0].items for fid in i.covered_fact_ids} == set(texts)
        assert {sid for i in result.blocks[0].items for sid in i.cited_support_ids} == {
            sid for i in original.blocks[0].items for sid in i.cited_support_ids
        }
        assert all("3 октября" not in claim.text for claim in result.blocks[0].items[0].claims)
        assert result.blocks[1].block_id == untouched_block.block_id
        assert result.blocks[1].items == ()


def test_editor_recomposition_requires_all_block_items_authorized() -> None:
    import asyncio

    from src.publication.digest_editor import DigestEditor

    _, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    raw_items = [
        {
            "composition_unit_ids": [unit.unit_id],
            "covered_fact_ids": list(unit.fact_ids),
            "headline": "",
            "body": "Наблюдение",
            "claims": [
                {"text": fact.text, "covered_fact_ids": [fact.fact_id]}
                for fact in block.required_facts
                if fact.fact_id in unit.fact_ids
            ],
        }
        for unit in block.composition_units
    ]
    draft = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": raw_items}]}, plan=plan
    )

    class Provider:
        async def chat_completion(self, **kwargs):
            raise AssertionError("Unapproved recomposition must not call provider")

    result = asyncio.run(
        DigestEditor(provider=Provider()).polish_and_compress(
            draft,
            plan=plan,
            evidence=evidence,
            recompose_block_ids=(block.block_id,),
            target_item_ids=(draft.blocks[0].items[0].item_id,),
        )
    )
    assert result == draft


def test_recomposition_preserves_summary_only_story_in_the_original_item() -> None:
    import asyncio
    import json
    from dataclasses import replace

    from src.publication.digest_composition import DigestCompositionUnit
    from src.publication.digest_editor import DigestEditor

    _, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    summary_id = "summary:service"
    summary_support_id = f"{summary_id}:evidence:1"
    unit = DigestCompositionUnit(
        unit_id="summary-unit:service",
        rubric_id=block.rubric_id,
        fact_ids=(),
        story_ids=(summary_id,),
        support_ids=(summary_support_id,),
        canonical_area_key="",
        priority=1,
    )
    plan_block = replace(
        block,
        story_ids=(*block.story_ids, summary_id),
        support_ids=(*block.support_ids, summary_support_id),
        composition_units=(*block.composition_units, unit),
    )
    plan = replace(plan, blocks=(plan_block,))
    evidence[summary_support_id] = PublicationEvidence(
        evidence_id=summary_support_id,
        story_id=999,
        text="В городе продолжает работать дежурное отделение.",
        source_text="В городе продолжает работать дежурное отделение.",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=999,
        source_ref="telegram:source:1:item:999:rev:1:frag:999",
        source_id=1,
        source_item_id=999,
        source_role="community",
        observed_at=_NOW,
    )
    facts = [fact.fact_id for fact in plan_block.required_facts]
    units = {fid: u.unit_id for u in plan_block.composition_units for fid in u.fact_ids}
    texts = {fact.fact_id: fact.text for fact in plan_block.required_facts}
    source = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            "composition_unit_ids": [units[fid] for fid in facts],
                            "covered_fact_ids": facts,
                            "headline": "Электроснабжение",
                            "body": "По сообщениям жителей, " + " ".join(texts.values()),
                            "claims": [
                                {"text": texts[fid], "covered_fact_ids": [fid]} for fid in facts
                            ],
                        },
                        {
                            "composition_unit_ids": [unit.unit_id],
                            "covered_fact_ids": [],
                            "headline": "",
                            "body": "В городе продолжает работать дежурное отделение.",
                            "claims": [
                                {
                                    "text": "В городе продолжает работать дежурное отделение.",
                                    "covered_fact_ids": [],
                                    "summary_unit_ids": [unit.unit_id],
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        plan=plan,
    )
    summary_item = source.blocks[0].items[1]

    replacement = {
        # Even if the model tries to weave in this summary unit, the server removes
        # that membership and restores its original item after parsing.
        "composition_unit_ids": [units[fid] for fid in facts] + [unit.unit_id],
        "covered_fact_ids": facts,
        "headline": "Электроснабжение",
        "body": "По сообщениям жителей, " + " ".join(texts.values()),
        "claims": [{"text": texts[fid], "covered_fact_ids": [fid]} for fid in facts],
    }

    class Provider:
        async def chat_completion(self, **kwargs):
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "items": [],
                            "merges": [],
                            "recomposed_items": [replacement],
                        }
                    ]
                }
            )

    result = asyncio.run(
        DigestEditor(provider=Provider()).polish_and_compress(
            source,
            plan=plan,
            evidence=evidence,
            target_item_ids=tuple(item.item_id for item in source.blocks[0].items),
            recompose_block_ids=(block.block_id,),
        )
    )
    assert any(item.covered_story_ids == (summary_id,) for item in result.blocks[0].items)
    assert summary_item.body in {item.body for item in result.blocks[0].items}
    assert {sid for item in result.blocks[0].items for sid in item.covered_story_ids} == {
        *block.story_ids,
        summary_id,
    }


def test_scattered_power_items_raise_nonblocking_synthesis_prompt() -> None:
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    reports = tuple(
        ("electricity", location, f"На {location} несколько дней нет света.")
        for location in ("РТС", "АКЗ", "Крылова")
    )
    _, evidence, _, plan = _fixture(*reports)
    block = plan.blocks[0]
    unit_groups = (
        block.composition_units[:3],
        block.composition_units[3:5],
        block.composition_units[5:],
    )
    raw_items = []
    for group in unit_groups:
        fact_ids = [fact_id for unit in group for fact_id in unit.fact_ids]
        facts = [fact for fact in block.required_facts if fact.fact_id in fact_ids]
        raw_items.append(
            {
                "composition_unit_ids": [unit.unit_id for unit in group],
                "covered_fact_ids": fact_ids,
                "headline": "",
                "body": " ".join(fact.text for fact in facts),
                "claims": [
                    {"text": fact.text, "covered_fact_ids": [fact.fact_id]} for fact in facts
                ],
            }
        )
    assert len(raw_items) == 3
    raw_items[0]["body"] = "В отдельной публикации жители уточнили: " + str(raw_items[0]["body"])
    draft = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": raw_items}]}, plan=plan
    )
    audit = audit_digest_prose_quality(draft, evidence)
    warning = next(w for w in audit.warnings if w.code == "FRAGMENTED_SERVICE_REPORTS")
    assert warning.block_id == block.block_id
    assert "SOURCE_META_NARRATION" in {w.code for w in audit.warnings}
    assert audit.is_publishable


def test_named_city_chat_reference_is_removed_without_inventing_a_poster() -> None:
    from src.publication.digest_narrative import _fix_chat_leaks

    repaired = _fix_chat_leaks(
        "В Бердянском чате сообщают, что в городе заполняют систему отопления."
    )
    assert repaired == "Сообщается, что в городе заполняют систему отопления."
    assert "чате" not in repaired.casefold()
