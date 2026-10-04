"""Keep a supplied fare comparison attached to its paid leg."""

# ruff: noqa: S101
from src.publication.digest_reporting_context import fare_comparison_context


def test_passing_bus_comparison_preserves_leg_prices_and_destination_role():
    source = (
        "По информации автовокзала, билет на автобус Бердянск-Мелитополь стоит 800 рублей, "
        "а на проходящих автобусах (Ростов, Москва) — 1000 рублей."
    )
    assert fare_comparison_context([source]) == [
        {
            "source_text": source,
            "paid_leg": "Бердянск-Мелитополь",
            "ordinary_fare": "800 рублей",
            "passing_bus_fare_for_same_leg": "1000 рублей",
            "passing_bus_final_destinations": "Ростов, Москва",
        }
    ]


def test_missing_paid_leg_is_not_inferred_from_passing_destinations():
    assert (
        fare_comparison_context(["На проходящих автобусах (Ростов, Москва) — 1000 рублей."]) == []
    )


def test_distinct_explicit_leg_is_not_treated_as_same_leg_comparison():
    assert (
        fare_comparison_context(
            [
                "Билет на автобус Бердянск-Мелитополь стоит 800 рублей, "
                "а билет в Ростов на проходящем автобусе — 1000 рублей."
            ]
        )
        == []
    )


def test_evidence_aliases_share_the_same_source_metadata():
    from src.publication.digest_reporting_context import publish_support_metadata
    from src.publication.evidence import PublicationEvidence

    evidence = PublicationEvidence(
        evidence_id="evidence:1",
        story_id=1,
        text="На Лисках дали свет.",
        source_text="На Лисках дали свет.",
        kind="service_access",
        publication_use="PUBLISH",
        source_ref="telegram:source:1:item:2:rev:3:frag:4",
        fragment_id=4,
        source_id=1,
        source_item_id=2,
        source_item_revision_id=3,
        source_role="community",
        observed_at=None,
    )
    metadata = publish_support_metadata({evidence.evidence_id: evidence})
    for alias in [evidence.evidence_id, evidence.source_ref, "fragment:4"]:
        assert metadata[alias] == {
            "canonical_evidence_ids": ["evidence:1"],
            "evidence_kind": "service_access",
            "source_role": "community",
            "source_item_id": 2,
            "source_id": 1,
            "source_item_revision_id": 3,
        }


def test_ambiguous_fragment_alias_does_not_invent_one_source_identity():
    from dataclasses import replace

    from src.publication.digest_reporting_context import publish_support_metadata
    from src.publication.evidence import PublicationEvidence

    first = PublicationEvidence(
        evidence_id="evidence:1",
        story_id=1,
        text="Нет света.",
        source_text="Нет света.",
        kind="community_report",
        publication_use="PUBLISH",
        source_ref="source:first",
        fragment_id=4,
        source_id=1,
        source_item_id=2,
        source_role="community",
        observed_at=None,
    )
    second = replace(first, evidence_id="evidence:2", source_ref="source:second", source_item_id=3)
    metadata = publish_support_metadata({first.evidence_id: first, second.evidence_id: second})
    assert metadata["fragment:4"]["canonical_evidence_ids"] == ["evidence:1", "evidence:2"]
    assert metadata["fragment:4"]["source_item_id"] is None
    assert metadata["source:first"]["source_item_id"] == 2
    assert metadata["source:second"]["source_item_id"] == 3


def test_ambiguous_passing_fare_requests_repair_without_publication_veto():
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
    )
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality
    from src.publication.evidence import PublicationEvidence

    source = (
        "Билет на автобус Бердянск-Мелитополь стоит 800 рублей, "
        "а на проходящих автобусах (Ростов, Москва) — 1000 рублей."
    )
    evidence = PublicationEvidence(
        evidence_id="source",
        story_id=1,
        text=source,
        source_text=source,
        fragment_id=1,
        source_ref="source",
        source_id=1,
        source_item_id=1,
        kind="community_report",
        publication_use="PUBLISH",
        source_role="community",
        observed_at=None,
    )

    def warnings(body):
        draft = DigestNarrativeDraft(
            blocks=(
                DigestNarrativeBlockDraft(
                    block_id="transport",
                    items=(
                        DigestEditorialItemDraft(
                            body=body,
                            cited_support_ids=("source",),
                        ),
                    ),
                ),
            )
        )
        return audit_digest_prose_quality(draft, {"source": evidence}).warnings

    ambiguous = warnings(
        "Билет Бердянск—Мелитополь стоит 800 рублей. "
        "На проходящих автобусах в Ростов и Москву билет стоит 1000 рублей."
    )
    assert any(w.code == "AMBIGUOUS_PASSING_BUS_FARE" for w in ambiguous)
    explicit = warnings(
        "Билет Бердянск—Мелитополь стоит 800 рублей. "
        "На проходящих автобусах в Ростов и Москву этот же участок стоит 1000 рублей."
    )
    assert not any(w.code == "AMBIGUOUS_PASSING_BUS_FARE" for w in explicit)
