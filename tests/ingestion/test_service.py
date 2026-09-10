"""Collector contract and transactional ingestion service behaviour.

Task 2 pins the provider-neutral observation DTOs and enums here; Task 3
adds IngestionService flows on top of the same fixtures used by
tests/ingestion/test_repository.py. Plan 3 Task 2 extends ingestion with the
atomic relevance wiring inside ingest_batch_in_transaction: every new
revision fans out one exact-policy evaluate_relevance deferral per bound
edition on the caller's connection, so revisions and their jobs commit (or
roll back) together.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.ingestion.models import (
    CollectionBatch,
    CollectionOutcome,
    CollectionTrigger,
    ObservedAsset,
    ObservedItem,
    ObservedStateEvent,
)
from src.ingestion.repository import IngestionRepository
from src.ingestion.service import IngestionService
from src.repositories.event_revision_processing import EventRevisionProcessingRepository

STARTED_AT = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
COMPLETED_AT = datetime(2026, 8, 22, 10, 0, 5, tzinfo=timezone.utc)


def _observation(
    *,
    external_id: str = "42",
    text: str = "hello",
    parent_external_id: str | None = None,
    root_external_id: str | None = None,
) -> ObservedItem:
    return ObservedItem(
        kind="telegram_message",
        external_id=external_id,
        text=text,
        author_name="Resident",
        published_at=STARTED_AT,
        canonical_url=f"https://t.me/example/{external_id}",
        metadata={"topic": 7},
        observed_at=STARTED_AT,
        parent_external_id=parent_external_id,
        root_external_id=root_external_id,
    )


def _batch(
    *,
    text: str = "hello",
    outcome: CollectionOutcome = CollectionOutcome.SUCCESS,
    items: tuple[ObservedItem, ...] | None = None,
    assets: tuple[ObservedAsset, ...] = (),
    state_events: tuple[ObservedStateEvent, ...] = (),
    adapter_state: dict | None = None,
    error_kind: str | None = None,
) -> CollectionBatch:
    return CollectionBatch(
        outcome=outcome,
        items=items if items is not None else (_observation(text=text),),
        assets=assets,
        state_events=state_events,
        adapter_state={"cursor": 12} if adapter_state is None else adapter_state,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
        error_kind=error_kind,
    )


@pytest.fixture
def service(uow) -> IngestionService:
    return IngestionService(uow, IngestionRepository())


async def _scalar(uow, sql: str, params: tuple = ()) -> object:
    async with uow.pool.connection() as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


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
        "enrichment",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ingest_batch_reports_new_edit_unchanged_counts(service, source, uow):
    """First observation is new, re-observation unchanged, edit adds revision 2."""
    result1 = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch())
    assert result1.new_items == 1
    assert result1.new_revisions == 1
    assert result1.new_revision_ids == (result1.new_revision_ids[0],)
    assert result1.collection_run_id > 0

    result2 = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch())
    assert result2.new_items == 0
    assert result2.new_revisions == 0

    observed = await _scalar(
        uow,
        """
        SELECT source_item_revision_id
        FROM collection_run_revision_observations
        WHERE collection_run_id = %s
        """,
        (result2.collection_run_id,),
    )
    latest = await _scalar(
        uow,
        """
        SELECT sir.id
        FROM source_item_revisions sir
        JOIN source_items si ON si.id = sir.source_item_id
        WHERE si.source_id = %s AND si.external_id = '42'
        ORDER BY sir.revision_no DESC
        LIMIT 1
        """,
        (source.id,),
    )
    assert observed == latest

    result3 = await service.ingest_batch(
        source.id, CollectionTrigger.SCHEDULED, _batch(text="hello edited")
    )
    assert result3.new_revisions == 1
    assert len(result3.new_revision_ids) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unchanged_revision_without_processing_state_is_requeued(
    service, source, edition, conn, production_jobs_app
):
    """A scan must repair an old revision missing Event-First processing state."""
    await _bind(conn, source.id, edition.id)
    first = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch())
    revision_id = first.new_revision_ids[0]

    await conn.execute(
        "DELETE FROM event_revision_processing_state WHERE source_item_revision_id = %s",
        (revision_id,),
    )
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")

    second = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch())

    assert second.new_revisions == 0
    assert await _deferred_event_jobs(conn) == [[revision_id]]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_semantic_noop_revision_reuses_succeeded_predecessor(service, source, uow):
    first = await service.ingest_batch(
        source.id, CollectionTrigger.SCHEDULED, _batch(text="Water outage on Street A")
    )
    assert first.full_processing_revision_ids == first.new_revision_ids

    async with uow.transaction() as conn:
        processing_repo = EventRevisionProcessingRepository()
        await processing_repo.mark_succeeded(conn, first.new_revision_ids)

    original = _observation(text="Water outage on Street A")
    metadata_edit = replace(original, metadata={"topic": 8})
    second = await service.ingest_batch(
        source.id,
        CollectionTrigger.SCHEDULED,
        _batch(items=(metadata_edit,)),
    )

    assert second.new_revisions == 1
    assert second.full_processing_revision_ids == ()
    assert second.reused_revision_ids == second.new_revision_ids
    async with uow.pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT status, processing_mode, reused_from_revision_id
            FROM event_revision_processing_state
            WHERE source_item_revision_id = %s
            """,
            (second.new_revision_ids[0],),
        )
        assert await cursor.fetchone() == (
            "succeeded",
            "reused",
            first.new_revision_ids[0],
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ingest_batch_commits_everything_together(service, source, uow):
    """Items, revisions, assets, events, checkpoint and run commit as one."""
    asset = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url="https://cdn.example/1.jpg",
        mime_type="image/jpeg",
        content_hash="hash-photo",
        metadata={"width": 800},
    )
    event = ObservedStateEvent(
        item_external_id="42",
        type="restored",
        observed_at=COMPLETED_AT,
        reason="back in scan",
        evidence={"message_id": 42},
    )
    batch = _batch(assets=(asset,), state_events=(event,), adapter_state={"cursor": 99})

    result = await service.ingest_batch(source.id, CollectionTrigger.MANUAL, batch)

    async with uow.pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT r.revision_no, a.kind FROM source_item_revisions r
            JOIN source_assets a ON a.source_item_revision_id = r.id
            """
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "photo"

        cursor = await conn.execute("SELECT count(*) FROM source_item_state_events")
        assert (await cursor.fetchone())[0] == 1

        checkpoint = await conn.execute(
            "SELECT adapter_state, last_success_at FROM collection_checkpoints WHERE source_id = %s",
            (source.id,),
        )
        cp_row = await checkpoint.fetchone()
        assert cp_row is not None
        assert cp_row[0] == {"cursor": 99}
        assert cp_row[1] == COMPLETED_AT

        run = await conn.execute(
            "SELECT status, completed_at FROM collection_runs WHERE id = %s",
            (result.collection_run_id,),
        )
        run_row = await run.fetchone()
        assert run_row is not None
        assert run_row[0] == "success"
        assert run_row[1] == COMPLETED_AT


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ingest_batch_rolls_back_everything_on_failure(service, source, uow, monkeypatch):
    """A failure after revision insertion persists nothing at all."""

    async def boom(conn, revision_id, asset):
        raise RuntimeError("asset persistence exploded")

    monkeypatch.setattr(service.repo, "upsert_asset_for_revision", boom)

    asset = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url="https://cdn.example/1.jpg",
        mime_type="image/jpeg",
        content_hash="hash-photo",
        metadata={},
    )
    with pytest.raises(RuntimeError, match="asset persistence exploded"):
        await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch(assets=(asset,)))

    for table in (
        "source_items",
        "source_item_revisions",
        "source_assets",
        "collection_runs",
        "collection_checkpoints",
    ):
        assert await _scalar(uow, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_duplicate_execution_is_idempotent(service, source, uow):
    """Running the identical batch twice creates no duplicate rows."""
    asset = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url="https://cdn.example/1.jpg",
        mime_type="image/jpeg",
        content_hash="hash-photo",
        metadata={},
    )
    batch = _batch(assets=(asset,))

    first = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch)
    second = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch)

    assert first.new_items == 1 and first.new_revisions == 1
    assert second.new_items == 0 and second.new_revisions == 0

    revisions = await _scalar(uow, "SELECT count(*) FROM source_item_revisions")
    assets = await _scalar(uow, "SELECT count(*) FROM source_assets")
    runs = await _scalar(uow, "SELECT count(*) FROM collection_runs")
    assert revisions == 1
    assert assets == 1
    assert runs == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_state_events_append_on_reingest(service, source, uow):
    """Append-only baseline: re-ingesting a batch appends an identical event row."""
    event = ObservedStateEvent(
        item_external_id="42",
        type="inaccessible",
        observed_at=COMPLETED_AT,
        reason="hidden from scan",
        evidence={"message_id": 42},
    )
    batch = _batch(state_events=(event,))

    await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch)
    await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch)

    async with uow.pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT type, reason, evidence FROM source_item_state_events ORDER BY id
            """
        )
        rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0] == rows[1]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_out_of_order_parent_resolves_after_all_shells_exist(service, source, uow):
    """A reply earlier in the batch still links once its parent shell exists."""
    reply = _observation(
        external_id="11", text="reply", parent_external_id="10", root_external_id="10"
    )
    parent = _observation(external_id="10", text="root message")
    batch = _batch(items=(reply, parent), adapter_state={})

    result = await service.ingest_batch(source.id, CollectionTrigger.BACKFILL, batch)
    assert result.new_items == 2
    assert result.new_revisions == 2

    async with uow.pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT child.parent_item_id, child.root_item_id, parent.id
            FROM source_items child
            JOIN source_items parent ON parent.external_id = '10'
                AND parent.source_id = child.source_id
            WHERE child.external_id = '11'
            """
        )
        row = await cursor.fetchone()
    assert row is not None
    stored_parent, stored_root, parent_id = row
    assert stored_parent == parent_id
    assert stored_root == parent_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_counters_record_seen_new_updated(service, source):
    """CollectionRun bookkeeping mirrors the batch: seen vs new items vs revisions."""
    first = await service.ingest_batch(
        source.id,
        CollectionTrigger.SCHEDULED,
        _batch(items=(_observation(external_id="1"), _observation(external_id="2"))),
    )
    assert (first.new_items, first.new_revisions) == (2, 2)

    async with service.uow.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT seen_count, new_count, updated_count FROM collection_runs WHERE id = %s",
            (first.collection_run_id,),
        )
        row = await cursor.fetchone()
    assert row == (2, 2, 2)

    second = await service.ingest_batch(
        source.id,
        CollectionTrigger.SCHEDULED,
        _batch(
            items=(
                _observation(external_id="1"),
                _observation(external_id="2", text="edited"),
            )
        ),
    )
    assert (second.new_items, second.new_revisions) == (0, 1)

    async with service.uow.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT seen_count, new_count, updated_count FROM collection_runs WHERE id = %s",
            (second.collection_run_id,),
        )
        row = await cursor.fetchone()
    assert row == (2, 0, 1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_self_referential_parent_reference_is_ignored(service, source):
    """A message replying to itself must not create a self-referencing FK link."""
    result = await service.ingest_batch(
        source.id,
        CollectionTrigger.SCHEDULED,
        _batch(
            items=(
                _observation(
                    external_id="77",
                    parent_external_id="77",
                    root_external_id="77",
                ),
            )
        ),
    )
    assert result.new_items == 1

    async with service.uow.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT parent_item_id, root_item_id FROM source_items WHERE external_id = '77'",
            (),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_batch_updates_checkpoint_without_success_time(service, source):
    """Transient outcomes refresh adapter_state but keep the last success."""
    success = await service.ingest_batch(
        source.id, CollectionTrigger.SCHEDULED, _batch(adapter_state={"cursor": 1})
    )
    assert success.new_items == 1

    transient = _batch(
        outcome=CollectionOutcome.TRANSIENT,
        adapter_state={"cursor": 2},
        error_kind="timeout",
    )
    result = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, transient)

    async with service.uow.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT adapter_state, last_success_at FROM collection_checkpoints WHERE source_id = %s",
            (source.id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == {"cursor": 2}
        assert row[1] == COMPLETED_AT

        run_cursor = await conn.execute(
            "SELECT status, error_kind FROM collection_runs WHERE id = %s",
            (result.collection_run_id,),
        )
        run_row = await run_cursor.fetchone()
        assert run_row is not None
        assert run_row[0] == "transient"
        assert run_row[1] == "timeout"


# ---------------------------------------------------------------------------
# Plan 3 Task 2: atomic relevance wiring inside ingest_batch_in_transaction.
# ---------------------------------------------------------------------------


async def _bind(conn, source_id: int, edition_id: int) -> None:
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (source_id, edition_id),
    )


async def _fetch_scalar(conn, sql: str, params: tuple = ()) -> object:
    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _deferred_event_jobs(conn) -> list[list[int]]:
    """Queued process_event_revisions jobs as revision lists."""
    import json

    cursor = await conn.execute(
        """
        SELECT args->>'revision_ids'
        FROM procrastinate.procrastinate_jobs
        WHERE task_name = 'process_event_revisions'
        ORDER BY id
        """
    )
    rows = await cursor.fetchall()
    return [json.loads(row[0]) for row in rows]


class _ExplodingDeferral:
    """Stand-in task whose configure() always fails, simulating queue outage."""

    @staticmethod
    def configure(**_kwargs):
        raise RuntimeError("defer exploded")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ingest_commits_revision_and_deferred_event_job_together(
    service, source, edition, production_jobs_app, database_config
):
    """New revision + process_event_revisions job commit atomically."""
    import psycopg

    async with service.uow.pool.connection() as conn:
        await _bind(conn, source.id, edition.id)

        async with conn.transaction():
            result = await service.ingest_batch_in_transaction(
                conn, source_id=source.id, trigger=CollectionTrigger.SCHEDULED, batch=_batch()
            )
            assert len(result.new_revision_ids) == 1
            revision_id = result.new_revision_ids[0]

            # Pre-commit, an outside connection sees neither revision nor job.
            outside = await psycopg.AsyncConnection.connect(database_config.url, autocommit=True)
            try:
                assert (
                    await _fetch_scalar(outside, "SELECT COUNT(*) FROM source_item_revisions") == 0
                )
                assert (
                    await _fetch_scalar(
                        outside, "SELECT COUNT(*) FROM procrastinate.procrastinate_jobs"
                    )
                    == 0
                )
            finally:
                await outside.close()

    async with service.uow.pool.connection() as conn:
        jobs = await _deferred_event_jobs(conn)

    assert jobs == [[revision_id]]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_forced_defer_failure_rolls_back_ingestion(
    service, source, edition, conn, monkeypatch, jobs_import_env
):
    """A failing deferral aborts the whole ingestion transaction:
    revisions, runs, checkpoints and queued jobs all roll back."""
    import src.jobs.event_processing as event_jobs

    monkeypatch.setattr(event_jobs, "process_event_revisions_task", _ExplodingDeferral())

    await _bind(conn, source.id, edition.id)

    with pytest.raises(RuntimeError, match="defer exploded"):
        await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, _batch())

    async with service.uow.pool.connection() as db:
        for table in (
            "source_items",
            "source_item_revisions",
            "collection_runs",
            "collection_checkpoints",
            "procrastinate.procrastinate_jobs",
        ):
            assert await _fetch_scalar(db, f"SELECT COUNT(*) FROM {table}") == 0
