# Generic Ingestion and Telegram Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce provider-neutral immutable source ingestion and move Telegram collection to scheduled persistent ingestion without changing editorial semantics.

**Architecture:** Telethon becomes one Collector adapter that returns provider-neutral observations. `IngestionService` owns persistence transactions, dedup/revision creation, and CollectionRun/checkpoint updates; Plan 3 attaches atomic edition-processing deferral once those semantic tasks exist. Existing digest/article code temporarily reads persisted Telegram revisions through a compatibility reader instead of collecting on demand.

**Tech Stack:** Python 3.14+, Psycopg 3 async, PostgreSQL 18, Procrastinate, Telethon, existing filter/config code.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- `SourceItem` identity is stable; revisions are immutable.
- Collectors do not write the DB and do not decide editorial relevance.
- Checkpoints optimize scans but never provide the dedup correctness guarantee.
- Global source rows have no `edition_id`; use `source_editions` for edition eligibility.
- Telegram is persistently collected on schedule; publication callers must stop opening Telethon sessions directly once compatibility reader cutover is enabled.
- Keep old `Message` as a compatibility DTO only; do not use it in new repository interfaces.
- Collection failures are source-local and fail soft.
- Persist raw provider observations before legacy `MessageFilter` chains; never let a publication-time filter erase Source history.

---

## File structure locked by this plan

Create:

```text
migrations/0004_source_items.sql
src/ingestion/
  __init__.py
  models.py
  protocol.py
  repository.py
  service.py
  registry.py
  schedule.py
  errors.py
  reader.py
src/providers/
  __init__.py
  telegram.py
src/jobs/ingestion.py
src/worker.py
tests/ingestion/
  test_repository.py
  test_service.py
  test_registry.py
  test_schedule.py
  test_reader.py
tests/providers/test_telegram_provider.py
tests/jobs/test_ingestion_jobs.py
```

Modify:

```text
src/collector.py
src/core.py
src/config_loader.py
src/scheduler.py
main.py
tests/test_core.py
tests/test_scheduler.py
config.yaml.example
```

### Task 1: Add immutable source-item schema

**Files:**
- Create: `migrations/0004_source_items.sql`
- Test: `tests/ingestion/test_repository.py`

**Interfaces:**
- Tables: `source_items`, `source_item_revisions`, `source_assets`, `source_item_state_events`.
- `UNIQUE(source_id, external_id)` is the primary item correctness constraint.
- `IngestionRepository.get_or_create_item_shell(conn, item) -> tuple[SourceItem, bool]`, where the bool says whether the stable identity was inserted; parent/root links are resolved in a second pass with `ensure_relationships(conn, source_id, item_id, parent_external_id, root_external_id)` so batch order cannot break replies.
- A revision has `collected_at`, `content_hash`, immutable normalized payload, and monotonic per-item `revision_no`.
- `NewSourceItem` fields are `source_id`, `kind`, `external_id`, optional `parent_item_id`/`root_item_id`, author identity, canonical URL, `published_at`, and JSON metadata; revision text/payload is inserted separately.

- [ ] **Step 1: Write failing schema/repository expectations**

```python
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_source_external_id_is_unique(conn, source):
    repo = IngestionRepository()
    item = NewSourceItem(
        source_id=source.id,
        external_id="42",
        kind="telegram_message",
        parent_item_id=None,
        root_item_id=None,
        author_name="Resident",
        author_external_id=None,
        canonical_url="https://t.me/example/42",
        published_at=datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc),
        metadata={"topic_id": None},
    )
    first, first_created = await repo.get_or_create_item_shell(conn, item)
    second, second_created = await repo.get_or_create_item_shell(conn, item)
    assert first_created is True
    assert second_created is False
    assert second.id == first.id
```

Also test FK parent/root references and `revision_no` uniqueness.

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_repository.py -q
```

- [ ] **Step 3: Create schema**

Core SQL shape:

```sql
CREATE TABLE source_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id),
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    parent_item_id BIGINT NULL REFERENCES source_items(id),
    root_item_id BIGINT NULL REFERENCES source_items(id),
    author_name TEXT NULL,
    author_external_id TEXT NULL,
    canonical_url TEXT NULL,
    published_at TIMESTAMPTZ NULL,
    first_collected_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(source_id, external_id)
);
```

`source_item_revisions` uses `UNIQUE(source_item_id, revision_no)` and an index on `(source_item_id, content_hash)`, **not** global uniqueness on content hash. `IngestionRepository.insert_revision_if_changed()` compares only with the latest revision: `A -> B -> A` must create revision 3 because a source can edit and later revert. Assets link to exact revision. State events are append-only.

- [ ] **Step 4: Run migration tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test python scripts/migrate.py
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_repository.py -q
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0004_source_items.sql tests/ingestion/test_repository.py
git commit -m "feat(ingestion): add immutable source item schema"
```

### Task 2: Define provider-neutral collection types and Collector protocol

**Files:**
- Create: `src/ingestion/models.py`
- Create: `src/ingestion/protocol.py`
- Test: `tests/ingestion/test_service.py`

**Interfaces:**
- Produces `ObservedItem`, `ObservedAsset`, `ObservedStateEvent`, `CollectionBatch`, `CollectionOutcome`, `CollectionTrigger`.
- Produces protocol:

```python
class Collector(Protocol):
    async def scan(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
    ) -> CollectionBatch:
        raise NotImplementedError
```

- [ ] **Step 1: Write type/behavior tests**

```python
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
```

- [ ] **Step 2: Run and confirm failure**

```bash
pytest tests/ingestion/test_service.py -q -k observed
```

- [ ] **Step 3: Implement minimal frozen dataclasses/enums**

Start from these exact DTO boundaries:

```python
@dataclass(frozen=True)
class ObservedItem:
    kind: str
    external_id: str
    text: str
    author_name: str | None
    published_at: datetime | None
    canonical_url: str | None
    metadata: dict[str, JSONValue]
    observed_at: datetime
    parent_external_id: str | None = None
    root_external_id: str | None = None


@dataclass(frozen=True)
class ObservedAsset:
    item_external_id: str
    kind: str
    external_url: str | None
    mime_type: str | None
    content_hash: str | None
    metadata: dict[str, JSONValue]


@dataclass(frozen=True)
class ObservedStateEvent:
    item_external_id: str
    type: str
    observed_at: datetime
    reason: str
    evidence: dict[str, JSONValue]


@dataclass(frozen=True)
class CollectionBatch:
    outcome: CollectionOutcome
    items: tuple[ObservedItem, ...]
    assets: tuple[ObservedAsset, ...]
    state_events: tuple[ObservedStateEvent, ...]
    adapter_state: dict[str, JSONValue]
    started_at: datetime
    completed_at: datetime
    error_kind: str | None = None
```

Use `@dataclass(frozen=True)` for observation DTOs. `ObservedAsset` and `ObservedStateEvent` address their parent item by stable `item_external_id`; the ingestion service resolves that identity to the exact SourceItem/Revision inside its transaction. `ObservedStateEvent` carries state type (`deleted_at_source | inaccessible | restored` plus explicitly added provider-neutral future enum values), observation time, reason, and evidence. Keep raw provider metadata as JSON-compatible dictionaries. `CollectionOutcome` includes `success`, `transient`, `rate_limited`, `auth_required`, `account_action_required`, `access_denied`, `source_not_found`, `layout_changed`, `permanent`.

- [ ] **Step 4: Run unit tests**

```bash
pytest tests/ingestion/test_service.py -q -k observed
```

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/models.py src/ingestion/protocol.py tests/ingestion/test_service.py
git commit -m "feat(ingestion): define generic collector contract"
```

### Task 3: Implement ingestion repository and transactional service

**Files:**
- Create: `src/ingestion/repository.py`
- Create: `src/ingestion/service.py`
- Test: `tests/ingestion/test_repository.py`
- Test: `tests/ingestion/test_service.py`

**Interfaces:**
- Produces `IngestionService.ingest_batch(source_id, trigger, batch) -> IngestionResult`.
- Produces `IngestionService.ingest_batch_in_transaction(conn, source_id, trigger, batch) -> IngestionResult` for later provider-specific application services that need to co-commit provider coverage/state with generic source history; this method never commits.
- Service receives `DatabaseUnitOfWork` and `IngestionRepository`.
- In this migration plan it persists raw source history only; Plan 3 Task 2 adds atomic downstream relevance deferral once the relevance policy/task actually exists. This prevents Plan 2 from queuing jobs for an unregistered future task.

- [ ] **Step 1: Write failing tests for new/edit/unchanged observations**

```python
result1 = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch(text="hello"))
assert result1.new_items == 1
assert result1.new_revisions == 1

result2 = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch(text="hello"))
assert result2.new_items == 0
assert result2.new_revisions == 0

result3 = await service.ingest_batch(source.id, CollectionTrigger.SCHEDULED, batch(text="hello edited"))
assert result3.new_revisions == 1
```

Assert SourceItem/Revision/asset/state/checkpoint/run writes commit together and rollback together. Add an out-of-order parent test where a reply appears before its parent in the batch and still resolves `parent_item_id/root_item_id` correctly after all stable identities are created. No semantic processing job is expected yet in this plan.

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_repository.py tests/ingestion/test_service.py -q
```

- [ ] **Step 3: Implement service**

Keep transaction ownership in the public wrapper and all persistence in the explicit-connection method:

```python
async def ingest_batch(self, source_id, trigger, batch):
    async with self.uow.transaction() as conn:
        return await self.ingest_batch_in_transaction(
            conn, source_id=source_id, trigger=trigger, batch=batch
        )

async def ingest_batch_in_transaction(self, conn, source_id, trigger, batch):
    run = await self.repo.start_run(
        conn, source_id=source_id, trigger=trigger.value, started_at=batch.started_at
    )
    new_items = 0
    new_revision_ids: list[int] = []
    current_revision_by_external_id: dict[str, int] = {}

    item_by_external_id: dict[str, SourceItem] = {}
    for observation in batch.items:
        item, created = await self.repo.get_or_create_item_shell(
            conn, source_id, observation
        )
        item_by_external_id[observation.external_id] = item
        new_items += int(created)

    for observation in batch.items:
        item = item_by_external_id[observation.external_id]
        await self.repo.ensure_relationships(
            conn, source_id=source_id, item_id=item.id,
            parent_external_id=observation.parent_external_id,
            root_external_id=observation.root_external_id,
        )
        revision = await self.repo.insert_revision_if_changed(
            conn, item.id, observation, collected_at=observation.observed_at
        )
        current = revision or await self.repo.get_latest_revision(conn, item.id)
        current_revision_by_external_id[observation.external_id] = current.id
        if revision is not None:
            new_revision_ids.append(revision.id)

    for asset in batch.assets:
        revision_id = current_revision_by_external_id[asset.item_external_id]
        await self.repo.upsert_asset_for_revision(conn, revision_id, asset)

    for state_event in batch.state_events:
        await self.repo.insert_state_event(conn, source_id, state_event)

    await self.repo.update_checkpoint(
        conn,
        source_id=source_id,
        adapter_state=batch.adapter_state,
        last_success_at=batch.completed_at
        if batch.outcome == CollectionOutcome.SUCCESS
        else None,
    )
    await self.repo.finish_run(
        conn, run_id=run.id, outcome=batch.outcome.value, completed_at=batch.completed_at
    )
    return IngestionResult(
        collection_run_id=run.id,
        new_items=new_items,
        new_revisions=len(new_revision_ids),
        new_revision_ids=tuple(new_revision_ids),
    )
```

Do not hold provider network calls inside this transaction; `scan()` happens before `ingest_batch()`.

- [ ] **Step 4: Test rollback and duplicate execution**

Force a repository failure after revision insertion and assert revision/checkpoint/run do not partially commit. Then execute the same batch twice and assert no duplicate revisions. Plan 3 will add a separate transactional test proving revision/decision scheduling commits atomically once semantic processing is wired.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/repository.py src/ingestion/service.py tests/ingestion
git commit -m "feat(ingestion): persist collection batches atomically"
```

### Task 4: Bootstrap Sources/Editions from current Telegram configuration

**Files:**
- Create: `src/ingestion/registry.py`
- Modify: `src/config_loader.py`
- Modify: `config.yaml.example`
- Test: `tests/ingestion/test_registry.py`

**Interfaces:**
- Produces `SourceRegistry.bootstrap_from_config(conn, config) -> BootstrapResult`.
- Produces `CollectionConfig(telegram_interval_minutes: int = 45)` with validation `5 <= telegram_interval_minutes <= 360`; bootstrap copies this default into each bootstrap-managed Telegram Source's `collector_options.schedule.interval_minutes`.
- Current config remains bootstrap/default data, not runtime source-of-truth after rows exist.
- Telegram source external ID is the configured Telegram channel/chat ID; forum topics remain provider metadata under the same source initially.

- [ ] **Step 1: Write failing bootstrap tests**

Run bootstrap twice and assert no duplicate Sources. Assert current `berdyansk` edition exists and every configured enabled Telegram channel is bound through `source_editions`. Assert legacy editorial roles map explicitly: `news -> local_media`, `community -> community`, `official -> official`, `classifieds -> other`, `mixed -> other`; topic-level role overrides are carried in Telegram observation metadata and do not mutate the global Source role. Assert a default source gets `collector_options={"schedule": {"interval_minutes": 45}}` and that a DB-managed Source keeps its edited interval on later bootstrap.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_registry.py -q
```

- [ ] **Step 3: Implement idempotent bootstrap**

Use one explicit upsert that refuses to overwrite DB-managed rows:

```python
async def upsert_bootstrap_source(conn, source: NewSource) -> Source:
    cur = await conn.execute(
        """
        INSERT INTO sources(platform, kind, external_id, url, name, role, enabled, collector_options, management_mode)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'bootstrap')
        ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET
            name = EXCLUDED.name,
            url = EXCLUDED.url,
            role = EXCLUDED.role,
            enabled = EXCLUDED.enabled,
            collector_options = EXCLUDED.collector_options
        WHERE sources.management_mode = 'bootstrap'
        RETURNING *
        """,
        (source.platform, source.kind, source.external_id, source.url, source.name, source.role, source.enabled, Jsonb(source.collector_options)),
    )
    return Source.from_row(await cur.fetchone())
```

Add `sources.management_mode TEXT NOT NULL DEFAULT 'bootstrap' CHECK (management_mode IN ('bootstrap','database'))` in `0004_source_items.sql`. Use a PostgreSQL upsert with `ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL DO UPDATE SET name = EXCLUDED.name, url = EXCLUDED.url, role = EXCLUDED.role, enabled = EXCLUDED.enabled, collector_options = EXCLUDED.collector_options WHERE sources.management_mode = 'bootstrap'` only while `management_mode='bootstrap'`, updating bootstrap-controlled defaults such as configured name/URL/role. Once an operator/UI switches a Source to `management_mode='database'`, bootstrap must not overwrite `enabled`, role, schedule/options. Never infer ownership from timestamps.

- [ ] **Step 4: Verify idempotence**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_registry.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/registry.py src/config_loader.py config.yaml.example tests/ingestion/test_registry.py
git commit -m "feat(ingestion): bootstrap telegram sources into registry"
```

### Task 5: Adapt Telethon collection to generic ObservedItems

**Files:**
- Create: `src/providers/telegram.py`
- Modify: `src/collector.py`
- Test: `tests/providers/test_telegram_provider.py`
- Modify/Test: `tests/test_collector.py`

**Interfaces:**
- Produces `TelegramCollector(Collector)`.
- The old `MessageCollector` authentication/helper code may be reused internally, but `TelegramCollector.scan()` returns `CollectionBatch`, not `dict[str, list[Message]]`.
- Stable external ID for a normal message is the Telegram message ID scoped by Source; topic ID is metadata, not part of item identity.

- [ ] **Step 1: Write conversion tests**

Mock Telethon message objects and assert:

```python
observed.external_id == str(message.id)
observed.kind == "telegram_message"
observed.published_at == message.date
observed.metadata["topic_id"] == 123
observed.metadata["reply_to_id"] == 77
```

Forward origin and media metadata must be retained. Include `effective_source_role` in observation metadata using the existing topic > channel precedence so topic-specific editorial role survives migration.

- [ ] **Step 2: Run and confirm failure**

```bash
pytest tests/providers/test_telegram_provider.py tests/test_collector.py -q
```

- [ ] **Step 3: Implement adapter**

Implement the adapter as a conversion boundary, not a persistence service:

```python
class TelegramCollector:
    async def scan(self, source, checkpoint, context) -> CollectionBatch:
        started_at = context.now
        messages = await self._fetch_incremental(source, checkpoint)
        observed_at = datetime.now(timezone.utc)
        items = tuple(
            await self._to_observed_item(source, message, observed_at=observed_at)
            for message in messages
        )
        return CollectionBatch(
            outcome=CollectionOutcome.SUCCESS,
            items=items,
            assets=tuple(self._assets_for(messages)),
            state_events=(),
            adapter_state={"high_watermark_message_id": max((m.id for m in messages), default=None)},
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
```

Extract shared session/auth/link/sender/media helpers from `MessageCollector` only when needed to avoid duplication. Do not change interactive `python -m src.collector` authentication behavior in this task.

Checkpoint strategy for initial Telegram adapter: store highest observed message ID plus last successful scan timestamp, but correctness remains `UNIQUE(source_id, external_id)`.

- [ ] **Step 4: Run tests**

```bash
pytest tests/providers/test_telegram_provider.py tests/test_collector.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/providers/telegram.py src/collector.py tests/providers/test_telegram_provider.py tests/test_collector.py
git commit -m "feat(telegram): add persistent ingestion collector adapter"
```

### Task 6: Add Procrastinate Telegram scan tasks and worker entry point

**Files:**
- Modify: `src/jobs/ingestion.py`
- Create: `src/ingestion/schedule.py`
- Create: `src/ingestion/errors.py`
- Create: `src/worker.py`
- Modify: `src/scheduler.py`
- Test: `tests/jobs/test_ingestion_jobs.py`
- Test: `tests/ingestion/test_schedule.py`
- Modify/Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces task `scan_source(source_id: int, trigger: str) -> None`.
- Produces `enqueue_source_scan(source_id: int, trigger: CollectionTrigger, priority: int) -> int | None`, the single place that computes queueing/execution-lock options before deferring `scan_source`; Telegram resolves no execution lock, and Plan 5 extends the resolver for Facebook AuthProfiles.
- Produces `CollectionSchedulePolicy.is_due(source: Source, checkpoint: CollectionCheckpoint | None, scheduled_at: datetime) -> bool`; Plan 2 implements `interval_minutes`, Plan 5 extends it with Facebook `daily_times`.
- Produces periodic dispatcher `dispatch_due_sources(timestamp: int) -> None` which reads DB-managed collection policy and defers source scans.
- Use per-source queueing lock `scan-source:<id>` to avoid backlog. Telegram does not require a shared auth-profile execution lock; Facebook adds that distinct lock in Plan 5.
- Use priority constants `NORMAL_COLLECTION_PRIORITY = 0` and `PRE_PUBLISH_PRIORITY = 100`.
- `PRE_PUBLISH` defers the same task with higher priority.
- Typed collection retry rule: `transient` may retry twice with backoff; `rate_limited` persists `backoff_until`/provider retry-after and lets the periodic dispatcher reschedule after the backoff instead of hammering; `auth_required/account_action_required/access_denied/permanent` do not enter an automatic retry loop.

- [ ] **Step 1: Write failing dispatcher tests with Procrastinate InMemoryConnector**

```python
await dispatch_due_sources(timestamp=scheduled_timestamp)
assert queued[0].task_name.endswith("scan_source")
assert queued[0].task_kwargs["source_id"] == source.id
```

Assert disabled sources are not queued and duplicate queueing lock is handled as a no-op/logged condition. Seed one Source with a 45-minute interval and a successful checkpoint at 10:00: a dispatcher timestamp at 10:44 is not due, 10:45 is due; a Source with no checkpoint is due immediately. Test the pure `CollectionSchedulePolicy` separately from Procrastinate queue assertions.

- [ ] **Step 2: Run failure**

```bash
pytest tests/jobs/test_ingestion_jobs.py tests/ingestion/test_schedule.py tests/test_scheduler.py -q
```

- [ ] **Step 3: Implement jobs**

`scan_source` loads Source/checkpoint, selects collector from a small `CollectorRegistry` keyed by `source.platform`, performs network `scan()` outside a domain transaction, then calls `IngestionService.ingest_batch()`. Commit any partial observations before deciding whether the operational job should retry. Use typed errors only for genuinely transient task retry:

```python
result = await ingestion_service.ingest_batch(source_id, trigger, batch)
if batch.outcome == CollectionOutcome.TRANSIENT:
    raise TransientCollectionError(source_id=source_id)
if batch.outcome == CollectionOutcome.RATE_LIMITED:
    return  # checkpoint.backoff_until prevents dispatcher hammering
if batch.outcome in {
    CollectionOutcome.AUTH_REQUIRED,
    CollectionOutcome.ACCOUNT_ACTION_REQUIRED,
    CollectionOutcome.ACCESS_DENIED,
    CollectionOutcome.PERMANENT,
}:
    return
```

Register `scan_source` with a bounded Procrastinate retry strategy for `TransientCollectionError` only (two retries after the initial attempt, with increasing wait). The repeated execution is safe because SourceItem/Revision application is idempotent.

Register the normal collection clock directly with Procrastinate. The periodic task receives Procrastinate's scheduled timestamp and computes due Sources from persisted checkpoint + per-Source interval; it never sleeps inside a worker job:

```python
@procrastinate_app.periodic(
    cron="* * * * *",
    periodic_id="source-collection-dispatcher",
)
@procrastinate_app.task(
    queue="maintenance",
    queueing_lock="source-collection-dispatcher",
)
async def dispatch_due_sources(timestamp: int) -> None:
    scheduled_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        candidates = await source_repo.list_collection_candidates(conn)
    for source, checkpoint in candidates:
        if schedule_policy.is_due(source, checkpoint, scheduled_at):
            await enqueue_source_scan(
                source_id=source.id,
                trigger=CollectionTrigger.SCHEDULED,
                priority=NORMAL_COLLECTION_PRIORITY,
            )
```

The pure schedule policy considers an interval Source due when it has no successful checkpoint or `last_success_at + interval <= scheduled_at`; `backoff_until > scheduled_at` suppresses normal scheduling before policy evaluation. `enqueue_source_scan` always sets `queueing_lock=f"scan-source:{source_id}"` and asks a small platform lock resolver for an optional execution lock. The Plan 2 resolver returns `None` for Telegram. Plan 5 registers Facebook logic without changing callers.

`src/worker.py` should load config, open `ApplicationInfrastructure`, install it with `install_runtime()`, then run the worker; task functions resolve application services/pool through `get_runtime()` and still pass explicit connections to repositories:

```python
infrastructure = await build_infrastructure(config.database)
await infrastructure.open()
install_runtime(infrastructure)
try:
    await procrastinate_app.run_worker_async(queues=["collection", "maintenance"])
finally:
    clear_runtime(infrastructure)
    await infrastructure.close()
```

`scan_source` and `dispatch_due_sources` start with `runtime = get_runtime()`; no job constructs a fresh connection pool.

At the same commit, extend `src/jobs/app.py` `import_paths` to include `src.jobs.ingestion`. Keep worker queues to modules that exist in this plan (`collection`, `maintenance`).

- [ ] **Step 4: Run job tests and worker import smoke test**

```bash
pytest tests/jobs/test_ingestion_jobs.py tests/ingestion/test_schedule.py tests/test_scheduler.py -q
python -c "import src.worker"
```

- [ ] **Step 5: Commit**

```bash
git add src/jobs/ingestion.py src/ingestion/schedule.py src/ingestion/errors.py src/worker.py src/scheduler.py tests/jobs/test_ingestion_jobs.py tests/ingestion/test_schedule.py tests/test_scheduler.py
git commit -m "feat(ingestion): schedule durable telegram collection"
```

### Task 7: Add persisted-source compatibility reader

**Files:**
- Create: `src/ingestion/reader.py`
- Modify: `src/core.py`
- Modify: `src/config_loader.py`
- Test: `tests/ingestion/test_reader.py`
- Modify/Test: `tests/test_core.py`

**Interfaces:**
- Produces `SourceRevisionReader(uow: DatabaseUnitOfWork, repository: IngestionRepository).read_telegram_messages(edition_slug: str, since: datetime, until: datetime) -> dict[str, list[Message]]` only as a migration adapter.
- Adds transitional `settings.persistent_ingestion: bool = False`; enabling it requires `database.enabled=true`.
- Core publication builders no longer call `MessageCollector` when `settings.persistent_ingestion=true`.

- [ ] **Step 1: Write failing compatibility test**

Insert source items/revisions, then assert reconstructed `Message` preserves text, sender, published timestamp, link, media flags, message/reply/topic IDs, channel/source name.

Add a config test that `persistent_ingestion=true` with `database.enabled=false` raises a clear configuration error. Add a core test that mocks `MessageCollector.connect` to raise if called, enables persistent ingestion, and verifies `_build_digest_parts()` reaches summarization from DB data.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion/test_reader.py tests/test_core.py -q -k persistent
```

- [ ] **Step 3: Implement reader and transitional switch**

Keep the migration adapter narrow and fail closed to persistent semantics once enabled:

```python
async def _collect_messages(config, logger, hours):
    if config.settings.persistent_ingestion:
        runtime = get_runtime()
        reader = SourceRevisionReader(runtime.uow, IngestionRepository())
        now = datetime.now(timezone.utc)
        messages = await reader.read_telegram_messages(
            edition_slug="berdyansk",
            since=now - timedelta(hours=hours),
            until=now,
        )
    else:
        messages = await _collect_messages_legacy_raw(config, logger, hours)

    # Transitional compatibility only: Source history was already persisted raw.
    return await _apply_configured_filters(messages, config, logger)
```

Add `persistent_ingestion` migration flag defaulting `false` until operator cutover. When true, `_collect_messages()` becomes a DB read compatibility adapter and never invokes Telethon. Preserve existing publication-time filter behavior by applying the current filter chain **after** the DB read during this transition; the ingestion path itself never filters before persistence. Do not silently fall back to live Telegram on DB errors once the flag is true; return an explicit operational failure because live fallback would violate snapshot/persistent-ingestion semantics.

- [ ] **Step 4: Run current core/article/digest tests**

```bash
pytest tests/test_core.py tests/test_article_generator.py tests/test_article_pipeline.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/reader.py src/core.py src/config_loader.py tests/ingestion/test_reader.py tests/test_core.py
git commit -m "feat(telegram): read digest inputs from persistent source history"
```

## Plan 2 completion gate

Run:

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/ingestion tests/providers tests/jobs -q
```

Manual acceptance:

1. Run worker and collect one Telegram source twice; second scan creates no duplicate revision when content is unchanged.
2. Edit/test an observed source fixture; new revision is created.
3. Enable `persistent_ingestion` and generate a dry-run digest with Telethon collection disabled; content comes from PostgreSQL.
