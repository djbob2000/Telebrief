# ruff: noqa: S101
"""A rolled-back structural batch records why, with codes and unit IDs only."""

from __future__ import annotations

from src.publication.article_editor import rollback_cause_fields


def test_new_blocker_rollback_keeps_codes_and_units():
    fields = rollback_cause_fields(
        "new_blocker_atomic_rollback",
        ["UNSUPPORTED_CRITICAL_TERM:P015", "THEME_MISMATCHED_SECTION:P016"],
    )
    assert fields == {
        "rollback_cause": "new_blocker_atomic_rollback",
        "rollback_blockers": ["UNSUPPORTED_CRITICAL_TERM:P015", "THEME_MISMATCHED_SECTION:P016"],
    }


def test_prose_in_blocker_keys_never_reaches_metadata():
    fields = rollback_cause_fields(
        "new_blocker_atomic_rollback",
        ["UNSUPPORTED_CRITICAL_TERM:P015 'Связь оставалась'", "x y z"],
    )
    assert fields["rollback_blockers"] == []


def test_unknown_cause_is_replaced_by_the_generic_code():
    fields = rollback_cause_fields("some free text from the model", [])
    assert fields["rollback_cause"] == "assessment_error_atomic_rollback"


def test_no_rollback_adds_no_fields():
    assert rollback_cause_fields(None, ["UNSUPPORTED_CRITICAL_TERM:P015"]) == {}
