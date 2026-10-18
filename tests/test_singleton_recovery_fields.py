# ruff: noqa: S101
"""Diagnostics for a story re-sent alone after a failed batch carry the original reason."""

from __future__ import annotations

from src.processing.event_authority import singleton_recovery_fields
from src.processing.event_triage import StoryGateBatchResult


def _batch(deferred_story_ids: tuple[int, ...] = (), **kwargs) -> StoryGateBatchResult:
    return StoryGateBatchResult(results=(), deferred_story_ids=deferred_story_ids, **kwargs)


def test_missing_story_is_labelled_missing():
    fields = singleton_recovery_fields(
        story_id=7,
        batch_result=_batch(missing_story_ids=(7,), batch_error_kind="partial_response"),
        singleton_result=_batch(deferred_story_ids=(7,)),
    )
    assert fields["original_reason"] == "missing"
    assert fields["singleton_recovered"] is False


def test_invalid_story_keeps_its_validation_reason():
    fields = singleton_recovery_fields(
        story_id=8,
        batch_result=_batch(
            invalid_story_ids=(8,),
            batch_error_kind="invalid_response",
            invalid_reasons={8: "unsupported_publish_extraction"},
        ),
        singleton_result=_batch(deferred_story_ids=()),
    )
    assert fields["original_reason"] == "unsupported_publish_extraction"
    assert fields["batch_error_kind"] == "invalid_response"


def test_unknown_reason_is_not_guessed():
    fields = singleton_recovery_fields(
        story_id=9,
        batch_result=_batch(invalid_story_ids=(9,)),
        singleton_result=_batch(deferred_story_ids=()),
    )
    assert fields["original_reason"] == "unknown"


def test_recovered_singleton_is_reported():
    gate = object()
    singleton = StoryGateBatchResult(results=(gate,), deferred_story_ids=())  # type: ignore[arg-type]
    fields = singleton_recovery_fields(
        story_id=10,
        batch_result=_batch(
            invalid_story_ids=(10,), invalid_reasons={10: "missing_scope_basis_fragments"}
        ),
        singleton_result=singleton,
    )
    assert fields["singleton_recovered"] is True
    assert fields["singleton_error_kind"] is None
