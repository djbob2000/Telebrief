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
    assert all("observed_time" not in f for f in fact_rows.values())
    assert all(r.observed_time == _NOW for b in plan.blocks for r in b.composition_fact_records)


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


def test_incomplete_writer_coverage_can_be_assessed_only_before_editor_repair() -> None:
    from src.publication.errors import DigestCoverageInvariantError

    _, _, presentation, plan = _fixture()
    block = plan.blocks[0]
    unit_for_fact = {
        str(fact_id): str(unit.unit_id)
        for unit in block.composition_units
        for fact_id in unit.fact_ids
    }
    omitted = str(block.required_facts[0].fact_id)
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [unit_for_fact[str(fact.fact_id)]],
                        "covered_fact_ids": [str(fact.fact_id)],
                        "headline": "",
                        "body": str(fact.text),
                        "claims": [],
                    }
                    for fact in block.required_facts
                    if str(fact.fact_id) != omitted
                ],
            }
        ]
    }
    draft = _parse_composition_writer_output(
        raw,
        plan=plan,
        allow_incomplete_fact_coverage=True,
    )

    with pytest.raises(DigestCoverageInvariantError):
        build_digest_coverage_trace(presentation, draft, plan)
    provisional = build_digest_coverage_trace(
        presentation, draft, plan, allow_incomplete_coverage=True
    )
    assert provisional.story_coverage < 1.0
    assert provisional.material_fact_coverage < 1.0


def test_incomplete_writer_fact_coverage_can_be_parsed_only_for_editor_repair() -> None:
    _, _, _, plan = _fixture()
    block = plan.blocks[0]
    omitted_fact_id = str(block.required_facts[-1].fact_id)
    item_units = {
        str(fact_id): str(unit.unit_id)
        for unit in block.composition_units
        for fact_id in unit.fact_ids
    }
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [item_units[str(fact.fact_id)]],
                        "covered_fact_ids": [str(fact.fact_id)],
                        "headline": "",
                        "body": str(fact.text),
                        "claims": [],
                    }
                    for fact in block.required_facts
                    if str(fact.fact_id) != omitted_fact_id
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="fact partition mismatch"):
        _parse_composition_writer_output(raw, plan=plan)

    draft = _parse_composition_writer_output(
        raw, plan=plan, allow_incomplete_fact_coverage=True
    )
    assert omitted_fact_id not in {
        fact_id for item in draft.blocks[0].items for fact_id in item.covered_fact_ids
    }


def test_writer_accepts_a_repairable_omission_after_one_provider_call() -> None:
    import asyncio
    import json

    from src.publication.digest_narrative import DigestNarrativeWriter

    cards, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    omitted_fact_id = str(block.required_facts[-1].fact_id)
    unit_for_fact = {
        str(fact_id): str(unit.unit_id)
        for unit in block.composition_units
        for fact_id in unit.fact_ids
    }
    response = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [unit_for_fact[str(fact.fact_id)]],
                        "covered_fact_ids": [str(fact.fact_id)],
                        "headline": "",
                        "body": str(fact.text),
                        "claims": [],
                    }
                    for fact in block.required_facts
                    if str(fact.fact_id) != omitted_fact_id
                ],
            }
        ]
    }

    class Provider:
        calls = 0

        async def chat_completion(self, **kwargs):
            self.calls += 1
            return json.dumps(response)

    provider = Provider()
    draft = asyncio.run(
        DigestNarrativeWriter(provider)._generate_composition_draft(
            plan=plan,
            cards=cards,
            evidence=evidence,
            language="Russian",
            max_output_tokens=4096,
            model=None,
        )
    )
    assert provider.calls == 1
    assert omitted_fact_id not in {
        fact_id for item in draft.blocks[0].items for fact_id in item.covered_fact_ids
    }


def test_writer_fact_ids_recover_an_unknown_composition_unit_label() -> None:
    _, _, _, plan = _fixture()
    block = plan.blocks[0]
    fact = block.required_facts[0]
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": ["composition:318"],
                        "covered_fact_ids": [str(fact.fact_id)],
                        "headline": "",
                        "body": str(fact.text),
                        "claims": [],
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="unknown composition_unit_id"):
        _parse_composition_writer_output(raw, plan=plan)

    draft = _parse_composition_writer_output(
        raw,
        plan=plan,
        allow_incomplete_fact_coverage=True,
        allow_unmapped_writer_unit_ids=True,
    )
    assert draft.blocks[0].items[0].composition_unit_ids == (
        next(unit.unit_id for unit in block.composition_units if fact.fact_id in unit.fact_ids),
    )


def test_writer_duplicate_fact_membership_can_reach_editor_but_is_not_finally_valid() -> None:
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestRecompositionError

    _, _, _, plan = _fixture()
    block = plan.blocks[0]
    facts = list(block.required_facts)
    unit_for_fact = {
        str(fact_id): str(unit.unit_id)
        for unit in block.composition_units
        for fact_id in unit.fact_ids
    }
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [unit_for_fact[str(facts[0].fact_id)]],
                        "covered_fact_ids": [str(facts[0].fact_id), str(facts[1].fact_id)],
                        "headline": "",
                        "body": f"{facts[0].text} {facts[1].text}",
                        "claims": [],
                    },
                    {
                        "composition_unit_ids": [unit_for_fact[str(facts[2].fact_id)]],
                        "covered_fact_ids": [str(facts[0].fact_id), str(facts[2].fact_id)],
                        "headline": "",
                        "body": f"{facts[0].text} {facts[2].text}",
                        "claims": [],
                    },
                    {
                        "composition_unit_ids": [unit_for_fact[str(facts[3].fact_id)]],
                        "covered_fact_ids": [str(facts[3].fact_id)],
                        "headline": "",
                        "body": str(facts[3].text),
                        "claims": [],
                    },
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="partition mismatch"):
        _parse_composition_writer_output(raw, plan=plan)

    writer_draft = _parse_composition_writer_output(
        raw,
        plan=plan,
        allow_incomplete_fact_coverage=True,
        allow_unmapped_writer_unit_ids=True,
        allow_duplicate_writer_fact_ids=True,
    )
    scope = build_digest_block_edit_scope(
        writer_draft, plan=plan, block_ids=[block.block_id]
    )
    validate_digest_block_replacement(
        writer_draft,
        writer_draft,
        scope=scope,
        plan=plan,
        allow_incomplete_fact_coverage=True,
        allow_duplicate_fact_coverage=True,
    )
    with pytest.raises(DigestRecompositionError, match="MEMBERSHIP"):
        validate_digest_block_replacement(
            writer_draft, writer_draft, scope=scope, plan=plan
        )


@pytest.mark.parametrize("invalid", [False, True])
def test_editor_recomposition_preserves_all_facts_or_rolls_back(invalid: bool) -> None:
    import asyncio
    import json
    from dataclasses import replace

    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import DigestNarrativeBlockDraft, DigestNarrativeDraft
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    _, evidence, _, plan = _fixture(
        ("electricity", "Лиски", "В Лисках подали электричество после отключения.")
    )
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
                    "items": [
                        item(["fact:1", "fact:2", "fact:3", "fact:5"]),
                        item(["fact:4"]),
                    ],
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
                items=(long_item, original.blocks[0].items[1]),
            ),
            DigestNarrativeBlockDraft(block_id=untouched_block.block_id, items=()),
        )
    )
    audit = audit_digest_prose_quality(original, evidence)
    assert "OVERLONG_SYNTHESIS" in {w.code for w in audit.warnings}

    captured_system_prompt = ""
    captured_user_prompt = ""

    class Provider:
        async def chat_completion(self, **kwargs):
            nonlocal captured_system_prompt
            nonlocal captured_user_prompt
            captured_system_prompt = kwargs["messages"][0]["content"]
            captured_user_prompt = kwargs["messages"][1]["content"]
            replacement = [item(["fact:1", "fact:2", "fact:3", "fact:5"])]
            replacement[0]["composition_unit_ids"] = [replacement[0]["composition_unit_ids"][0]]
            replacement[0]["claims"][0]["text"] = "3 октября жители сообщают об отключении света."
            if invalid:
                replacement.pop()
            return json.dumps(
                {"blocks": [{"block_id": block.block_id, "recomposed_items": replacement}]}
            )

    edit = DigestEditor(provider=Provider()).polish_and_compress(
        original,
        plan=plan,
        evidence=evidence,
        target_item_ids=(original.blocks[0].items[0].item_id,),
        recompose_block_ids=(block.block_id,),
        violations=[f"STYLE_OBSERVATION:fragment_{index}" for index in range(10)]
        + ["EDITORIAL_CONSTRAINT: Previous response omitted fact:1 and fact:5."],
    )
    if invalid:
        with pytest.raises(ValueError, match="missing facts"):
            asyncio.run(edit)
    else:
        result = asyncio.run(edit)
    assert "600 characters" in captured_system_prompt
    assert "source messages" in captured_system_prompt
    assert "Previous response omitted fact:1 and fact:5." in captured_system_prompt
    if not invalid:
        assert json.loads(captured_user_prompt)["target_recomposition_fact_ids"] == sorted(
            original.blocks[0].items[0].covered_fact_ids
        )
        assert len(result.blocks[0].items) == 2
        assert {fid for i in result.blocks[0].items for fid in i.covered_fact_ids} == set(texts)
        assert {sid for i in result.blocks[0].items for sid in i.cited_support_ids} == {
            sid for i in original.blocks[0].items for sid in i.cited_support_ids
        }
        assert all("3 октября" not in claim.text for claim in result.blocks[0].items[0].claims)
        assert result.blocks[0].items[1].body == original.blocks[0].items[1].body
        assert result.blocks[1].block_id == untouched_block.block_id
        assert result.blocks[1].items == ()


def test_editor_recomposition_changes_only_authorized_items() -> None:
    import asyncio
    import json

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
            request = json.loads(kwargs["messages"][1]["content"])
            assert request["target_item_ids"] == [draft.blocks[0].items[0].item_id]
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "recomposed_items": [
                                {**raw_items[0], "body": "Пересобранное наблюдение."}
                            ],
                        }
                    ]
                }
            )

    result = asyncio.run(
        DigestEditor(provider=Provider()).polish_and_compress(
            draft,
            plan=plan,
            evidence=evidence,
            recompose_block_ids=(block.block_id,),
            target_item_ids=(draft.blocks[0].items[0].item_id,),
        )
    )
    assert result.blocks[0].items[0].body == "Пересобранное наблюдение."
    assert result.blocks[0].items[1:] == draft.blocks[0].items[1:]


def test_recomposition_rejects_summary_membership_from_an_untargeted_item() -> None:
    import asyncio
    import json
    from dataclasses import replace

    from src.publication.digest_composition import DigestCompositionUnit
    from src.publication.digest_editor import DigestEditor, DigestRecompositionError

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
        # The editor may not borrow the standalone summary unit when recomposing facts.
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

    with pytest.raises(DigestRecompositionError, match="targeted summary units"):
        asyncio.run(
            DigestEditor(provider=Provider()).polish_and_compress(
                source,
                plan=plan,
                evidence=evidence,
                target_item_ids=tuple(item.item_id for item in source.blocks[0].items),
                recompose_block_ids=(block.block_id,),
            )
        )
    assert any(item.covered_story_ids == (summary_id,) for item in source.blocks[0].items)
    assert summary_item.body in {item.body for item in source.blocks[0].items}
    assert {sid for item in source.blocks[0].items for sid in item.covered_story_ids} == {
        *block.story_ids,
        summary_id,
    }


def test_recomposition_can_regroup_facts_from_an_item_with_a_summary_unit() -> None:
    import asyncio
    import json
    from dataclasses import replace

    from src.publication.digest_composition import DigestCompositionUnit
    from src.publication.digest_editor import DigestEditor

    _, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    summary_id = "summary:power"
    summary_support_id = f"{summary_id}:evidence:1"
    summary_unit = DigestCompositionUnit(
        unit_id="summary-unit:power",
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
        composition_units=(*block.composition_units, summary_unit),
    )
    plan = replace(plan, blocks=(plan_block,))
    evidence[summary_support_id] = PublicationEvidence(
        evidence_id=summary_support_id,
        story_id=999,
        text="При восстановлении электричества напряжение держится около 154 В.",
        source_text="При восстановлении электричества напряжение держится около 154 В.",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=999,
        source_ref="telegram:source:1:item:999:rev:1:frag:999",
        source_id=1,
        source_item_id=999,
        source_role="community",
        observed_at=_NOW,
    )
    facts = {fact.fact_id: fact for fact in plan_block.required_facts}
    power_fact_ids = [
        fact.fact_id for fact in plan_block.required_facts if fact.subject_key == "electricity"
    ]
    power_unit_ids = [
        unit.unit_id
        for unit in plan_block.composition_units
        if set(unit.fact_ids).intersection(power_fact_ids)
    ]
    water_unit = next(
        unit
        for unit in plan_block.composition_units
        if not set(unit.fact_ids).intersection(power_fact_ids)
    )
    water_fact_ids = list(water_unit.fact_ids)
    source_item = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            "composition_unit_ids": [*power_unit_ids, summary_unit.unit_id],
                            "covered_fact_ids": power_fact_ids,
                            "headline": "",
                            "body": "Сводка по электроснабжению. При восстановлении электричества напряжение держится около 154 В.",
                            "claims": [
                                {"text": facts[fid].text, "covered_fact_ids": [fid]}
                                for fid in power_fact_ids
                            ]
                            + [
                                {
                                    "text": "При восстановлении электричества напряжение держится около 154 В.",
                                    "covered_fact_ids": [],
                                    "summary_unit_ids": [summary_unit.unit_id],
                                }
                            ],
                        },
                        {
                            "composition_unit_ids": [water_unit.unit_id],
                            "covered_fact_ids": water_fact_ids,
                            "headline": "",
                            "body": " ".join(facts[fid].text for fid in water_fact_ids),
                            "claims": [
                                {"text": facts[fid].text, "covered_fact_ids": [fid]}
                                for fid in water_fact_ids
                            ],
                        },
                    ],
                }
            ]
        },
        plan=plan,
    )
    observed: dict[str, bool] = {}

    class Provider:
        async def chat_completion(self, **kwargs):
            user_input = json.loads(kwargs["messages"][1]["content"])
            observed["targeted"] = user_input["blocks"][0]["items"][0]["targeted_for_recomposition"]
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "items": [],
                            "merges": [],
                            "recomposed_items": [
                                {
                                    "composition_unit_ids": [
                                        *power_unit_ids,
                                        summary_unit.unit_id,
                                    ],
                                    "covered_fact_ids": power_fact_ids,
                                    "headline": "",
                                    "body": "По сообщениям жителей, на АКЗ свет включали на 15 минут после 62 дней без электричества; на Крылова света нет больше 10 дней, а в центре — вторые сутки. При восстановлении электричества напряжение держится около 154 В.",
                                    "claims": [
                                        {
                                            "text": facts[fid].text,
                                            "covered_fact_ids": [fid],
                                        }
                                        for fid in power_fact_ids
                                    ]
                                    + [
                                        {
                                            "text": "При восстановлении электричества напряжение держится около 154 В.",
                                            "covered_fact_ids": [],
                                            "summary_unit_ids": [summary_unit.unit_id],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )

    result = asyncio.run(
        DigestEditor(provider=Provider()).polish_and_compress(
            source_item,
            plan=plan,
            evidence=evidence,
            target_item_ids=(source_item.blocks[0].items[0].item_id,),
            recompose_block_ids=(block.block_id,),
        )
    )

    assert observed.get("targeted") is True
    assert len(result.blocks[0].items) == 2
    assert set(result.blocks[0].items[0].covered_fact_ids) == set(power_fact_ids)
    assert set(result.blocks[0].items[1].covered_fact_ids) == set(water_fact_ids)
    assert summary_id in result.blocks[0].items[0].covered_story_ids


@pytest.mark.parametrize("synthesized", [False, True])
def test_editor_distinguishes_four_fragments_from_four_synthesized_items(synthesized: bool) -> None:
    import asyncio
    import json

    from src.publication.digest_editor import DigestEditor

    extra_reports = [("electricity", "РТС", "На РТС света нет неделю.")]
    if synthesized:
        extra_reports.extend(
            ("electricity", place, text)
            for place, text in (
                ("АКЗ", "На АКЗ электричество появилось в 10:35."),
                ("Крылова", "На Крылова свет включали вчера на час."),
                ("Центр", "В центре напряжение составляло 154 В."),
                ("Вроцлавская", "На Вроцлавской света нет с воскресенья."),
            )
        )
    _, evidence, _, plan = _fixture(*extra_reports)
    block = plan.blocks[0]
    power_fact_ids = {
        fact.fact_id for fact in block.required_facts if fact.subject_key == "electricity"
    }
    raw_items = []
    for unit in block.composition_units:
        fact_ids = [fact_id for fact_id in unit.fact_ids if fact_id in power_fact_ids]
        if not fact_ids:
            continue
        facts = [fact for fact in block.required_facts if fact.fact_id in fact_ids]
        raw_items.append(
            {
                "composition_unit_ids": [unit.unit_id],
                "covered_fact_ids": fact_ids,
                "headline": "",
                "body": " ".join(fact.text for fact in facts),
                "claims": [
                    {"text": fact.text, "covered_fact_ids": [fact.fact_id]} for fact in facts
                ],
            }
        )
    water_unit = next(
        unit
        for unit in block.composition_units
        if not set(unit.fact_ids).intersection(power_fact_ids)
    )
    water_facts = [fact for fact in block.required_facts if fact.fact_id in water_unit.fact_ids]
    raw_items.append(
        {
            "composition_unit_ids": [water_unit.unit_id],
            "covered_fact_ids": [fact.fact_id for fact in water_facts],
            "headline": "",
            "body": " ".join(fact.text for fact in water_facts),
            "claims": [
                {"text": fact.text, "covered_fact_ids": [fact.fact_id]} for fact in water_facts
            ],
        }
    )
    draft = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": raw_items}]}, plan=plan
    )

    class Provider:
        async def chat_completion(self, **kwargs):
            over_fragmented = [
                {**item, "body": "Пересобранный пункт. " + item["body"]} for item in raw_items[:-1]
            ]
            if synthesized:
                grouped = []
                for index in range(0, len(over_fragmented), 2):
                    pair = over_fragmented[index : index + 2]
                    grouped.append(
                        {
                            "composition_unit_ids": [
                                uid for item in pair for uid in item["composition_unit_ids"]
                            ],
                            "covered_fact_ids": [
                                fid for item in pair for fid in item["covered_fact_ids"]
                            ],
                            "headline": "",
                            "body": " ".join(item["body"] for item in pair),
                            "claims": [claim for item in pair for claim in item["claims"]],
                        }
                    )
                over_fragmented = grouped
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "items": [],
                            "merges": [],
                            "recomposed_items": over_fragmented,
                        }
                    ]
                }
            )

    edit = DigestEditor(provider=Provider()).polish_and_compress(
        draft,
        plan=plan,
        evidence=evidence,
        target_item_ids=tuple(
            item.item_id
            for item in draft.blocks[0].items
            if set(item.covered_fact_ids).intersection(power_fact_ids)
        ),
        recompose_block_ids=(block.block_id,),
    )
    result = asyncio.run(edit)
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    fragmented = any(
        warning.code == "FRAGMENTED_SERVICE_REPORTS"
        for warning in audit_digest_prose_quality(result, evidence).warnings
    )
    assert fragmented is (not synthesized)
    assert {fid for item in result.blocks[0].items for fid in item.covered_fact_ids} == {
        fact.fact_id for fact in block.required_facts
    }


@pytest.mark.parametrize("power_count", [3, 4])
def test_power_fragmentation_threshold_matches_three_item_editor_limit(power_count: int) -> None:
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    reports = () if power_count == 3 else (("electricity", "РТС", "На РТС света нет неделю."),)
    _, evidence, _, plan = _fixture(*reports)
    block = plan.blocks[0]
    power_fact_ids = {
        fact.fact_id for fact in block.required_facts if fact.subject_key == "electricity"
    }
    power_units = [
        unit for unit in block.composition_units if set(unit.fact_ids).intersection(power_fact_ids)
    ]
    raw_items = []
    for unit in power_units:
        fact_ids = [fact_id for fact_id in unit.fact_ids if fact_id in power_fact_ids]
        facts = [fact for fact in block.required_facts if fact.fact_id in fact_ids]
        raw_items.append(
            {
                "composition_unit_ids": [unit.unit_id],
                "covered_fact_ids": fact_ids,
                "headline": "",
                "body": " ".join(fact.text for fact in facts),
                "claims": [
                    {"text": fact.text, "covered_fact_ids": [fact.fact_id]} for fact in facts
                ],
            }
        )
    water_units = [
        unit
        for unit in block.composition_units
        if not set(unit.fact_ids).intersection(power_fact_ids)
    ]
    for unit in water_units:
        facts = [fact for fact in block.required_facts if fact.fact_id in unit.fact_ids]
        raw_items.append(
            {
                "composition_unit_ids": [unit.unit_id],
                "covered_fact_ids": [fact.fact_id for fact in facts],
                "headline": "",
                "body": " ".join(fact.text for fact in facts),
                "claims": [
                    {"text": fact.text, "covered_fact_ids": [fact.fact_id]} for fact in facts
                ],
            }
        )
    draft = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": raw_items}]}, plan=plan
    )
    audit = audit_digest_prose_quality(draft, evidence)
    fragment_warnings = [w for w in audit.warnings if w.code == "FRAGMENTED_SERVICE_REPORTS"]
    if power_count == 3:
        assert fragment_warnings == []
    else:
        assert len(fragment_warnings) == 4
        assert {warning.block_id for warning in fragment_warnings} == {block.block_id}
    assert audit.is_publishable


@pytest.mark.parametrize(
    "body",
    [
        "По их подсчётам, в одном из сообщений отсутствие электричества длилось 64 дня.",
        "Также сообщалось об отсутствии света в районе РТС.",
        "В Бердянске заполняют систему отопления, сообщается в городе.",
        "Сообщения об электроснабжении в АКЗ расходятся: одно описывает отсутствие света.",
        "Опубликовано объявление о маршрутах из Бердянска в Ростов.",
        "Другие сообщения об отключениях: на Кирова света нет 64 дня.",
    ],
)
def test_source_meta_narration_variants_trigger_editorial_repair(body: str) -> None:
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    _, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    fact_ids = [fact.fact_id for fact in block.required_facts]
    units = [unit for unit in block.composition_units if unit.fact_ids]
    fact_text = {fact.fact_id: fact.text for fact in block.required_facts}
    draft = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            "composition_unit_ids": [unit.unit_id for unit in units],
                            "covered_fact_ids": fact_ids,
                            "headline": "",
                            "body": body,
                            "claims": [
                                {"text": fact_text[fact_id], "covered_fact_ids": [fact_id]}
                                for fact_id in fact_ids
                            ],
                        }
                    ],
                }
            ]
        },
        plan=plan,
    )

    audit = audit_digest_prose_quality(draft, evidence)

    assert "SOURCE_META_NARRATION" in {warning.code for warning in audit.warnings}
    assert audit.is_publishable


def test_named_city_chat_reference_is_removed_without_inventing_a_poster() -> None:
    from src.publication.digest_narrative import _fix_chat_leaks

    repaired = _fix_chat_leaks(
        "В Бердянском чате сообщают, что в городе заполняют систему отопления."
    )
    assert repaired == "Сообщается, что в городе заполняют систему отопления."
    assert "чате" not in repaired.casefold()


@pytest.mark.parametrize("unsafe_second_edit", [False, True])
def test_bounded_repair_polishes_new_items_and_keeps_last_safe_checkpoint(unsafe_second_edit):
    import asyncio
    import json
    from types import SimpleNamespace

    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import build_digest_support_text_index
    from src.publication.digest_quality_diagnostics import (
        DigestQualityAudit,
        audit_digest_prose_quality,
    )
    from src.publication.generation import _repair_digest_candidate

    cards, evidence, presentation, plan = _fixture(
        ("electricity", "РТС", "На РТС света нет неделю.")
    )
    block = plan.blocks[0]
    unit_for_fact = {fid: u.unit_id for u in block.composition_units for fid in u.fact_ids}
    texts = {fact.fact_id: fact.text for fact in block.required_facts}
    power_ids = [fact.fact_id for fact in block.required_facts if fact.subject_key == "electricity"]

    def raw_item(fids, prefix=""):
        return {
            "composition_unit_ids": list(dict.fromkeys(unit_for_fact[fid] for fid in fids)),
            "covered_fact_ids": fids,
            "headline": "",
            "body": prefix + " ".join(texts[fid] for fid in fids),
            "claims": [],
        }

    initial = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": [raw_item([fid]) for fid in texts]}]},
        plan=plan,
    )
    source_meta_prefix = "В одном из сообщений говорится: "
    revised_body = "По сообщениям жителей, " + " ".join(texts[fid] for fid in power_ids[:2])
    observed_targets = []

    class Provider:
        calls = 0

        async def chat_completion(self, **kwargs):
            self.calls += 1
            request = json.loads(kwargs["messages"][1]["content"])
            observed_targets.append(request["target_item_ids"])
            assert {fact["fact_id"] for fact in request["required_recomposition_facts"]} == set(
                request["target_recomposition_fact_ids"]
            )
            assert all(fact["text"] for fact in request["required_recomposition_facts"])
            if self.calls == 1:
                items = [raw_item(power_ids[:2], source_meta_prefix), raw_item(power_ids[2:])]
                for item in items:
                    item.pop("claims")
                return json.dumps(
                    {
                        "blocks": [
                            {
                                "block_id": block.block_id,
                                "recomposed_items": items,
                            }
                        ]
                    }
                )
            assert self.calls == 2
            body = (
                revised_body.replace("15 минут", "99 минут") if unsafe_second_edit else revised_body
            )
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "items": [
                                {
                                    "item_id": request["target_item_ids"][0],
                                    "headline": "",
                                    "body": body,
                                }
                            ],
                        }
                    ]
                }
            )

    class Observer:
        finished = []

        async def attempt_started(self, kind, metadata):
            assert kind == "repair"
            return metadata["repair_call"]

        async def attempt_finished(self, attempt_id, status, **kwargs):
            self.finished.append((attempt_id, status))

    def assess(draft):
        validation = validate_digest_narrative(
            draft,
            plan,
            support_text_by_id=build_digest_support_text_index(evidence=evidence, cards=cards),
        )
        coverage = build_digest_coverage_trace(presentation, draft, plan)
        audit = DigestQualityAudit(
            checks=(), prose_audit=audit_digest_prose_quality(draft, evidence)
        )
        return validation, coverage, SimpleNamespace(), audit

    provider = Provider()
    observer = Observer()
    checkpoint, used, max_calls = asyncio.run(
        _repair_digest_candidate(
            checkpoint=(initial, *assess(initial)),
            plan=plan,
            evidence=evidence,
            editor=DigestEditor(provider=provider),
            observer=observer,
            evaluate_candidate=assess,
            model=None,
            timeout_seconds=10,
            implementation_versions={},
        )
    )
    final, validation, coverage, _, audit = checkpoint
    assert provider.calls == max_calls == 2
    assert used and validation.is_valid
    assert coverage.story_coverage == coverage.material_fact_coverage == 1.0
    assert len(observed_targets[0]) == 4
    assert len(observed_targets[1]) == 1
    assert observed_targets[1][0] == final.blocks[0].items[0].item_id
    if unsafe_second_edit:
        assert final.blocks[0].items[0].body.startswith(source_meta_prefix)
        assert observer.finished == [(1, "succeeded"), (2, "failed")]
        assert any(w.code == "SOURCE_META_NARRATION" for w in audit.prose_audit.warnings)
    else:
        assert final.blocks[0].items[0].body == revised_body
        assert observer.finished == [(1, "succeeded"), (2, "succeeded")]
        assert not any(w.code == "SOURCE_META_NARRATION" for w in audit.prose_audit.warnings)


def test_mixed_recomposition_returns_actionable_failure_without_partial_changes() -> None:
    import asyncio
    import json
    from dataclasses import asdict

    from src.publication.digest_editor import DigestEditor, DigestRecompositionError

    _, evidence, _, plan = _fixture()
    block = plan.blocks[0]
    raw_items = [
        {
            "composition_unit_ids": [unit.unit_id],
            "covered_fact_ids": list(unit.fact_ids),
            "headline": "",
            "body": " ".join(
                fact.text for fact in block.required_facts if fact.fact_id in unit.fact_ids
            ),
            "claims": [],
        }
        for unit in block.composition_units
    ]
    draft = _parse_composition_writer_output(
        {"blocks": [{"block_id": block.block_id, "items": raw_items}]}, plan=plan
    )
    before = asdict(draft)

    class Provider:
        async def chat_completion(self, **kwargs):
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": block.block_id,
                            "recomposed_items": raw_items,
                            "items": [
                                {
                                    "item_id": draft.blocks[0].items[0].item_id,
                                    "body": "This patch must never be applied.",
                                }
                            ],
                        }
                    ]
                }
            )

    with pytest.raises(DigestRecompositionError, match="mixed recomposition"):
        asyncio.run(
            DigestEditor(provider=Provider()).polish_and_compress(
                draft,
                plan=plan,
                evidence=evidence,
                target_item_ids=tuple(item.item_id for item in draft.blocks[0].items),
                recompose_block_ids=(block.block_id,),
            )
        )
    assert asdict(draft) == before
