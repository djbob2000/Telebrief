"""Collector contract and transactional ingestion service behaviour.

Task 2 pins the provider-neutral observation DTOs and enums here; Task 3
adds IngestionService flows on top of the same fixtures used by
tests/ingestion/test_repository.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ingestion.models import (
    CollectionBatch,
    CollectionOutcome,
    CollectionTrigger,
    ObservedItem,
)


def test_observed_item_requires_stable_external_id():
    with pytest.raises(ValueError):
        ObservedItem(
            kind="telegram_message",
            external_id="",
            text="x",
            author_name="Resident",
            published_at=datetime.now(timezone.utc),
            canonical_url=None,
            metadata={},
            observed_at=datetime.now(timezone.utc),
        )


def test_collection_batch_preserves_partial_success():
    item = ObservedItem(
        kind="telegram_message",
        external_id="42",
        text="water restored",
        author_name="Resident",
        published_at=datetime.now(timezone.utc),
        canonical_url="https://t.me/example/42",
        metadata={},
        observed_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    batch = CollectionBatch(
        outcome=CollectionOutcome.SUCCESS,
        items=(item,),
        assets=(),
        state_events=(),
        adapter_state={"cursor": 12},
        started_at=now,
        completed_at=now,
    )
    assert batch.items == (item,)


def test_collection_outcome_values_match_run_status_contract():
    """CollectionOutcome maps onto collection_runs.status CHECK values."""
    assert {outcome.value for outcome in CollectionOutcome} == {
        "success",
        "transient",
        "rate_limited",
        "auth_required",
        "account_action_required",
        "access_denied",
        "source_not_found",
        "layout_changed",
        "permanent",
    }


def test_collection_trigger_values_match_run_trigger_contract():
    """CollectionTrigger maps onto collection_runs.trigger CHECK values."""
    assert {trigger.value for trigger in CollectionTrigger} == {
        "scheduled",
        "pre_publish",
        "manual",
        "backfill",
    }
