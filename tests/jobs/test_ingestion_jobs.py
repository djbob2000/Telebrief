"""Procrastinate ingestion jobs: dispatcher scheduling and scan_source behavior.

Two layers are tested separately:

* queue assertions run against a Procrastinate ``InMemoryConnector`` app that
  the installed runtime exposes as ``procrastinate_app``; queued jobs are read
  back through ``app.connector.list_jobs_all()`` (rows keyed like
  ``queue_name``/``task_name``/``args``/``priority``/``queueing_lock``), so no
  real PostgreSQL queue tables are needed for enqueue assertions;
* database assertions (sources, checkpoints, runs, items) run against the
  persistent test database and are gated on TELEBRIEF_TEST_DATABASE_URL.

The stub ``scan_source`` task registered on the memory app is a probe: the
real ``scan_source`` body is exercised directly (not through a worker) with a
fake collector injected into the jobs module's collector registry.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import procrastinate
import psycopg
import pytest
from procrastinate.testing import InMemoryConnector
from psycopg.types.json import Jsonb

from src.bootstrap import ApplicationInfrastructure
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork
from src.domain.sources import Source
from src.ingestion.errors import TransientCollectionError
from src.ingestion.models import (
    CollectionBatch,
    CollectionCheckpoint,
    CollectionOutcome,
    ObservedItem,
)
from src.ingestion.registry import CollectorRegistry
from src.runtime import clear_runtime, install_runtime

UTC = timezone.utc

# Placeholder only used by tests that never open the production app; the real
# value comes from TELEBRIEF_TEST_DATABASE_URL when the database is required.
FALLBACK_DATABASE_URL = "postgresql://telebrief:telebrief@localhost:5432/telebrief_test"

requires_db = pytest.mark.skipif(
    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
)

NOW = datetime(2026, 8, 20, 10, 30, tzinfo=UTC)
COMPLETED_AT = NOW + timedelta(seconds=5)


def _scheduled_ts(hour: int, minute: int) -> int:
    """Procrastinate-style periodic timestamp for 2026-08-20 HH:MM UTC."""
    return int(datetime(2026, 8, 20, hour, minute, tzinfo=UTC).timestamp())


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure registry + error contracts (no environment requirements)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transient_error_carries_source_id():
    error = TransientCollectionError(source_id=42)
    assert error.source_id == 42
    assert "42" in str(error)


class _CountingCollector:
    def __init__(self) -> None:
        self.build_count = 0

    def build(self):
        self.build_count += 1
        return self

    async def scan(self, source, checkpoint, context):  # pragma: no cover - shape only
        raise AssertionError("scan not expected in registry tests")


@pytest.mark.unit
def test_collector_registry_selects_and_caches_per_platform():
    collector = _CountingCollector()
    registry = CollectorRegistry()
    registry.register("telegram", collector.build)

    first = registry.select("telegram")
    second = registry.select("telegram")

    assert first is second is collector
    assert collector.build_count == 1


@pytest.mark.unit
def test_collector_registry_reregister_replaces_cached_instance():
    first_collector = _CountingCollector()
    second_collector = _CountingCollector()
    registry = CollectorRegistry()
    registry.register("telegram", first_collector.build)
    registry.select("telegram")

    registry.register("telegram", second_collector.build)

    assert registry.select("telegram") is second_collector


@pytest.mark.unit
def test_collector_registry_unknown_platform_raises_lookup_error():
    registry = CollectorRegistry()

    with pytest.raises(LookupError):
        registry.select("facebook")


@pytest.mark.unit
def test_collector_registry_reports_registered_platforms():
    collector = _CountingCollector()
    registry = CollectorRegistry()
    registry.register("telegram", collector.build)

    assert "telegram" in registry.registered_platforms()
    assert "facebook" not in registry.registered_platforms()


class _ClosableCollector:
    """Records aclose calls so shutdown wiring can be asserted."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def scan(self, source, checkpoint, context):  # pragma: no cover - shape only
        raise AssertionError("scan not expected in registry tests")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_registry_aclose_closes_cached_instances():
    collector = _ClosableCollector()
    registry = CollectorRegistry()
    registry.register("telegram", lambda: collector)
    registry.select("telegram")

    await registry.aclose()

    assert collector.closed is True
    # A closed cache must not hand out stale instances afterwards.
    assert registry._instances == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_registry_aclose_tolerates_collectors_without_aclose():
    collector = _CountingCollector()
    registry = CollectorRegistry()
    registry.register("telegram", lambda: collector)
    registry.select("telegram")

    await registry.aclose()


@pytest.mark.unit
def test_worker_module_imports_without_database_url():
    """`python -c "import src.worker"` must not demand DATABASE_URL.

    All heavy imports (config, infrastructure, queue app) live inside the
    worker coroutine so the module can be imported on machines without any
    database configuration.
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env.pop("TELEBRIEF_TEST_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", "import src.worker"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Task registration metadata (needs the jobs import env, not the database)
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Environment so importing src.jobs.app works without repo-root DB config.

    Mirrors the fixture in tests/db/test_transactional_defer.py; falls back to
    a placeholder URL so import-only tests also run without the DB variable.
    """
    url = os.environ.get("TELEBRIEF_TEST_DATABASE_URL", FALLBACK_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", url)
    (tmp_path / "config.yaml").write_text(
        "database:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return url


@pytest.mark.usefixtures("jobs_import_env")
def test_scan_source_retry_strategy_is_bounded_and_transient_only() -> None:
    from src.jobs.app import procrastinate_app
    from src.jobs.ingestion import SCAN_SOURCE_TASK_NAME, TransientCollectionError

    task = procrastinate_app.tasks[SCAN_SOURCE_TASK_NAME]
    strategy = task.retry_strategy

    assert strategy is not None
    # max_attempts counts TOTAL executions (gate: attempts >= max_attempts,
    # attempts starts at 0 on the first run): 2 = initial attempt plus exactly
    # two retries, honoring "transient may retry twice".
    assert strategy.max_attempts == 2
    assert strategy.retry_exceptions == (TransientCollectionError,)
    # Increasing wait across retries.
    assert strategy.linear_wait > 0 or strategy.exponential_wait > 0


@pytest.mark.usefixtures("jobs_import_env")
def test_scan_source_task_registered_on_collection_queue() -> None:
    from src.jobs.app import procrastinate_app
    from src.jobs.ingestion import (
        NORMAL_COLLECTION_PRIORITY,
        PRE_PUBLISH_PRIORITY,
        SCAN_SOURCE_TASK_NAME,
    )

    assert procrastinate_app.tasks[SCAN_SOURCE_TASK_NAME].queue == "collection"
    assert NORMAL_COLLECTION_PRIORITY == 0
    assert PRE_PUBLISH_PRIORITY == 100


@pytest.mark.usefixtures("jobs_import_env")
def test_dispatcher_periodic_registration() -> None:
    from src.jobs.app import procrastinate_app

    # Procrastinate names tasks by their dotted module path unless an explicit
    # name is given; the dispatcher keeps the default.
    periodic_entries = list(procrastinate_app.periodic_registry.periodic_tasks.values())
    matching = [
        entry for entry in periodic_entries if entry.task.name.endswith("dispatch_due_sources")
    ]
    assert len(matching) == 1

    dispatcher_task = matching[0].task
    assert dispatcher_task.queue == "maintenance"
    assert dispatcher_task.queueing_lock == "source-collection-dispatcher"
    assert matching[0].cron == "* * * * *"
    assert matching[0].periodic_id == "source-collection-dispatcher"
    assert dispatcher_task.name in procrastinate_app.tasks


# ---------------------------------------------------------------------------
# Database-backed dispatcher and scan_source behavior
# ---------------------------------------------------------------------------


@pytest.fixture
async def memory_app(jobs_import_env) -> procrastinate.App:
    """Unopened InMemory app carrying a scan_source probe under the same name.

    Importing src.jobs.ingestion builds the production app at import time, so
    the import-env fixture must land first.
    """
    del jobs_import_env
    from src.jobs.ingestion import SCAN_SOURCE_TASK_NAME

    app = procrastinate.App(connector=InMemoryConnector())
    blueprint = procrastinate.Blueprint()

    @blueprint.task(name=SCAN_SOURCE_TASK_NAME, queue="collection")
    async def scan_source_probe(source_id: int, trigger: str) -> None:
        del source_id, trigger

    app.add_tasks_from(blueprint, namespace="")
    return app


async def _truncate(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(
        """
        TRUNCATE source_items, source_item_revisions, source_assets,
                 source_item_state_events, collection_checkpoints,
                 collection_runs, source_editions, sources, editions
        RESTART IDENTITY CASCADE
        """
    )


@pytest.fixture
async def db_conn(database_config) -> AsyncIterator[psycopg.AsyncConnection]:
    """Autocommit connection over a truncated slice of the test database."""
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await _truncate(conn)
        yield conn
    finally:
        await _truncate(conn)
        await conn.close()


@pytest.fixture
async def infrastructure(memory_app, database_config) -> AsyncIterator[ApplicationInfrastructure]:
    """Runtime whose procrastinate_app is the InMemory queue app.

    Domain pool/UoW stay real so dispatcher queries and ingestion persistence
    hit the actual test database while job deferrals land in memory.
    """
    domain_pool = await open_pool(database_config)
    infra = ApplicationInfrastructure(
        pool=domain_pool,
        uow=DatabaseUnitOfWork(domain_pool),
        procrastinate_app=memory_app,
    )
    await infra.open()
    install_runtime(infra)
    try:
        yield infra
    finally:
        clear_runtime(infra)
        await infra.close()
        await close_pool(domain_pool)


@pytest.fixture
def install_fake_collector(monkeypatch: pytest.MonkeyPatch):
    """Swap the jobs module collector registry for one serving a given fake."""
    from src.jobs import ingestion as ingestion_jobs

    def _install(collector) -> object:
        registry = CollectorRegistry()
        registry.register("telegram", lambda: collector)
        monkeypatch.setattr(ingestion_jobs, "collector_registry", registry)
        return collector

    return _install


class FakeCollector:
    """Records scans and replays a canned batch; never touches the network."""

    def __init__(self, batch: CollectionBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[Source, CollectionCheckpoint | None]] = []

    async def scan(self, source, checkpoint, context):
        del context
        self.calls.append((source, checkpoint))
        return self.batch


def _observed_item(external_id: str = "101") -> ObservedItem:
    return ObservedItem(
        kind="telegram_message",
        external_id=external_id,
        text="hello world",
        author_name="Author",
        published_at=NOW,
        canonical_url=f"https://t.me/example/{external_id}",
        metadata={"topic_id": None},
        observed_at=NOW,
    )


def _batch(
    outcome: CollectionOutcome,
    *,
    items: tuple[ObservedItem, ...] = (),
    adapter_state: dict | None = None,
    error_kind: str | None = None,
) -> CollectionBatch:
    return CollectionBatch(
        outcome=outcome,
        items=items,
        assets=(),
        state_events=(),
        adapter_state=adapter_state if adapter_state is not None else {},
        started_at=NOW,
        completed_at=COMPLETED_AT,
        error_kind=error_kind,
    )


async def _seed_source(
    db_conn: psycopg.AsyncConnection,
    *,
    name: str = "Example",
    platform: str = "telegram",
    enabled: bool = True,
    interval_minutes: int | None = 45,
) -> int:
    options: dict = (
        {} if interval_minutes is None else {"schedule": {"interval_minutes": interval_minutes}}
    )
    cursor = await db_conn.execute(
        """
        INSERT INTO sources(platform, kind, external_id, url, name, role, enabled, collector_options)
        VALUES (%s, 'channel', %s, NULL, %s, 'local_media', %s, %s)
        RETURNING id
        """,
        (platform, f"@{name.lower()}", name, enabled, Jsonb(options)),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _seed_checkpoint(
    db_conn: psycopg.AsyncConnection,
    source_id: int,
    *,
    last_success_at: datetime | None = None,
    backoff_until: datetime | None = None,
    adapter_state: dict | None = None,
) -> None:
    await db_conn.execute(
        """
        INSERT INTO collection_checkpoints(source_id, last_success_at, last_scan_at, adapter_state, backoff_until)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            source_id,
            last_success_at,
            last_success_at,
            Jsonb(adapter_state if adapter_state is not None else {}),
            backoff_until,
        ),
    )


async def _queued_jobs(app: procrastinate.App) -> list[dict]:
    rows = list(await app.connector.list_jobs_all())
    return sorted((dict(row) for row in rows), key=lambda row: int(row["id"]))


async def _fetch_one(db_conn: psycopg.AsyncConnection, query: str, params: tuple) -> tuple | None:
    cursor = await db_conn.execute(query, params)
    return await cursor.fetchone()


@requires_db
async def test_dispatch_enqueues_source_without_checkpoint_immediately(
    db_conn, infrastructure
) -> None:
    from src.jobs.ingestion import dispatch_due_sources

    source_id = await _seed_source(db_conn)

    await dispatch_due_sources(timestamp=_scheduled_ts(10, 30))

    queued = await _queued_jobs(infrastructure.procrastinate_app)
    assert len(queued) == 1
    assert queued[0]["task_name"].endswith("scan_source")
    assert queued[0]["args"] == {"source_id": source_id, "trigger": "scheduled"}
    assert queued[0]["queue_name"] == "collection"
    assert queued[0]["queueing_lock"] == f"scan-source:{source_id}"
    assert queued[0]["lock"] is None


@requires_db
async def test_dispatch_45min_interval_not_due_one_minute_before(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import dispatch_due_sources

    source_id = await _seed_source(db_conn, interval_minutes=45)
    await _seed_checkpoint(db_conn, source_id, last_success_at=_at(10, 0))

    await dispatch_due_sources(timestamp=_scheduled_ts(10, 44))

    assert await _queued_jobs(infrastructure.procrastinate_app) == []


@requires_db
async def test_dispatch_45min_interval_due_at_boundary(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import dispatch_due_sources

    source_id = await _seed_source(db_conn, interval_minutes=45)
    await _seed_checkpoint(db_conn, source_id, last_success_at=_at(10, 0))

    await dispatch_due_sources(timestamp=_scheduled_ts(10, 45))

    queued = await _queued_jobs(infrastructure.procrastinate_app)
    assert [row["args"]["source_id"] for row in queued] == [source_id]


@requires_db
async def test_dispatch_skips_disabled_sources(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import dispatch_due_sources

    await _seed_source(db_conn, enabled=False)

    await dispatch_due_sources(timestamp=_scheduled_ts(10, 30))

    assert await _queued_jobs(infrastructure.procrastinate_app) == []


@requires_db
async def test_dispatch_skips_platforms_without_registered_collector(
    db_conn, infrastructure
) -> None:
    """Unknown-platform sources never enter the per-minute failure pile-up."""
    from src.jobs.ingestion import dispatch_due_sources

    await _seed_source(db_conn, name="Unsupported Network", platform="unsupported_network")

    await dispatch_due_sources(timestamp=_scheduled_ts(10, 30))

    assert await _queued_jobs(infrastructure.procrastinate_app) == []


@requires_db
async def test_enqueue_duplicate_queueing_lock_is_logged_noop(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import (
        NORMAL_COLLECTION_PRIORITY,
        CollectionTrigger,
        dispatch_due_sources,
        enqueue_source_scan,
    )

    source_id = await _seed_source(db_conn)

    first_job_id = await enqueue_source_scan(
        source_id=source_id,
        trigger=CollectionTrigger.SCHEDULED,
        priority=NORMAL_COLLECTION_PRIORITY,
    )
    assert isinstance(first_job_id, int)

    duplicate_job_id = await enqueue_source_scan(
        source_id=source_id,
        trigger=CollectionTrigger.SCHEDULED,
        priority=NORMAL_COLLECTION_PRIORITY,
    )
    assert duplicate_job_id is None

    # The dispatcher path hits the same guard.
    await dispatch_due_sources(timestamp=_scheduled_ts(10, 30))
    queued = await _queued_jobs(infrastructure.procrastinate_app)
    assert [int(row["id"]) for row in queued] == [first_job_id]


@requires_db
async def test_enqueue_source_scan_sets_priority_and_trigger(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import PRE_PUBLISH_PRIORITY, CollectionTrigger, enqueue_source_scan

    source_id = await _seed_source(db_conn)

    job_id = await enqueue_source_scan(
        source_id=source_id,
        trigger=CollectionTrigger.PRE_PUBLISH,
        priority=PRE_PUBLISH_PRIORITY,
    )

    assert isinstance(job_id, int)
    queued = await _queued_jobs(infrastructure.procrastinate_app)
    assert len(queued) == 1
    assert queued[0]["priority"] == PRE_PUBLISH_PRIORITY
    assert queued[0]["args"]["trigger"] == "pre_publish"


@requires_db
async def test_enqueue_unknown_source_is_noop(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import (
        NORMAL_COLLECTION_PRIORITY,
        CollectionTrigger,
        enqueue_source_scan,
    )

    result = await enqueue_source_scan(
        source_id=999999,
        trigger=CollectionTrigger.SCHEDULED,
        priority=NORMAL_COLLECTION_PRIORITY,
    )
    assert result is None
    assert await _queued_jobs(infrastructure.procrastinate_app) == []


@requires_db
async def test_list_collection_candidates_joins_checkpoints_and_filters_disabled(
    db_conn, infrastructure
) -> None:
    from src.ingestion.repository import IngestionRepository

    enabled_id = await _seed_source(db_conn, name="Enabled")
    await _seed_source(db_conn, name="Disabled", enabled=False)
    await _seed_checkpoint(
        db_conn,
        enabled_id,
        last_success_at=_at(10, 0),
        adapter_state={"high_watermark_message_id": 7},
    )

    async with infrastructure.pool.connection() as conn:
        candidates = await IngestionRepository().list_collection_candidates(conn)

    assert [source.id for source, _ in candidates] == [enabled_id]
    checkpoint = candidates[0][1]
    assert checkpoint is not None
    assert checkpoint.last_success_at == _at(10, 0)
    assert checkpoint.adapter_state == {"high_watermark_message_id": 7}


@requires_db
async def test_scan_source_happy_path_persists_rows(
    db_conn, infrastructure, install_fake_collector
) -> None:
    from src.jobs.ingestion import scan_source

    source_id = await _seed_source(db_conn)
    batch = _batch(
        CollectionOutcome.SUCCESS,
        items=(_observed_item("101"),),
        adapter_state={"high_watermark_message_id": 101},
    )
    install_fake_collector(FakeCollector(batch))

    await scan_source(source_id=source_id, trigger="scheduled")

    item_row = await _fetch_one(
        db_conn,
        "SELECT external_id FROM source_items WHERE source_id = %s",
        (source_id,),
    )
    assert item_row is not None
    assert item_row[0] == "101"

    revision_row = await _fetch_one(
        db_conn,
        """
        SELECT r.text_content FROM source_item_revisions r
        JOIN source_items i ON i.id = r.source_item_id WHERE i.source_id = %s
        """,
        (source_id,),
    )
    assert revision_row is not None
    assert revision_row[0] == "hello world"

    checkpoint_row = await _fetch_one(
        db_conn,
        """
        SELECT last_success_at, backoff_until, adapter_state
        FROM collection_checkpoints WHERE source_id = %s
        """,
        (source_id,),
    )
    assert checkpoint_row is not None
    assert checkpoint_row[0] == COMPLETED_AT
    assert checkpoint_row[1] is None
    assert checkpoint_row[2]["high_watermark_message_id"] == 101

    run_row = await _fetch_one(
        db_conn,
        "SELECT status, trigger FROM collection_runs WHERE source_id = %s",
        (source_id,),
    )
    assert run_row == ("success", "scheduled")


@requires_db
async def test_scan_source_transient_raises_after_committing_partial_observations(
    db_conn, infrastructure, install_fake_collector
) -> None:
    from src.jobs.ingestion import scan_source

    source_id = await _seed_source(db_conn)
    batch = _batch(CollectionOutcome.TRANSIENT, items=(_observed_item("201"),))
    fake = install_fake_collector(FakeCollector(batch))

    with pytest.raises(TransientCollectionError) as exc_info:
        await scan_source(source_id=source_id, trigger="scheduled")

    assert exc_info.value.source_id == source_id
    # The fake received the persisted checkpoint (None on a first scan).
    assert fake.calls[0][1] is None

    # Partial observations were committed before the retryable raise.
    item_row = await _fetch_one(
        db_conn,
        "SELECT id FROM source_items WHERE source_id = %s AND external_id = '201'",
        (source_id,),
    )
    assert item_row is not None

    run_row = await _fetch_one(
        db_conn, "SELECT status FROM collection_runs WHERE source_id = %s", (source_id,)
    )
    assert run_row == ("transient",)


@requires_db
async def test_scan_source_rate_limited_persists_backoff(
    db_conn, infrastructure, install_fake_collector
) -> None:
    from src.jobs.ingestion import scan_source

    source_id = await _seed_source(db_conn)
    batch = _batch(
        CollectionOutcome.RATE_LIMITED,
        adapter_state={"retry_after_seconds": 600},
        error_kind="flood_wait",
    )
    install_fake_collector(FakeCollector(batch))

    await scan_source(source_id=source_id, trigger="scheduled")

    checkpoint_row = await _fetch_one(
        db_conn,
        "SELECT backoff_until, last_success_at FROM collection_checkpoints WHERE source_id = %s",
        (source_id,),
    )
    assert checkpoint_row is not None
    assert checkpoint_row[0] == COMPLETED_AT + timedelta(seconds=600)
    assert checkpoint_row[1] is None

    run_row = await _fetch_one(
        db_conn, "SELECT status FROM collection_runs WHERE source_id = %s", (source_id,)
    )
    assert run_row == ("rate_limited",)

    # No retryable exception escaped; nothing was queued either.
    assert await _queued_jobs(infrastructure.procrastinate_app) == []


@requires_db
async def test_scan_source_auth_required_returns_silently(
    db_conn, infrastructure, install_fake_collector
) -> None:
    from src.jobs.ingestion import scan_source

    source_id = await _seed_source(db_conn)
    batch = _batch(CollectionOutcome.AUTH_REQUIRED, error_kind="session_unauthorized")
    install_fake_collector(FakeCollector(batch))

    await scan_source(source_id=source_id, trigger="scheduled")

    checkpoint_row = await _fetch_one(
        db_conn,
        "SELECT backoff_until FROM collection_checkpoints WHERE source_id = %s",
        (source_id,),
    )
    assert checkpoint_row is not None
    assert checkpoint_row[0] is None

    run_row = await _fetch_one(
        db_conn, "SELECT status FROM collection_runs WHERE source_id = %s", (source_id,)
    )
    assert run_row == ("auth_required",)


@requires_db
async def test_scan_source_missing_source_is_noop(db_conn, infrastructure) -> None:
    from src.jobs.ingestion import scan_source

    await scan_source(source_id=424242, trigger="scheduled")

    run_row = await _fetch_one(db_conn, "SELECT COUNT(*) FROM collection_runs", ())
    assert run_row is not None
    assert int(run_row[0]) == 0


@requires_db
async def test_scan_source_hands_checkpoint_to_collector(
    db_conn, infrastructure, install_fake_collector
) -> None:
    from src.jobs.ingestion import scan_source

    source_id = await _seed_source(db_conn)
    await _seed_checkpoint(
        db_conn,
        source_id,
        adapter_state={"high_watermark_message_id": 55},
    )
    batch = _batch(CollectionOutcome.SUCCESS, adapter_state={"high_watermark_message_id": 56})
    fake = install_fake_collector(FakeCollector(batch))

    await scan_source(source_id=source_id, trigger="scheduled")

    seen_source, seen_checkpoint = fake.calls[0]
    assert seen_source.id == source_id
    assert seen_checkpoint is not None
    assert seen_checkpoint.adapter_state["high_watermark_message_id"] == 55
