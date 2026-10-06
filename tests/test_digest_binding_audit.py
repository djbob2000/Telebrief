"""A binding model's IDs and spans must be checked against the frozen plan."""

# ruff: noqa: S101

from scripts.digest_evaluation.binding_audit import audit_digest_binding_integrity
from src.publication.digest_presentation import RequiredDigestFact


def fact(fact_id: str, *support_ids: str) -> RequiredDigestFact:
    return RequiredDigestFact(
        fact_id=fact_id,
        rubric_id="infrastructure",
        subject_key="service",
        subject_label="Служба",
        story_ids=(f"story:{fact_id}",),
        support_ids=tuple(support_ids),
        text=f"required fact {fact_id}",
    )


def test_valid_per_fact_support_edges_account_for_all_facts() -> None:
    facts = (fact("power", "support:power"), fact("water", "support:water"))
    candidate = "Power is unavailable. Water is intermittent."
    result = {
        "paragraphs": [
            {
                "exact_text": "Power is unavailable.",
                "fact_bindings": [
                    {
                        "fact_id": "power",
                        "support_ids": ["support:power"],
                        "status": "fully_supported",
                    }
                ],
            },
            {
                "exact_text": "Water is intermittent.",
                "fact_bindings": [
                    {
                        "fact_id": "water",
                        "support_ids": ["support:water"],
                        "status": "fully_supported",
                    }
                ],
            },
        ],
        "uncovered_fact_ids": [],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity(candidate, result, facts)

    assert audit.valid
    assert audit.covered_fact_ids == ("power", "water")
    assert audit.unaccounted_fact_ids == ()


def test_support_allowed_for_another_fact_cannot_prove_this_fact() -> None:
    facts = (fact("power", "support:power"), fact("water", "support:water"))
    result = {
        "paragraphs": [
            {
                "exact_text": "Water is intermittent.",
                "fact_bindings": [
                    {
                        "fact_id": "water",
                        "support_ids": ["support:power"],
                        "status": "fully_supported",
                    }
                ],
            }
        ],
        "uncovered_fact_ids": [],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity("Water is intermittent.", result, facts)

    assert not audit.valid
    assert audit.invalid_support_bindings == (("water", "support:power"),)
    assert audit.unaccounted_fact_ids == ("power", "water")


def test_unknown_uncovered_id_is_rejected_and_does_not_hide_missing_facts() -> None:
    result = {
        "paragraphs": [],
        "uncovered_fact_ids": ["fact:invented"],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity("A digest.", result, (fact("power", "s1"),))

    assert not audit.valid
    assert audit.unknown_uncovered_fact_ids == ("fact:invented",)
    assert audit.unaccounted_fact_ids == ("power",)


def test_nonexact_text_spans_and_unsupported_fragments_are_rejected() -> None:
    result = {
        "paragraphs": [
            {
                "exact_text": "Power outage.",
                "fact_bindings": [
                    {
                        "fact_id": "power",
                        "support_ids": ["s1"],
                        "status": "fully_supported",
                    }
                ],
            }
        ],
        "uncovered_fact_ids": [],
        "unsupported_spans": ["invented detail"],
    }

    audit = audit_digest_binding_integrity(
        "A power outage. No extra claim.", result, (fact("power", "s1"),)
    )

    assert not audit.valid
    assert audit.nonexact_paragraph_spans == ("Power outage.",)
    assert audit.nonexact_unsupported_spans == ("invented detail",)


def test_partial_binding_must_remain_explicitly_uncovered() -> None:
    result = {
        "paragraphs": [
            {
                "exact_text": "Water returns only in the evening.",
                "fact_bindings": [
                    {
                        "fact_id": "water",
                        "support_ids": ["s1"],
                        "status": "partially_supported",
                    }
                ],
            }
        ],
        "uncovered_fact_ids": ["water"],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity(
        "Water returns only in the evening.", result, (fact("water", "s1"),)
    )

    assert audit.valid
    assert audit.covered_fact_ids == ()
    assert audit.uncovered_fact_ids == ("water",)


def test_story_without_required_facts_is_covered_by_its_own_support() -> None:
    stories = {"story:power": ("s1",), "story:water-context": ("s2",)}
    result = {
        "paragraphs": [
            {
                "exact_text": "Power is unavailable.",
                "fact_bindings": [
                    {
                        "fact_id": "power",
                        "support_ids": ["s1"],
                        "status": "fully_supported",
                    }
                ],
                "story_bindings": [
                    {"story_id": "story:power", "support_ids": ["s1"], "status": "fully_supported"}
                ],
            },
            {
                "exact_text": "The local library will open late.",
                "fact_bindings": [],
                "story_bindings": [
                    {
                        "story_id": "story:water-context",
                        "support_ids": ["s2"],
                        "status": "fully_supported",
                    }
                ],
            },
        ],
        "uncovered_fact_ids": [],
        "uncovered_story_ids": [],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity(
        "Power is unavailable.\nThe local library will open late.",
        result,
        (fact("power", "s1"),),
        required_story_supports=stories,
    )

    assert audit.valid
    assert audit.has_full_coverage
    assert audit.covered_story_ids == ("story:power", "story:water-context")


def test_story_cannot_be_covered_by_another_storys_support() -> None:
    result = {
        "paragraphs": [
            {
                "exact_text": "The local library will open late.",
                "fact_bindings": [],
                "story_bindings": [
                    {
                        "story_id": "story:water-context",
                        "support_ids": ["s1"],
                        "status": "fully_supported",
                    }
                ],
            }
        ],
        "uncovered_fact_ids": ["power"],
        "uncovered_story_ids": [],
        "unsupported_spans": [],
    }

    audit = audit_digest_binding_integrity(
        "The local library will open late.",
        result,
        (fact("power", "s1"),),
        required_story_supports={"story:water-context": ("s2",)},
    )

    assert not audit.valid
    assert audit.invalid_story_support_bindings == (("story:water-context", "s1"),)
