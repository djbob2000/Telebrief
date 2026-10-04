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
    assert support["reply_parent_context_text"] == "Контекст «буквально так»"


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
    assert len(data["relations"]) == sum(len(b.composition_relations) for b in plan.blocks)


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
