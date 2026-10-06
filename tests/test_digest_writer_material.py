"""Compact dossier changes representation, never evidence membership or source text."""

# ruff: noqa: S101
import json
from dataclasses import replace

import pytest
from test_digest_synthesis import _fixture


def test_compact_material_preserves_exact_membership_and_distinct_supports():
    from src.publication.digest_writer_material import build_compact_digest_material

    cards, evidence, _, plan = _fixture()
    key = next(iter(evidence))
    evidence[key] = replace(evidence[key], reply_parent_context_text="Контекст «буквально так»")
    data = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    assert {f["fact_id"] for f in data["facts"]} == {"fact:1", "fact:2", "fact:3", "fact:4"}
    assert len({s["support_id"] for s in data["supports"]}) == len(data["supports"])
    assert {u["composition_unit_id"] for u in data["units"]} == {
        u.unit_id for b in plan.blocks for u in b.composition_units
    }
    support = next(s for s in data["supports"] if s["support_id"] == key)
    assert evidence[key].text in support["texts"]
    assert "reply_parent_context_text" not in support


def test_unknown_household_location_and_single_source_are_preserved():
    from src.publication.digest_writer_material import build_compact_digest_material

    cards, evidence, _, plan = _fixture(
        ("power", "", "Жительница сообщает: у сына неделями нет света, место не указано.")
    )
    data = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    fact = next(f for f in data["facts"] if f["fact_id"] == "fact:5")
    assert fact["original_location"] == ""
    assert "место не указано" in fact["text"]
    assert "story:5:evidence:0:frag:5" in fact["support_ids"]
    assert (
        next(s for s in data["supports"] if s["support_id"] == "story:5:evidence:0:frag:5")[
            "source_role"
        ]
        == "community"
    )


def test_street_aliases_do_not_create_new_geographic_relations():
    from src.publication.digest_writer_material import build_compact_digest_material

    cards, evidence, _, plan = _fixture(
        ("water", "Центральная", "На Центральной вода есть."),
        ("water", "Карла Маркса", "На Карла Маркса воды нет."),
    )
    data = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    assert (
        next(f for f in data["facts"] if f["fact_id"] == "fact:5")["original_location"]
        == "Центральная"
    )
    assert (
        next(f for f in data["facts"] if f["fact_id"] == "fact:6")["original_location"]
        == "Карла Маркса"
    )
    assert any(
        {relation.left_fact_id, relation.right_fact_id} == {"fact:5", "fact:6"}
        and relation.kind.value == "RELATED_ONLY"
        for block in plan.blocks
        for relation in block.composition_relations
    )
    assert all(
        relation["kind"] in {"SAME_FACT", "SAME_SITUATION"} for relation in data["relations"]
    )
    assert not any(
        {relation["left_fact_id"], relation["right_fact_id"]} == {"fact:5", "fact:6"}
        for relation in data["relations"]
    )


def test_quotes_reply_context_and_precise_tariff_commands_survive():
    from src.publication.digest_writer_material import build_compact_digest_material

    text = "Житель пишет: «Телефон заряжаю». 30 ГБ за 300 рублей: *506*30#."
    cards, evidence, _, plan = _fixture(("communications", "", text))
    data = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    assert next(f for f in data["facts"] if f["fact_id"] == "fact:5")["text"] == text
    assert "Телефон заряжаю" in data["quote_allowlist"]


def test_compact_material_budget_never_truncates_facts():
    from src.publication.digest_writer_material import encode_digest_material

    data = {"facts": [{"text": "важная деталь" * 100}]}
    with pytest.raises(ValueError, match="DIGEST_MATERIAL_CONTEXT_BUDGET"):
        encode_digest_material(data, max_chars=10)
    assert json.loads(encode_digest_material(data)) == data


def test_material_format_default_and_unknown_value():
    from src.config.parsers.publication import _parse_publication_editorial_config
    from src.config.schemas.publication import PublicationEditorialConfig

    assert PublicationEditorialConfig().digest_writer_material_format == "legacy"
    with pytest.raises(ValueError, match="digest_writer_material_format"):
        PublicationEditorialConfig(digest_writer_material_format="typo")
    with pytest.raises(ValueError, match="digest_writer_material_format"):
        _parse_publication_editorial_config(
            {"publication_editorial": {"digest_writer_material_format": "typo"}}
        )


def test_observation_timestamp_is_not_exposed_as_a_citable_event_date():
    import datetime as dt

    from src.publication.digest_narrative import _composition_writer_payload
    from src.publication.digest_writer_material import build_compact_digest_material

    source = "По сообщению жителя, 29 сентября свет включили в 17:20."
    cards, evidence, _, plan = _fixture(("power", "АЗМОЛ", source))
    observed = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)
    plan = replace(
        plan,
        blocks=tuple(
            replace(
                block,
                composition_fact_records=tuple(
                    replace(record, observed_time=observed)
                    for record in block.composition_fact_records
                ),
            )
            for block in plan.blocks
        ),
    )
    legacy = _composition_writer_payload(plan=plan, evidence=evidence, cards=cards)
    legacy_facts = [f for b in legacy for u in b["composition_units"] for f in u["facts"]]
    assert all("observed_time" not in fact for fact in legacy_facts)
    compact = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    assert all("observed_time" not in fact for fact in compact["facts"])
    assert next(f for f in compact["facts"] if f["fact_id"] == "fact:5")["text"] == source
    assert all(
        record.observed_time == observed
        for block in plan.blocks
        for record in block.composition_fact_records
    )


def test_writer_receives_clock_observation_on_its_own_fact_not_other_service():
    from src.publication.digest_writer_material import build_compact_digest_material

    cards, evidence, _, plan = _fixture(
        ("power", "АКЗ", "На АКЗ свет появился в 10:35."),
        ("water", "АКЗ", "На АКЗ воды нет."),
    )
    material = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    power = next(f for f in material["facts"] if f["fact_id"] == "fact:5")
    water = next(f for f in material["facts"] if f["fact_id"] == "fact:6")
    assert power["protected_details"]["clock_observations"] == ["10:35"]
    assert water["protected_details"]["clock_observations"] == []
    assert power["protected_details"]["scope"]["original_location"] == "АКЗ"


def test_reply_parent_text_is_not_citable_writer_material():
    from src.publication.digest_narrative import (
        _composition_writer_payload,
        build_digest_support_text_index,
    )
    from src.publication.digest_writer_material import build_compact_digest_material

    parent = "Димитрова в ожидании чуда. Идут четвертые сутки без света"
    current = f'Нам дали свет на сутки, вчера отключили. (in_reply_to: "{parent}")'
    cards, evidence, _, plan = _fixture(("power", "Димитрова", current))
    eid = "story:5:evidence:0:frag:5"
    evidence[eid] = replace(evidence[eid], reply_parent_context_text=parent)

    legacy = _composition_writer_payload(plan=plan, evidence=evidence, cards=cards)
    legacy_text = json.dumps(legacy, ensure_ascii=False)
    compact = build_compact_digest_material(plan=plan, evidence=evidence, cards=cards)
    compact_text = json.dumps(compact, ensure_ascii=False)
    validation_index = build_digest_support_text_index(evidence=evidence, cards=cards)

    legacy_supports = [
        support
        for block in legacy
        for unit in block["composition_units"]
        for support in unit["supports"]
    ]
    compact_supports = compact["supports"]
    assert all(
        parent not in text
        for support in (*legacy_supports, *compact_supports)
        for text in support["texts"]
    )
    assert all(parent not in fact["text"] for fact in compact["facts"])
    assert compact["quote_allowlist"] == []
    assert any(
        support.get("reply_parent_context")
        == {"kind": "background_only_not_citable", "texts": [parent]}
        for support in (*legacy_supports, *compact_supports)
    )
    assert parent in legacy_text and parent in compact_text
    assert all(parent not in text for text in validation_index.values())
    assert "(in_reply_to:" not in legacy_text + compact_text
    assert evidence[eid].source_text.endswith(f'(in_reply_to: "{parent}")')


def test_digest_contract_allows_unique_reply_parent_to_resolve_location_only():
    from src.publication.narrative_contract import DIGEST_REPLY_CONTEXT_GUIDE

    guide = DIGEST_REPLY_CONTEXT_GUIDE.casefold()

    assert "may clarify the reply's referent or location only when" in guide
    assert "unique direct answer to its parent question" in guide
    assert "parent must never support or add the reply's status" in guide
    assert "source role, or any other event detail" in guide
