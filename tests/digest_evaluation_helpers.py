"""Shared sealed inputs for digest assessment tests."""

from test_digest_synthesis import _NOW, _fixture

from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.publication.digest_narrative import (
    _parse_composition_writer_output,
    build_digest_support_text_index,
)
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.renderers import PublicationDigestRenderer


def assessment_inputs():
    cards, evidence, presentation, plan = _fixture()
    frozen = FrozenEditorialInput(
        EditorialAnalysis(cards=cards, evidence=evidence),
        PreparedBundle({}, "", 4, 4),
        edition_slug="berdyansk",
    )
    renderer = PublicationDigestRenderer(include_statistics=False)
    renderer.rubrics = [{"id": "infrastructure", "title": "Коммунальная обстановка"}]
    supports = build_digest_support_text_index(evidence=evidence, cards=cards, frozen_input=frozen)
    context = {
        "frozen": frozen,
        "plan": plan,
        "presentation_plan": presentation,
        "evidence": evidence,
        "support_text_by_id": supports,
        "allowed_context_terms": (),
        "snapshot_at": _NOW,
        "timezone_name": "UTC",
        "renderer": renderer,
    }
    raw = {
        "blocks": [
            {
                "block_id": b.block_id,
                "items": [
                    {
                        "composition_unit_ids": [u.unit_id],
                        "covered_fact_ids": list(u.fact_ids),
                        "headline": "",
                        "body": "По сообщению жителя, "
                        + " ".join(f.text for f in b.required_facts if f.fact_id in u.fact_ids),
                        "claims": [],
                    }
                    for u in b.composition_units
                ],
            }
            for b in plan.blocks
        ]
    }
    return context, _parse_composition_writer_output(raw, plan=plan)
