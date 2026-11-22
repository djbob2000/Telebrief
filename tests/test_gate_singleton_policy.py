# ruff: noqa: S101
"""Single-story re-send policy for invalid Gate stories."""

from __future__ import annotations

from src.processing.event_authority import singleton_recoverable_story_ids
from src.processing.event_triage import StoryGateBatchResult


def _batch_result() -> StoryGateBatchResult:
    return StoryGateBatchResult(
        results=(),
        deferred_story_ids=(1, 2, 3),
        missing_story_ids=(1,),
        invalid_story_ids=(2, 3),
        invalid_reasons={2: "drop_without_valid_brief", 3: "unsupported_publish_extraction"},
    )


def test_singleton_recovery_on_re_sends_missing_and_recoverable_invalid_stories():
    # Story 3 stays out: its check is deterministic and never recovers alone.
    assert singleton_recoverable_story_ids(_batch_result()) == {1, 2}


def test_singleton_recovery_off_re_sends_only_missing_stories():
    assert singleton_recoverable_story_ids(_batch_result(), invalid_recovery_enabled=False) == {1}
