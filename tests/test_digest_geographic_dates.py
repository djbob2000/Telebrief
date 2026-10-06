# ruff: noqa: S101
from __future__ import annotations

from src.publication import digest_reporting_context


def find_unsupported_digest_claims(*args, **kwargs):
    check = getattr(digest_reporting_context, "find_unsupported_digest_claims", None)
    assert callable(check), "Digest needs source-bound geographic date disambiguation"
    return check(*args, **kwargs)


def test_colloquial_area_alias_supports_named_area_without_becoming_date():
    failures = find_unsupported_digest_claims(
        "В районе 8 Марта сейчас нет света.",
        ["На 8хе сейчас света нет совсем."],
        edition_slug="berdyansk",
    )
    assert not any(item.kind == "date" for item in failures)


def test_colloquial_alias_does_not_support_calendar_date():
    failures = find_unsupported_digest_claims(
        "8 марта свет появится.",
        ["На 8хе сейчас света нет совсем."],
        edition_slug="berdyansk",
    )
    assert any(item.kind == "date" for item in failures)


def test_another_area_does_not_support_named_area_date_exception():
    failures = find_unsupported_digest_claims(
        "В районе 8 Марта нет света.",
        ["На АКЗ нет света."],
        edition_slug="berdyansk",
    )
    assert any(item.kind == "date" for item in failures)


def test_geographic_name_does_not_waive_same_calendar_date_elsewhere():
    failures = find_unsupported_digest_claims(
        "В районе 8 Марта свет появится 8 марта.",
        ["На 8хе сейчас света нет совсем."],
        edition_slug="berdyansk",
    )
    assert any(item.kind == "date" for item in failures)


def test_alias_for_area_does_not_establish_same_named_street():
    failures = find_unsupported_digest_claims(
        "На улице 8 Марта нет света.",
        ["На 8хе сейчас света нет совсем."],
        edition_slug="berdyansk",
    )
    assert any(item.kind == "date" for item in failures)


def test_composition_claim_uses_source_bound_area_disambiguation():
    from test_digest_evidence_bindings import binding_inputs

    from src.publication.digest_narrative import (
        _parse_composition_writer_output,
        validate_digest_narrative,
    )

    plan, supports = binding_inputs(["На 8хе сейчас света нет совсем."], locations=["8ха"])
    block = plan.blocks[0]
    body = "По сообщению жителя, в районе 8 Марта сейчас нет света."
    draft = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            "composition_unit_ids": [u.unit_id for u in block.composition_units],
                            "covered_fact_ids": [f.fact_id for f in block.required_facts],
                            "headline": "",
                            "body": body,
                            "claims": [],
                        }
                    ],
                }
            ]
        },
        plan=plan,
    )
    from dataclasses import replace

    item = draft.blocks[0].items[0]
    item = replace(item, claims=(replace(item.claims[0], text=body),))
    draft = replace(draft, blocks=(replace(draft.blocks[0], items=(item,)),))
    result = validate_digest_narrative(draft, plan, support_text_by_id=supports)
    assert not any("[date]" in finding for finding in result.violations), result.violations
