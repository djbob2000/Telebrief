# Frozen Publication Snapshot and Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make digest/article generation consume frozen persistent knowledge snapshots and separate generation from immutable destination delivery.

**Architecture:** `PublicationRun` freezes knowledge at `snapshot_at`, deterministic eligibility freezes the candidate set, AI selection freezes exact StoryRevisions/Claims, and all writer/repair/fallback attempts use only those sealed inputs. Successful generation creates one immutable `Publication`; destination rendering creates immutable payloads and delivery retries only resend/reconcile those payloads. The current Story Cards writer and light audit are reused through a deterministic adapter so the present permissive `custom` behavior is preserved.

**Tech Stack:** PostgreSQL 18, Psycopg 3 async, Procrastinate, existing `editorial_*` pipeline, Telegram Bot API, Telegra.ph integration.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- Publication never calls Telegram/Facebook collectors.
- `snapshot_at` is the knowledge cutoff; artifacts completed after it are excluded.
- Candidate set is sealed before AI selection; selected input set is sealed before generation.
- A later StoryRevision never makes an older publication candidate stale.
- Single-source/unverified evidence cannot be excluded by a universal verification threshold.
- `VerificationAssessment` is optional advisory metadata only: its absence, provider failure, or `reported` state cannot make a Story ineligible. Deterministic eligibility must not read a verification-derived publish/allow/block boolean because no such boolean exists.
- Generation retry/repair/fallback cannot expand knowledge scope or select new Stories.
- `Publication` is created only after a successful generation attempt and is immutable.
- Existing light fact-check behavior remains fail-open except unresolved dangerous high-risk wording.
- Delivery retries reuse the exact immutable destination payload; delivery failure never regenerates a Publication.
- `outcome_unknown` must reconcile before resend.

---

## File structure locked by this plan

Create:

```text
migrations/0008_publications.sql
src/publication/
  __init__.py
  models.py
  repository.py
  snapshot.py
  selection.py
  editorial_adapter.py
  generation.py
  delivery.py
  renderers.py
src/jobs/publication.py
src/jobs/schedules.py
tests/jobs/test_publication_schedules.py
tests/publication/
  test_snapshot.py
  test_selection.py
  test_editorial_adapter.py
  test_generation.py
  test_delivery.py
  test_temporal_publication.py
```

Modify:

```text
src/article_generator.py
src/editorial_writer.py only if a narrow reusable entry point is needed
src/editorial_audit.py only to preserve current behavior through the new entry point
src/core.py
src/scheduler.py
src/config_loader.py
pyproject.toml
requirements.txt
uv.lock
src/bot_commands.py
src/mcp_server.py
src/telegraph.py
src/sender.py
main.py
tests/test_article_generator.py
tests/test_article_pipeline.py
tests/test_core.py
tests/test_scheduler.py
tests/test_bot_commands.py
```

### Task 1: Add PublicationRun, frozen candidate/input, attempt, Publication, and Delivery schema

**Files:**
- Create: `migrations/0008_publications.sql`
- Create: `src/publication/models.py`
- Create: `src/publication/repository.py`
- Test: `tests/publication/test_snapshot.py`
- Test: `tests/publication/test_generation.py`
- Test: `tests/publication/test_delivery.py`

**Interfaces:**
- Tables: `eligibility_policy_versions`, `editorial_selection_policy_versions`, `writer_policy_versions`, optional aggregate `publication_policy_versions`, `publication_runs`, `publication_candidates`, `publication_selection_decisions`, `publication_inputs`, `publication_input_claims`, `publication_input_evidence_clusters`, `publication_generation_attempts`, `publications`, `delivery_destinations`, `publication_delivery_payloads`, `publication_deliveries`, `publication_delivery_attempts`.
- `publication_runs.request_key TEXT NOT NULL UNIQUE` provides request idempotency. Scheduled runs use a deterministic key such as `scheduled:berdyansk:daily_article:2026-08-22T20:00:00+03:00`; manual/on-demand callers generate a new UUID key unless explicitly retrying the same request.
- `Publication.winning_generation_attempt_id` must belong to the same run.
- A delivery payload/delivery must belong to the same Publication. Delivery rows also store an internal `idempotency_key` that adapters pass to destinations when the destination API supports one.

- [ ] **Step 1: Write failing DB constraint tests**

Test:

```text
candidate revision belongs to Story
selection decision references an existing candidate row
publication input references INCLUDE decision
winning attempt belongs to publication_run
payload publication matches delivery publication
```

Also test `publications.body NOT NULL`; there is no generating/null-body Publication state. Insert the same scheduled `request_key` twice and assert the repository resolves the existing run rather than creating a second workflow.

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/publication/test_snapshot.py tests/publication/test_generation.py tests/publication/test_delivery.py -q
```

- [ ] **Step 3: Implement schema**

Use explicit lifecycle states for `publication_runs`, for example:

```text
created
candidates_sealed
selected_inputs_sealed
generating
succeeded
failed
```

Candidates and selected inputs are append-only for a run after their seal transition. The service enforces sealing transactionally; DB uniqueness ensures exact StoryRevision cannot be duplicated inside one run.

- [ ] **Step 4: Verify constraints**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/publication -q -k constraint
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0008_publications.sql src/publication/models.py src/publication/repository.py tests/publication
git commit -m "feat(publication): add frozen publication persistence"
```

### Task 2: Freeze deterministic eligibility candidates at `snapshot_at`

**Files:**
- Create: `src/publication/policies.py`
- Create: `src/publication/snapshot.py`
- Modify: `src/publication/repository.py`
- Test: `tests/publication/test_snapshot.py`
- Test: `tests/publication/test_temporal_publication.py`

**Interfaces:**
- Produces `PublicationPolicyService.ensure_current(conn, edition_id: int, publication_type: str, config: Config) -> PublicationPolicySet`; it hashes the exact eligibility config, selection prompt/config, and writer/digest prompt/template inputs and returns immutable policy-version IDs. Same hashes reuse policy rows; changed semantics create new versions.
- Produces `PublicationSnapshotService.create_run(edition_id, publication_type, snapshot_at, policy_ids) -> PublicationRun`; policy IDs are resolved once at run creation and never re-read on retries.
- Produces `seal_candidates(run_id: int) -> list[PublicationCandidate]`.
- Candidate row stores exact `story_revision_id` and derived snapshot features.

- [ ] **Step 1: Write failing temporal candidate tests**

Create the same publication policy inputs twice and assert `ensure_current()` reuses the same version IDs; change one selection/writer prompt hash and assert a new version is created without changing an existing run. Create Story revision #12 at 19:55, revision #13 at 20:03, snapshot at 19:58. Assert candidate references #12 even when queried after 20:03.

Create Claim/extraction/evidence artifact with completion at 20:01 and assert it is not counted in 19:58 candidate snapshot features.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/publication/test_snapshot.py tests/publication/test_temporal_publication.py -q
```

- [ ] **Step 3: Implement temporal eligibility query**

Resolve policy versions when the run is first requested, not during retry/selection/generation:

```python
async with self.uow.transaction() as conn:
    policies = await publication_policy_service.ensure_current(
        conn, edition_id=edition_id, publication_type=publication_type, config=config
    )
    run = await publication_repo.get_or_create_run(
        conn, edition_id=edition_id, publication_type=publication_type,
        snapshot_at=snapshot_at, request_key=request_key, policy_ids=policies.ids,
    )
```

Query only knowledge visible at the run cutoff and insert candidates before sealing:

```python
async with self.uow.transaction() as conn:
    run = await publication_repo.lock_run(conn, run_id)
    rows = await publication_repo.eligible_story_revisions(
        conn,
        edition_id=run.edition_id,
        snapshot_at=run.snapshot_at,
        eligibility_policy_id=run.eligibility_policy_id,
    )
    for rank, row in enumerate(rows, start=1):
        await publication_repo.insert_candidate(conn, run.id, row, deterministic_rank=rank)
    await publication_repo.transition_run(conn, run.id, "candidates_sealed")
```

The SQL must enforce `story_revision.created_at <= snapshot_at` and use only Claims/Story state events/derived artifacts whose creation or completion time is <= `snapshot_at`.

Use SQL that resolves each Story's latest revision **created no later than snapshot** and counts only knowledge artifacts with relevant `completed_at/created_at <= snapshot_at`. Deterministic eligibility may exclude edition mismatch, invalid/archived-outside-policy stories, or no relevant activity; it may not exclude merely because evidence is `reported` or single-source. It must also include an otherwise eligible Story when no `VerificationAssessment` row exists at all.

In the sealing transaction insert all candidates and transition run `created -> candidates_sealed`. Reject attempts to insert candidates after sealing.

- [ ] **Step 4: Run temporal tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/publication/test_snapshot.py tests/publication/test_temporal_publication.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/policies.py src/publication/snapshot.py src/publication/repository.py tests/publication/test_snapshot.py tests/publication/test_temporal_publication.py
git commit -m "feat(publication): freeze temporal candidate snapshots"
```

### Task 3: Add AI editorial selection over the frozen candidate set

**Files:**
- Create: `src/publication/selection.py`
- Modify: `src/jobs/publication.py`
- Test: `tests/publication/test_selection.py`

**Interfaces:**
- Produces `EditorialSelectionService.select(run_id) -> SelectionResult`.
- Decision enum: `INCLUDE | OMIT`.
- Presentation values initially: `lead | normal | brief | unverified_operational | follow_up`.
- AI may only reference candidate `(story_id, story_revision_id)` pairs supplied in the frozen set.

- [ ] **Step 1: Write failing selection tests**

Cases:

1. one useful single-source operational Story can be `INCLUDE`;
2. the same Story remains a candidate and can be `INCLUDE` when VerificationAssessment is missing/unavailable;
3. selector tries to return Story revision not in candidates -> reject response/retry according to bounded provider policy;
4. omitted Story remains persistent and can be included by a later run;
5. retry sees identical candidate set.

- [ ] **Step 2: Run failure**

```bash
pytest tests/publication/test_selection.py -q
```

- [ ] **Step 3: Implement selector**

Persist the exact candidate set before and after the external model call:

```python
candidates = await publication_repo.load_sealed_candidates(run_id)
raw = await selector.select(run, candidates)
validated = validate_selection(raw, allowed={(c.story_id, c.story_revision_id) for c in candidates})

async with self.uow.transaction() as conn:
    for decision in validated:
        await publication_repo.insert_selection_decision(conn, run_id, decision)
        if decision.decision == "INCLUDE":
            await publication_repo.freeze_selected_input(conn, run_id, decision)
    await publication_repo.transition_run(conn, run_id, "selected_inputs_sealed")
    await generate_publication.configure(connection=conn).defer_async(run_id=run_id)
```

Prompt explicitly separates evidence uncertainty from admission. Do not provide instructions such as `minimum_sources`, `confidence >= X`, or `official confirmation required`.

After provider response, in one transaction validate exact candidate keys, insert immutable decisions, insert `publication_inputs` only for INCLUDE decisions, and freeze the exact Claim IDs/EvidenceCluster IDs knowledge-visible at `snapshot_at` into `publication_input_claims` / `publication_input_evidence_clusters`. Then transition run to `selected_inputs_sealed`. Persist ranking/reason/confidence but do not use confidence as a later hard gate.

- [ ] **Step 4: Run selection tests**

```bash
pytest tests/publication/test_selection.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/selection.py src/jobs/publication.py tests/publication/test_selection.py
git commit -m "feat(publication): select from frozen story candidates"
```

### Task 4: Build deterministic Story Cards/source bundle from sealed knowledge

**Files:**
- Create: `src/publication/editorial_adapter.py`
- Modify: `src/article_generator.py`
- Test: `tests/publication/test_editorial_adapter.py`
- Modify/Test: `tests/test_article_generator.py`

**Interfaces:**
- Produces `KnowledgeEditorialAdapter.build(run_id) -> FrozenEditorialInput` containing `EditorialAnalysis` Story Cards plus a `PreparedBundle`/equivalent exact source-record bundle.
- Produces new `ArticleGenerator.generate_from_frozen_input(input: FrozenEditorialInput, attempt_observer: GenerationAttemptObserver) -> tuple[str, str, str]`.
- `GenerationAttemptObserver` receives `started(kind)` / `finished(attempt_id, status, error_kind)` callbacks around each writer, repair, deterministic fallback, and story-renderer fallback model/render operation so those operations become real `PublicationGenerationAttempt` rows without rewriting the current editorial logic wholesale. The production `DatabaseGenerationAttemptObserver` owns short UoW transactions for these audit writes; repositories still receive explicit connections and never commit themselves.
- Existing `generate_article(messages_by_channel)` remains temporarily for compatibility/tests and supplies a no-op observer while delegating to shared internal writer/audit code.

- [ ] **Step 1: Write failing adapter tests**

Given two sealed publication inputs, assert generated Story Cards contain only exact selected StoryRevisions/Claims and reference only SourceItemRevisions knowledge-visible at the run snapshot. A later StoryRevision inserted after sealing must not appear. Add an observer fake and assert a writer call emits one `writer` attempt event and each targeted repair emits its own `repair` attempt event while the old compatibility entry point remains behaviorally unchanged.

- [ ] **Step 2: Run failure**

```bash
pytest tests/publication/test_editorial_adapter.py tests/test_article_generator.py -q
```

- [ ] **Step 3: Refactor existing article pipeline at the narrow seam**

Extract current post-analysis writer/audit/fallback path from `ArticleGenerator.generate_article()` into a method that accepts already-validated `EditorialAnalysis` + exact source bundle. Do **not** rewrite `LightFactChecker` policy.

`KnowledgeEditorialAdapter` maps persistent Claims/EvidenceClusters into the existing Story Card semantics:

```text
strong named/official evidence -> hard_facts with appropriate attribution
resident/community report -> community_observations, status=attributed
contradiction -> disputed/uncertainty
single useful unverified report -> retained with attribution
```

The adapter must never invent a second Claim to make a Story appear corroborated.

- [ ] **Step 4: Run old and new editorial tests**

```bash
pytest tests/publication/test_editorial_adapter.py tests/test_article_generator.py tests/test_editorial_audit.py tests/test_editorial_writer.py -q
```

Expected: current permissive audit tests remain unchanged/passing.

- [ ] **Step 5: Commit**

```bash
git add src/publication/editorial_adapter.py src/article_generator.py tests/publication/test_editorial_adapter.py tests/test_article_generator.py
git commit -m "refactor(editorial): generate articles from frozen knowledge"
```

### Task 5: Add immutable generation attempts and fail-open writer/fallback execution

**Files:**
- Create: `src/publication/generation.py`
- Modify: `src/jobs/publication.py`
- Test: `tests/publication/test_generation.py`
- Modify/Test: `tests/test_article_pipeline.py`

**Interfaces:**
- Produces `PublicationGenerationService.generate(run_id) -> Publication`.
- Attempt kinds: `writer | repair | deterministic_fallback | story_renderer_fallback`; every actual writer/repair/fallback operation is represented, not only the outer pipeline call.
- A successful result creates `Publication` in the same transaction that marks the winning attempt succeeded/run succeeded and defers payload preparation.

- [ ] **Step 1: Write failing generation tests**

Cases:

```text
writer succeeds -> Publication winner=writer attempt
writer + one successful repair -> writer attempt + repair attempt; winner points to the final content-producing repair attempt
writer succeeds, audit unavailable -> Publication still created
writer fails, story renderer succeeds -> Publication winner=story_renderer_fallback attempt
attempt retry -> same run/input IDs
new knowledge after sealing -> never visible
all safe paths fail -> run failed, no Publication row
```

- [ ] **Step 2: Run failure**

```bash
pytest tests/publication/test_generation.py tests/test_article_pipeline.py -q
```

- [ ] **Step 3: Implement generation service**

Create the attempt before network work and create the Publication only from a successful normalized result:

```python
sealed = await publication_repo.load_sealed_generation_input(run_id)
observer = DatabaseGenerationAttemptObserver(publication_repo, run_id=run_id)
try:
    content = await editorial_adapter.generate(sealed, attempt_observer=observer)
except WriterUnavailableError:
    content = await editorial_adapter.generate_safe_fallback(
        sealed, attempt_observer=observer
    )
winning_attempt = observer.last_successful_content_attempt

async with self.uow.transaction() as conn:
    publication = await publication_repo.create_publication(
        conn, run_id=run_id, winning_attempt_id=winning_attempt.id, content=content
    )
    await publication_repo.assert_attempt_succeeded(conn, winning_attempt.id)
    await publication_repo.transition_run(conn, run_id, "succeeded")
    await prepare_delivery_payloads.configure(connection=conn).defer_async(
        publication_id=publication.id
    )
return publication
```

Load sealed inputs only through repository method that refuses non-sealed run state. Create attempt row before external generation call, update its operational outcome afterward. Preserve current `ArticleGenerator` behavior where `FactCheckUnavailableError` publishes valid writer output and only unresolved `publication_blocking` high-risk fixes force safer fallback/removal.

- [ ] **Step 4: Run tests**

```bash
pytest tests/publication/test_generation.py tests/test_article_pipeline.py tests/test_editorial_audit.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/generation.py src/jobs/publication.py tests/publication/test_generation.py tests/test_article_pipeline.py
git commit -m "feat(publication): persist fail-open generation attempts"
```

### Task 6: Add digest generator from frozen Story inputs

**Files:**
- Create: `src/publication/renderers.py`
- Test: `tests/publication/test_generation.py`
- Modify: `src/formatter.py` only if a reusable formatting primitive is needed

**Interfaces:**
- Produces `KnowledgeDigestGenerator.generate(frozen_input) -> PublicationContent`.
- It consumes sealed Story revisions/evidence, not per-channel live summaries.

- [ ] **Step 1: Write failing digest tests**

Fixture with one confirmed utility Story, one single-source useful road closure, one low-value rumor. Assert confirmed and road-closure items can appear; low-value rumor may be omitted by selection. Assert attribution is present for the single-source operational report.

- [ ] **Step 2: Run failure**

```bash
pytest tests/publication/test_generation.py -q -k digest
```

- [ ] **Step 3: Implement compact digest generation**

Keep digest input equal to the sealed publication input:

```python
class KnowledgeDigestGenerator:
    async def generate(self, frozen_input: FrozenPublicationInput) -> PublicationContent:
        payload = DigestPromptPayload.from_frozen_input(frozen_input)
        try:
            result = await self.provider.generate_structured_digest(payload)
            return PublicationContent.from_digest_result(result)
        except ProviderUnavailableError:
            return self.deterministic_renderer.render(payload)
```

Use one focused AI call or deterministic renderer over already-selected frozen Stories. The model is not asked to perform collection/relevance again. Output normalized `headline/lead/body` or a digest-specific structured content object that is persisted as a `Publication` body. Preserve evidence modality.

- [ ] **Step 4: Run test**

```bash
pytest tests/publication/test_generation.py -q -k digest
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/renderers.py src/formatter.py tests/publication/test_generation.py
git commit -m "feat(publication): generate digest from persistent stories"
```

### Task 7: Add immutable destination payloads, delivery attempts, and reconciliation

**Files:**
- Create: `src/publication/delivery.py`
- Modify: `src/publication/renderers.py`
- Modify: `src/jobs/publication.py`
- Modify: `src/sender.py`
- Modify: `src/telegraph.py`
- Test: `tests/publication/test_delivery.py`

**Interfaces:**
- Produces `DeliveryService.prepare(publication_id, destination_config_id) -> PublicationDelivery`.
- Produces `DeliveryService.deliver(delivery_id) -> DeliveryOutcome`.
- Payload is rendered once and hashed.
- Status: `pending | in_progress | succeeded | failed | outcome_unknown`.

- [ ] **Step 1: Write failing delivery tests**

Mock destination API:

1. first Telegram call timeout after simulated remote creation -> `outcome_unknown`;
2. retry handler calls `reconcile()` first;
3. reconciliation finds external ID -> mark succeeded without send;
4. definitive not-found from a destination that supports reliable reconciliation -> resend same payload hash;
5. destination cannot reliably reconcile -> keep `outcome_unknown` and require explicit operator/manual resolution rather than auto-resending with duplicate risk;
6. formatter configuration changes between attempts -> payload content/hash unchanged.

- [ ] **Step 2: Run failure**

```bash
pytest tests/publication/test_delivery.py -q
```

- [ ] **Step 3: Implement destination adapters**

Define narrow protocol:

```python
class DestinationAdapter(Protocol):
    async def send(self, payload: DeliveryPayload) -> SendResult:
        raise NotImplementedError

    async def reconcile(
        self, delivery: PublicationDelivery, payload: DeliveryPayload
    ) -> ReconcileResult:
        raise NotImplementedError
```

Wrap existing Telegram/Telegraph code rather than letting adapters call generation. `reconcile()` must return `SUCCEEDED | NOT_FOUND | UNKNOWN`; automatic resend is allowed only after `NOT_FOUND`. Telegram/Telegraph adapters may return `UNKNOWN` when their APIs cannot reliably prove whether a timed-out create/send succeeded. Where a Telegram payload depends on a successfully created Telegraph URL, create/render that Telegram payload only after the Telegraph delivery succeeded; once created, it is immutable.

- [ ] **Step 4: Run sender/telegraph regression tests**

```bash
pytest tests/publication/test_delivery.py tests/test_sender.py tests/test_telegraph.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/delivery.py src/publication/renderers.py src/jobs/publication.py src/sender.py src/telegraph.py tests/publication/test_delivery.py
git commit -m "feat(delivery): separate immutable publication delivery"
```

### Task 8: Cut scheduler, bot, MCP, and CLI publication calls over to PublicationRun

**Files:**
- Modify: `src/core.py`
- Modify: `src/jobs/app.py`
- Modify: `src/scheduler.py`
- Create: `src/jobs/schedules.py`
- Modify: `src/config_loader.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `uv.lock`
- Modify: `src/bot_commands.py`
- Modify: `src/mcp_server.py`
- Modify: `main.py`
- Test: `tests/test_core.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_bot_commands.py`
- Test: `tests/jobs/test_publication_schedules.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces application facade:

```python
async def request_publication(
    publication_type: str,
    edition_slug: str,
    *,
    snapshot_at: datetime | None = None,
    request_key: str | None = None,
    dry_run: bool = False,
) -> PublicationRequestResult:
```

If `request_key` is omitted, on-demand callers receive a fresh UUID. Scheduled orchestration always passes a deterministic key derived from edition/type/scheduled cutoff, so at-least-once dispatcher execution cannot create duplicate scheduled publications.

- No facade method accepts `messages_by_channel` or instantiates a Collector.
- The new publication facade requires `database.enabled=true` and persistent ingestion; startup/request handling fails fast with a configuration error instead of silently falling back to live provider collection.

- [ ] **Step 1: Write failing no-live-collector and durable-schedule tests**

Add a configuration test that the knowledge-publication facade rejects `database.enabled=false` or `persistent_ingestion=false`. Patch `MessageCollector` and `TelegramCollector.scan` to raise if called. Trigger scheduled digest, scheduled article, `/digest`, CLI `--article`, and MCP digest path against seeded persistent knowledge. Assert PublicationRun/Publication are created and collector mocks remain untouched.

Add schedule-dispatch tests using the Procrastinate periodic timestamp, not wall-clock `now()`: if article time is `20:00` and `pre_publish_lead_minutes=15`, timestamp `19:45` defers high-priority `PRE_PUBLISH` scans and timestamp `20:00` defers a PublicationRun with `snapshot_at=20:00`. A late worker executing the `20:00` periodic job at `20:03` must still use the scheduled `20:00` timestamp.

```python
async def test_schedule_dispatch_uses_periodic_timestamp(procrastinate_app, seeded_sources):
    ts = datetime(2026, 8, 22, 20, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
    await publication_schedule_dispatcher(timestamp=int(ts.timestamp()))
    jobs = await procrastinate_app.job_manager.list_jobs_async()
    publication = next(j for j in jobs if j.task_name.endswith("create_scheduled_publication"))
    assert publication.task_kwargs["snapshot_at"] == ts.isoformat()
```

Also seed a successful digest Publication and assert `get_last_digest()` / `read_last_digest()` compatibility reads it from PostgreSQL rather than `data/last_digest.json`.

- [ ] **Step 2: Run failure**

```bash
pytest tests/test_core.py tests/test_scheduler.py tests/test_bot_commands.py tests/jobs/test_publication_schedules.py tests/test_mcp_server.py -q -k 'publication_run or schedule or last_digest'
```

- [ ] **Step 3: Replace direct generation orchestration**

At the cutover boundary, validate `database.enabled=true` and `settings.persistent_ingestion=true` before constructing publication services; this is a fail-fast invariant, not a fallback switch. `src/core.py` becomes a thin compatibility/facade module around publication services; delete or deprecate `_build_grouped_parts/_build_channel_parts` direct collection use only after equivalent callers move. CLI/bot/MCP request a new run over current knowledge. Replace the file-backed last-digest cache with a repository query over the latest successful digest Publication; keep `read_last_digest()` only as a compatibility facade if callers still import it.

Convert `DigestScheduler` into a compatibility/status facade with **no APScheduler ownership** so existing bot/status wiring can survive the cutover:

```python
class DigestScheduler:
    def start(self) -> None:
        self.is_running = True
        self.logger.info("Publication scheduling is handled by Procrastinate workers")

    def stop(self) -> None:
        self.is_running = False

    def get_next_run_time(self) -> str:
        return format_next_configured_digest_time(self.config, now=datetime.now(timezone.utc))
```

`main.py` may keep constructing this facade for existing command/help dependencies, but it no longer owns a clock. Move clock scheduling itself onto Procrastinate. Keep `DigestScheduler` only as a compatibility/status facade for bot/help callers, but remove its `AsyncIOScheduler` ownership and remove `apscheduler`/`APScheduler` from both `pyproject.toml` and the runtime `requirements.txt` once no imports remain; regenerate `uv.lock`. The dependency edit in this task is complete only after:

```bash
grep -Rni "apscheduler" src main.py || true
uv lock
```

and the grep returns no production imports. Add one one-minute Procrastinate periodic dispatcher that reads configured publication times and uses the periodic `timestamp` to decide what is due. The dispatcher only defers durable work; it never generates content inline.

```python
@procrastinate_app.periodic(
    cron="* * * * *",
    periodic_id="publication-schedule-dispatcher",
)
@procrastinate_app.task(
    queue="maintenance",
    queueing_lock="publication-schedule-dispatcher",
)
async def publication_schedule_dispatcher(timestamp: int) -> None:
    scheduled_for = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    config = load_config()
    for due in due_publication_actions(config, scheduled_for):
        task = pre_publish_refresh if due.kind == "pre_publish" else create_scheduled_publication
        await task.configure(
            queue="publication" if due.kind == "publish" else "collection",
            priority=100 if due.kind == "pre_publish" else 0,
            queueing_lock=due.queueing_lock,
        ).defer_async(**due.task_kwargs)
```

Add `pre_publish_lead_minutes: int = 15` with positive bounded validation. `pre_publish_refresh` fans out ordinary source scans through Plan 2's `enqueue_source_scan()` with trigger `PRE_PUBLISH` and priority `100`, so provider-specific execution locks (including Facebook AuthProfile locks added in Plan 5) are applied consistently; it does **not** wait for them. At the configured publication minute, `create_scheduled_publication` creates/gets the run with `snapshot_at` equal to the scheduled timestamp and deterministic `request_key=f"scheduled:{edition_slug}:{publication_type}:{snapshot_at.isoformat()}"`. Jobs completing after that cutoff remain valid knowledge for future publications but cannot enter this run.

Extend `src/jobs/app.py` `import_paths` with both `src.jobs.publication` and `src.jobs.schedules`; workers consume `publication`, `collection`, `processing`, and `maintenance` queues as configured.

- [ ] **Step 4: Run full publication and existing command tests**

```bash
pytest tests/publication tests/jobs/test_publication_schedules.py tests/test_core.py tests/test_scheduler.py tests/test_bot_commands.py tests/test_mcp_server.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/core.py src/scheduler.py src/jobs/schedules.py src/jobs/app.py src/config_loader.py pyproject.toml requirements.txt uv.lock src/bot_commands.py src/mcp_server.py main.py tests
git commit -m "refactor(publication): schedule and generate from frozen knowledge"
```

### Task 9: Add temporal replay and retry integration regression suite

**Files:**
- Expand: `tests/publication/test_temporal_publication.py`
- Create: `tests/fixtures/publication_snapshot_timeline.json`

**Interfaces:**
- No new production interface; this task locks behavior before Facebook is added.

- [ ] **Step 1: Build timeline fixture**

Include source revision collected 19:40, Story revision 19:50, Publication snapshot 19:58, new source 20:01, Story revision 20:03, later correction 20:20.

- [ ] **Step 2: Assert historical publication and current retrospective knowledge differ**

The old PublicationRun must use only pre-19:58 state. A new current query may see the correction. Retrying the old writer after 20:20 must still see the sealed old inputs.

- [ ] **Step 3: Run suite and fix only real violations**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/publication/test_temporal_publication.py -q
```

- [ ] **Step 4: Run full project verification**

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
```

- [ ] **Step 5: Commit**

```bash
git add tests/publication/test_temporal_publication.py tests/fixtures/publication_snapshot_timeline.json
git commit -m "test(publication): lock historical snapshot semantics"
```

## Plan 4 completion gate

With all provider collectors disabled, seed persistent knowledge and verify a scheduled/on-demand digest and article can both create immutable Publications and deliver them. Then force a delivery timeout after simulated remote success and verify reconciliation prevents a duplicate send.
