# Edition Knowledge, Stories, Places, and Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn persisted source revisions into edition-specific relevance decisions, immutable Claims, embeddings, persistent Stories/StoryRevisions, place resolution, and non-gating evidence assessments.

**Architecture:** Every semantic stage follows `PolicyVersion -> ProcessingRun -> immutable result` and is executed through Procrastinate with DB canonical constraints protecting against at-least-once duplication. Embeddings are created from semantic units (`Claim.normalized_assertion` and `StoryRevision.semantic_text`), never from fixed token/sentence chunks. Story retrieval is hybrid and recall-oriented: vector similarity is one candidate source alongside lexical/state signals and, after Place resolution lands, location/entity overlap; an AI matcher then decides `SAME_STORY` or `NEW_STORY`, and assignment is atomically applied with stale-revision protection. Evidence/verification improve provenance but never become a source-count publication gate.

**Tech Stack:** PostgreSQL 18, pgvector exact search, Psycopg 3 async, Procrastinate, existing AI provider cascade, city context data.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- Deterministic preprocessing may remove technical junk only; no geographic keyword gate.
- AI unavailable at relevance => uncertain/pending, not false `irrelevant`.
- Claims are immutable source-bound assertions.
- Corrections create new Claims + `CORRECTS`/`SUPERSEDES`/`RETRACTS` relations.
- Embed Claims and StoryRevisions only.
- Never split raw source text into fixed token-, character-, or sentence-count embedding chunks. Claim extraction is the semantic chunking boundary.
- `Claim.normalized_assertion` must be understandable without reading the surrounding chat/post; short but complete claims such as `Пожар у вокзала` are valid, while fragments such as `Всё ещё нет` must be resolved against bounded source context before embedding.
- One Claim produces one embedding input; one StoryRevision produces one embedding input. No minimum word/sentence count is required.
- Exact vector similarity is retrieval, never merge permission.
- Story candidate retrieval is a bounded union of independent recall signals; no vector or lexical threshold may drop a Claim or decide Story assignment.
- Location/entity overlap are soft signals.
- SAME_STORY apply must reject a stale selected StoryRevision.
- Story matching executes sequentially per Edition initially.
- Evidence counts and verification states are advisory; one useful community Claim remains eligible later.

---

## File structure locked by this plan

Create:

```text
migrations/0005_relevance_claims.sql
migrations/0006_embeddings_stories.sql
migrations/0007_places_evidence.sql
src/processing/
  __init__.py
  relevance.py
  vision.py
  claims.py
  embeddings.py
  story_matching.py
  places.py
  evidence.py
  verification.py
src/domain/
  claims.py
  stories.py
  places.py
  evidence.py
src/repositories/
  relevance.py
  vision.py
  claims.py
  embeddings.py
  story_candidates.py
  stories.py
  places.py
  evidence.py
src/jobs/processing.py
tests/processing/
  test_relevance.py
  test_vision.py
  test_claims.py
  test_embeddings.py
  test_story_matching.py
  test_places.py
  test_evidence.py
```

Modify:

```text
src/jobs/ingestion.py
src/ai_providers.py only if a small common structured-call helper is genuinely required
src/city_context.py only through an adapter, not by rewriting existing behavior
config.yaml.example
```

### Task 1: Add relevance and Claim schema with policy-version invariants

**Files:**
- Create: `migrations/0005_relevance_claims.sql`
- Create: `src/domain/claims.py`
- Create: `src/repositories/relevance.py`
- Create: `src/repositories/claims.py`
- Test: `tests/processing/test_relevance.py`
- Test: `tests/processing/test_claims.py`

**Interfaces:**
- Tables: `relevance_policy_versions`, `edition_relevance_decisions`, `vision_policy_versions`, `vision_analysis_runs`, `vision_observations`, `claim_extraction_policy_versions`, `claim_extraction_runs`, `processing_attempts`, `claims`, `claim_relations`, `claim_state_events`.
- `processing_attempts(stage, semantic_run_id, attempt_no, provider, model, started_at, completed_at, status, error_kind, metadata)` is operational audit history, not a queue; `(stage, semantic_run_id, attempt_no)` is unique.
- Migration `0005` also adds nullable `editions.current_relevance_policy_id` and its edition-consistent FK now that `relevance_policy_versions` exists.
- One canonical successful ClaimExtraction result per `(source_item_revision_id, edition_id, extraction_policy_id)`.

- [ ] **Step 1: Write failing constraint tests**

Test edition-policy mismatch is rejected by composite FK; duplicate successful extraction run for the same semantic key is rejected; a new policy version is allowed.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_relevance.py tests/processing/test_claims.py -q
```

- [ ] **Step 3: Implement schema and repositories**

Use composite uniqueness on policy `(id, edition_id)` and decision/run FKs that include `edition_id`. Add nullable `editions.current_relevance_policy_id` in this migration and enforce `(current_relevance_policy_id, editions.id) -> relevance_policy_versions(id, edition_id)` with NULL permitted. Task 2 creates/chooses the initial policy row from config and then sets the edition pointer; the SQL migration does not hard-code an AI prompt/config hash. A root relevance decision is unique for `(source_item_revision_id, edition_id, relevance_policy_id)` where `parent_decision_id IS NULL`; a post-vision decision is a new immutable child pointing to the prior `needs_media` decision. Use a partial unique index for canonical ClaimExtraction success, e.g.:

```sql
CREATE UNIQUE INDEX uq_claim_extraction_success
ON claim_extraction_runs(source_item_revision_id, edition_id, extraction_policy_id)
WHERE status = 'succeeded';
```

Claims reference `claim_extraction_run_id NOT NULL`; expose repository insert only inside the success transaction.

- [ ] **Step 4: Verify constraints and repository mapping**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_relevance.py tests/processing/test_claims.py -q
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0005_relevance_claims.sql src/domain/claims.py src/repositories/relevance.py src/repositories/claims.py tests/processing
git commit -m "feat(knowledge): add relevance and claim persistence"
```

### Task 2: Implement AI relevance task with conservative fail-open semantics

**Files:**
- Create: `src/processing/relevance.py`
- Modify: `src/jobs/processing.py`
- Modify: `src/jobs/ingestion.py`
- Modify: `src/ingestion/service.py`
- Modify: `src/jobs/app.py`
- Test: `tests/processing/test_relevance.py`
- Modify/Test: `tests/ingestion/test_service.py`

**Interfaces:**
- Produces `RelevancePolicyService.ensure_current(conn, edition_id: int, config_hash: str, prompt_version: str) -> RelevancePolicyVersion`.
- Produces `RelevanceService.evaluate(source_item_revision_id: int, edition_id: int, policy_id: int) -> EditionRelevanceDecision`.
- Procrastinate task is `evaluate_relevance(context, source_item_revision_id: int, edition_id: int, policy_id: int)` with `pass_context=True` and a retry strategy limited to `TransientProcessingError`. Plan 3 modifies the ingestion downstream deferrer so the policy ID is resolved **before** the job is queued and therefore remains fixed across Procrastinate retries.
- Produces bounded `backfill_relevance(edition_id: int, policy_id: int, after_revision_id: int | None)` for SourceItemRevisions collected before this plan lands.

- [ ] **Step 1: Write failing behavior tests**

Cases:

```text
concrete local resident report -> relevant
obvious unrelated external story -> irrelevant
provider timeout -> two bounded Procrastinate retries; final failed attempt persists `uncertain` with reason `provider_unavailable` and succeeds operationally
media-only item -> needs_media
```

Assert no deterministic keyword rejection occurs before the AI policy sees valid non-empty text. Assert a retried job keeps the originally queued `policy_id` even if a newer relevance policy becomes current before the retry. Assert backfill queues only revisions lacking a root decision for that exact policy. Add a PostgreSQL integration test where a newly inserted SourceItemRevision and its deferred `evaluate_relevance(source_item_revision_id, edition_id, policy_id)` job commit together; force defer failure and assert the ingestion transaction rolls back.

- [ ] **Step 2: Run failure**

```bash
pytest tests/processing/test_relevance.py -q
```

- [ ] **Step 3: Implement prompt + result validation**

Define one typed result and make the fail-open mapping explicit:

```python
@dataclass(frozen=True)
class RelevanceResult:
    status: Literal["relevant", "irrelevant", "uncertain", "needs_media"]
    confidence: float | None
    reason: str


async def decide_revision(self, revision, edition, policy) -> RelevanceResult:
    response = await self.provider.chat_completion(
        messages=[
            {"role": "system", "content": self._system_prompt(edition, policy)},
            {"role": "user", "content": self._source_payload(revision)},
        ],
        model=self.model,
        max_tokens=self.max_output_tokens,
        reasoning_effort=self.reasoning_effort,
        response_format={"type": "json_object"},
    )
    return RelevanceResult.from_dict(parse_json_object(response))


async def evaluate_relevance_task(context, source_item_revision_id, edition_id, policy_id):
    try:
        return await service.evaluate(source_item_revision_id, edition_id, policy_id)
    except ProviderUnavailableError:
        if context.job.attempts < 2:
            raise TransientProcessingError("relevance provider unavailable")
        return await service.persist_uncertain(
            source_item_revision_id, edition_id, policy_id, reason="provider_unavailable"
        )
```

Use strict JSON schema with `status`, `confidence`, `reason`. The prompt must say source role is context, not proof; a single resident observation can be relevant; absence of corroboration is not irrelevance. In the same change, extend Plan 2's `IngestionService`: for every newly-created revision, enumerate bound Editions, resolve/create each Edition's current relevance policy inside the same transaction, set `editions.current_relevance_policy_id`, and defer the exact-policy task on that connection:

```python
for revision_id in new_revision_ids:
    for edition_id in await self.repo.list_source_edition_ids(conn, source_id):
        policy = await relevance_policy_service.ensure_current(
            conn, edition_id=edition_id, config_hash=current_config_hash, prompt_version=RELEVANCE_PROMPT_VERSION
        )
        await evaluate_relevance.configure(connection=conn).defer_async(
            source_item_revision_id=revision_id,
            edition_id=edition_id,
            policy_id=policy.id,
        )
```

Backfill uses the same exact-policy task signature. Do not add a queueing lock as a correctness mechanism here; duplicate executions are acceptable and the immutable/canonical DB constraints remain authoritative.

Task 2 stops at the immutable relevance decision. Do not defer not-yet-implemented Claim/Vision tasks from this commit. Task 3 will wire media analysis and Task 4 will wire Claim extraction, including bounded backfill of already-created decisions. This keeps every intermediate commit runnable without unknown Procrastinate tasks.

- [ ] **Step 4: Run unit + transactional integration tests**

```bash
pytest tests/processing/test_relevance.py -q
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_relevance.py -q -m postgres
```

- [ ] **Step 5: Commit**

```bash
git add src/processing/relevance.py src/jobs/processing.py src/jobs/ingestion.py src/jobs/app.py src/ingestion/service.py tests/processing/test_relevance.py tests/ingestion/test_service.py
git commit -m "feat(knowledge): add edition relevance processing"
```


### Task 3: Add bounded Vision analysis for media-dependent relevance

**Files:**
- Create: `src/processing/vision.py`
- Modify: `src/jobs/processing.py`
- Test: `tests/processing/test_vision.py`

**Interfaces:**
- Vision mode: `off | relevance_only | full`; default `relevance_only`.
- Produces `VisionPolicyService.ensure_current(conn, edition_id: int) -> VisionPolicyVersion` and task `analyze_vision(source_item_revision_id: int, relevance_decision_id: int, policy_id: int)`.
- Produces immutable `VisionAnalysisRun` and `VisionObservation` artifacts linked to exact SourceAsset/SourceItemRevision.
- A successful relevance-only vision run can create a new child `EditionRelevanceDecision` with `parent_decision_id` pointing to the earlier `needs_media` decision.
- Task 3 modifies relevance completion to atomically defer Vision for `needs_media` decisions and, in `full`, for already-relevant revisions with eligible media. It also backfills pre-existing decisions that need Vision.
- In `full` mode, an already-relevant item runs bounded media enrichment before Claim extraction; Task 4 wires the later Claim handoff. If Vision is unavailable, Task 4 must fall open to source text instead of losing the item.

- [ ] **Step 1: Write failing policy tests**

Cases: strong text does not spend a vision call in `relevance_only`; media-only/"see photo" item does; `off` leaves item unresolved/needs-media and never marks it irrelevant; max asset/call/size limits stop work with explicit partial outcome. Add `full`-mode cases proving a relevant text+photo item waits for bounded Vision enrichment before Claim extraction, and a Vision timeout still defers Claim extraction from text-only evidence.

- [ ] **Step 2: Run failure**

```bash
pytest tests/processing/test_vision.py -q
```

- [ ] **Step 3: Implement derived observations**

Persist visible text, scene observations, location/date clues, and uncertainty language; never replace source text and never perform face identification. The follow-up relevance decision consumes source text + bounded vision observations.

When Task 3 is installed, extend relevance completion so media work is durably scheduled with a fixed policy version:

```python
if should_run_vision(decision, revision, mode=vision_mode):
    vision_policy = await vision_policy_service.ensure_current(
        conn, edition_id=decision.edition_id
    )
    await analyze_vision.configure(connection=conn).defer_async(
        source_item_revision_id=revision.id,
        relevance_decision_id=decision.id,
        policy_id=vision_policy.id,
    )
```

Backfill `needs_media` decisions and `full`-mode relevant decisions lacking a VisionAnalysisRun for the current policy. Use one explicit completion helper so both media-dependent relevance and `full` enrichment have the same fail-open boundary:

```python
async def finish_vision_processing(conn, run, *, decision, observations, error=None):
    await vision_repo.complete(conn, run, observations=observations, error=error)
    if decision.status == "needs_media" and observations:
        child = await relevance_service.decide_with_vision(conn, decision, observations)
        return VisionHandoff(
            relevance_decision_id=child.id,
            vision_run_id=run.id,
            ready_for_claims=child.status == "relevant",
        )
    return VisionHandoff(
        relevance_decision_id=decision.id,
        vision_run_id=run.id if observations else None,
        ready_for_claims=decision.status == "relevant",
    )
```

On provider timeout/error in `full`, complete the Vision run with an unavailable/failed derived status and return a handoff that is still `ready_for_claims=True` for a text-supported relevant decision. Task 4 consumes this handoff and defers Claim extraction. Do not retry Vision indefinitely ahead of a text-supported Claim.

- [ ] **Step 4: Run tests**

```bash
pytest tests/processing/test_vision.py tests/processing/test_relevance.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/processing/vision.py src/jobs/processing.py tests/processing/test_vision.py
git commit -m "feat(knowledge): add bounded media relevance analysis"
```

### Task 4: Implement Claim extraction and semantic relation model

**Files:**
- Create: `src/processing/claims.py`
- Modify: `src/processing/relevance.py`
- Modify: `src/processing/vision.py`
- Modify: `src/jobs/processing.py`
- Test: `tests/processing/test_claims.py`

**Interfaces:**
- Produces `ClaimExtractionPolicyService.ensure_current(conn, edition_id: int) -> ClaimExtractionPolicyVersion`.
- Produces `ClaimExtractionContextBuilder.build(revision_id: int) -> ClaimExtractionContext`, containing the exact SourceItemRevision plus bounded parent/root conversation context when the item is a reply/comment. The target revision remains the provenance source; context exists only to resolve ellipsis/deictic language such as `тоже`, `ещё нет`, `там`, or `с восьми`.
- Produces `ClaimExtractionService.extract(revision_id, edition_id, relevance_decision_id, policy_id, vision_run_id: int | None = None) -> ClaimExtractionResult`.
- Task 4 wires the handoff from relevant decisions/Vision into `extract_claims(source_item_revision_id, edition_id, policy_id=exact_id)` and adds bounded backfill for relevant decisions that do not yet have a ClaimExtractionRun.
- Claim fields include `assertion_text` plus a semantically self-contained `normalized_assertion`, event-time interval/precision/confidence, immutable place/entity mentions (resolved later), and provenance through extraction run.
- `normalized_assertion` is the canonical embedding/query text in Task 5. It is a semantic proposition, not a fixed-size text chunk. It may be one short sentence or several sentences when needed to preserve attribution/meaning; there is no minimum/maximum sentence-count rule.

- [ ] **Step 1: Write failing extraction tests**

Feed a source revision such as `"На АКЗ возле почты вода уже появилась"` and expect one Claim with attribution/provenance and an unresolved place mention text `"АКЗ"` or `"на АКЗ"`. Feed a post containing two independent assertions and expect two Claims because the semantic assertions differ, **not** because the post has two sentences.

Add context-dependence regressions. Given parent/root context `"На АКЗ воду уже дали?"` and a reply revision `"Всё ещё нет."`, the extracted Claim must preserve provenance to the reply but produce a self-contained `normalized_assertion`, for example `"По сообщению автора, водоснабжение на АКЗ на момент комментария ещё не восстановилось."`. Assert the extractor never emits only `"Всё ещё нет"` as `normalized_assertion`. Conversely, `"Пожар у вокзала."` is valid despite being only three words: do not introduce minimum-word or minimum-sentence heuristics.

Add a long-post regression: a seven-sentence SourceItemRevision containing exactly two independent factual propositions produces two Claims, and later embedding code must therefore make two Claim embedding calls, not seven sentence-chunk calls.

Test correction behavior separately:

```python
new_claim = await service.record_correction(old_claim_id, corrected_assertion, relation="CORRECTS")
assert old_claim.assertion_text != new_claim.assertion_text
```

- [ ] **Step 2: Run failure**

```bash
pytest tests/processing/test_claims.py -q
```

- [ ] **Step 3: Implement extraction with atomic visibility**

First wire the upstream handoff. For a relevant decision that does not require pending `full` Vision, or for a completed `VisionHandoff(ready_for_claims=True)`, resolve/create the current extraction policy inside the same transaction and defer the exact-policy job:

```python
policy = await claim_policy_service.ensure_current(conn, edition_id=decision.edition_id)
await extract_claims.configure(connection=conn).defer_async(
    revision_id=decision.source_item_revision_id,
    edition_id=decision.edition_id,
    relevance_decision_id=decision.id,
    policy_id=policy.id,
    vision_run_id=vision_run_id,
)
```

Backfill scans relevant decisions without any extraction run for the current policy and routes them through the same Vision-mode handoff rules. Then keep the semantic run stable across technical attempts:

```python
run = await claim_repo.get_or_create_run(
    revision_id=revision_id,
    edition_id=edition_id,
    relevance_decision_id=relevance_decision_id,
    extraction_policy_id=policy_id,
)
attempt = await claim_repo.start_attempt(run.id, provider=self.provider.name, model=self.model)
try:
    extracted = await self._call_model(run, vision_run_id=vision_run_id)
except Exception as exc:
    await claim_repo.finish_attempt(attempt.id, status="failed", error_kind=classify_error(exc))
    raise

async with self.uow.transaction() as conn:
    claims = await claim_repo.insert_claims(conn, run_id=run.id, extracted=extracted)
    await claim_repo.mark_run_succeeded(conn, run.id)
return claims
```

Provider call occurs outside the final DB apply transaction. Build the bounded `ClaimExtractionContext` before the call and instruct the model that each returned `normalized_assertion` must stand on its own without surrounding-message context; parent/root text may clarify the target revision but must never become fake provenance for the Claim. Persist/start run and `processing_attempts` row, call provider, close the attempt row with success/error, validate result, then in one transaction insert immutable Claims + mentions and mark the run succeeded. Do **not** queue an embedding task in Task 4 because that task is introduced in Task 5; Task 5 adds the atomic post-success embedding handoff plus a backfill for Claims created during this intermediate boundary. Provider retries keep the same semantic run ID and increment only `processing_attempts.attempt_no`; this table is audit history, never a queue.

- [ ] **Step 4: Test duplicate at-least-once execution**

Call the handler twice with the same semantic key and assert the second invocation returns/observes the existing successful canonical run rather than inserting duplicate Claims.

- [ ] **Step 5: Commit**

```bash
git add src/processing/claims.py src/processing/relevance.py src/processing/vision.py src/jobs/processing.py tests/processing/test_claims.py
git commit -m "feat(knowledge): extract immutable source-bound claims"
```

### Task 5: Add semantic Claim/StoryRevision embeddings and exact pgvector repository

**Files:**
- Create: `migrations/0006_embeddings_stories.sql`
- Create: `src/embedding_providers.py`
- Create: `src/processing/embeddings.py`
- Create: `src/repositories/embeddings.py`
- Modify: `src/processing/claims.py`
- Modify: `src/jobs/processing.py`
- Modify: `src/config_loader.py`
- Modify: `config.yaml.example`
- Modify/Test: `tests/test_config_loader.py`
- Test: `tests/processing/test_embeddings.py`

**Interfaces:**
- Produces `EmbeddingConfig(provider: str = "google", model: str = "gemini-embedding-2", dimensions: int = 1536, timeout: int = 45)`; model/dimensions are explicit config and are copied into each embedding job so retries keep the same vector space.
- Produces `EmbeddingInputBuilder.for_claim(claim: Claim) -> str`, returning the complete `claim.normalized_assertion`; it never reads/splits raw SourceItemRevision text and never creates token/sentence chunks.
- Produces `EmbeddingInputBuilder.for_story_revision(revision: StoryRevision) -> str`, returning the complete compact `revision.semantic_text`; StoryRevision construction is responsible for keeping this representation focused on current story meaning rather than concatenating the entire source history.
- Produces `EmbeddingProvider.embed(text: str, *, purpose: Literal["claim_query", "story_document"], model: str, dimensions: int) -> list[float]`; initial Google implementation uses the configured Gemini API key and validates exact output dimensions.
- Produces tasks `embed_claim(claim_id: int, model: str, dimensions: int)` and `embed_story_revision(story_revision_id: int, model: str, dimensions: int)`.
- Produces exactly one immutable embedding row per `(semantic object, model, dimensions, purpose, content_hash)`; no `embedding_chunks` table exists.
- Produces immutable `claim_embeddings` and `story_revision_embeddings`.
- `EmbeddingRepository.find_story_candidates(conn, *, edition_id: int, query_embedding: list[float], model: str, dimensions: int, limit: int, event_time_start: datetime | None = None, event_time_end: datetime | None = None) -> list[StoryVectorCandidate]` returns vector candidates only; Task 7 unions this stream with independent non-vector recall signals before the LLM matcher sees candidates.

- [ ] **Step 1: Write failing embedding persistence/retrieval tests**

Assert embedding config validates `128 <= dimensions <= 3072`, defaults to Google `gemini-embedding-2` at 1536 dimensions, and never logs the API key. Mock the provider and assert `embed_claim()` receives the **entire self-contained `Claim.normalized_assertion` exactly once** with `purpose="claim_query"`; it must not receive the raw SourceItemRevision, individual sentences, or sliding chunks. Assert a seven-sentence source that produced two Claims results in exactly two Claim embedding calls. Assert a short complete Claim such as `"Пожар у вокзала."` is embedded unchanged and is not rejected for length. Assert a context-dependent raw reply such as `"Всё ещё нет."` is never used as embedding input when the Claim's normalized assertion is `"Водоснабжение на АКЗ ещё не восстановилось."`.

Mock StoryRevision embedding and assert the complete `semantic_text` is sent once with `purpose="story_document"`; there is no sentence/token chunk loop. A returned vector with the wrong length is rejected. Assert same content hash/model/purpose reuses an existing vector; new model/dimensions create a new row. Insert known vectors and assert exact cosine order using pgvector `<=>`.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_embeddings.py -q
```

- [ ] **Step 3: Implement schema and service**

Use a narrow provider abstraction; the initial Google adapter calls the Gemini embedding endpoint with the configured model/output dimensionality and maps Claim vs StoryRevision to retrieval-query vs retrieval-document purpose. `EmbeddingInputBuilder` is the only owner of embedding text: Claim input is the complete `normalized_assertion`, Story input is the complete compact `semantic_text`. **Do not add fixed token-, character-, or sentence-count chunking, sliding windows, or per-sentence embedding rows.** If a future provider rejects an unexpectedly oversized semantic object, classify it as an embedding-input error and fix the upstream semantic representation/policy; do not silently split one Claim into unrelated fragments at the embedding layer. Reject any response whose vector length differs from the queued `dimensions`. The task arguments freeze `model` and `dimensions`; changing config schedules a new backfill rather than mutating old rows.

```python
class EmbeddingProvider(Protocol):
    async def embed(
        self, text: str, *, purpose: Literal["claim_query", "story_document"],
        model: str, dimensions: int,
    ) -> list[float]: ...

@procrastinate_app.task(queue="processing")
async def embed_claim(claim_id: int, model: str, dimensions: int) -> None:
    await embedding_service.embed_claim(claim_id, model=model, dimensions=dimensions)

@procrastinate_app.task(queue="processing")
async def embed_story_revision(story_revision_id: int, model: str, dimensions: int) -> None:
    await embedding_service.embed_story_revision(
        story_revision_id, model=model, dimensions=dimensions
    )
```

Persist and retrieve exact vectors with model/dimension filters:

```python
async def find_story_candidates(
    self, conn, *, edition_id, query_embedding, model, dimensions, limit,
    event_time_start=None, event_time_end=None,
):
    cur = await conn.execute(
        """
        SELECT s.id AS story_id, sr.id AS story_revision_id, sre.id AS embedding_id,
               sre.embedding <=> %s AS vector_distance
        FROM stories s
        JOIN story_revisions sr ON sr.id = s.current_revision_id
        JOIN story_revision_embeddings sre ON sre.story_revision_id = sr.id
        WHERE s.edition_id = %s AND sre.model = %s AND sre.dimensions = %s
          AND s.lifecycle_state IN ('active', 'reopened', 'resolved')
        ORDER BY sre.embedding <=> %s
        LIMIT %s
        """,
        (query_embedding, edition_id, model, dimensions, query_embedding, limit),
    )
    return [StoryVectorCandidate.from_row(row) async for row in cur]
```

Use pgvector `vector` float32. No ANN index. Store `model`, `dimensions`, `purpose`, `content_hash`, `created_at`. Do not store artificial chunk ordinals because semantic objects are embedded whole. Retrieval SQL filters edition/lifecycle/broad time/model/dimensions before `ORDER BY embedding <=> %s LIMIT %s`. In this task, modify ClaimExtraction successful apply so Claims + `run=succeeded` + one `embed_claim(claim_id, model, dimensions)` defer per newly-created Claim commit atomically on the same connection. The model/dimensions come from the embedding config and are copied into task arguments. Provide bounded backfill commands for Claims/StoryRevisions missing that exact model/dimension vector, covering Claims created while Task 4 existed without Task 5; model changes never update old embedding rows.

- [ ] **Step 4: Verify exact retrieval**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_embeddings.py -q
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0006_embeddings_stories.sql src/embedding_providers.py src/processing/embeddings.py src/processing/claims.py src/repositories/embeddings.py src/jobs/processing.py src/config_loader.py config.yaml.example tests/test_config_loader.py tests/processing/test_embeddings.py
git commit -m "feat(knowledge): add exact claim and story embeddings"
```

### Task 6: Add persistent Story lifecycle and meaningful revisions

**Files:**
- Create: `src/domain/stories.py`
- Create: `src/repositories/stories.py`
- Test: `tests/processing/test_story_matching.py`

**Interfaces:**
- Tables also include `story_state_events` and immutable `story_relations` with `created_at`; relation invalidation, if needed, is represented by a later state event rather than deleting history.
- Produces `StoryRepository.create_story_with_revision(conn, *, edition_id: int, claim_id: int, revision: NewStoryRevision) -> StoryWithRevision`.
- Produces `StoryRepository.attach_claim(conn, *, story_id: int, claim_id: int, attached_at: datetime) -> None`.
- Produces `StoryRepository.create_revision_if_semantic_change(conn, *, story_id: int, semantic_changed: bool, revision: NewStoryRevision | None) -> StoryRevision | None`.
- Story lifecycle: `candidate | active | resolved | reopened | archived`; reopening a resolved Story is an explicit state event, and `reopened` remains retrieval-eligible.

- [ ] **Step 1: Write failing lifecycle/revision tests**

Assert current revision FK cannot point to another Story. Implement the DB invariant with `UNIQUE(story_id, id)` on `story_revisions` plus a deferrable composite FK `(stories.id, stories.current_revision_id) -> story_revisions(story_id, id)`. Assert repeated semantically unchanged state does not require a revision when service indicates `semantic_changed=False`; restoration creates a new revision and state event.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_story_matching.py -q -k story_repository
```

- [ ] **Step 3: Implement repository transaction primitives**

Keep Story creation/revision writes explicit:

```python
async def create_story_with_revision(self, conn, *, edition_id, claim_id, revision):
    story_id = await self._insert_story_shell(conn, edition_id=edition_id)
    story_revision = await self._insert_revision(conn, story_id=story_id, revision=revision)
    await conn.execute(
        "UPDATE stories SET current_revision_id=%s, lifecycle_state='active' WHERE id=%s",
        (story_revision.id, story_id),
    )
    await self.attach_claim(conn, story_id=story_id, claim_id=claim_id, attached_at=revision.created_at)
    return StoryWithRevision(story_id=story_id, revision=story_revision)
```

Do not put AI semantic-change judgment in repository. Repository accepts an explicit revision payload when application service decided a meaningful change exists.

- [ ] **Step 4: Run tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_story_matching.py -q -k story_repository
```

- [ ] **Step 5: Commit**

```bash
git add src/domain/stories.py src/repositories/stories.py tests/processing/test_story_matching.py
git commit -m "feat(knowledge): add persistent story revisions"
```

### Task 7: Implement two-stage Story Matching with saved candidates and stale protection

**Files:**
- Create: `src/processing/story_matching.py`
- Create: `src/repositories/story_candidates.py`
- Modify: `src/processing/embeddings.py`
- Modify: `src/jobs/processing.py`
- Test: `tests/processing/test_story_matching.py`

**Interfaces:**
- Migration/schema includes `story_matching_policy_versions`, `story_matching_runs`, `story_matching_candidates`, `story_match_decisions`, `story_relation_proposals`; every candidate freezes the exact `story_revision_id` the matcher read. `story_revision_embedding_id` is nullable so lexical/state/location/entity recall can still propose a current revision before its embedding job completes. `StoryMatchingPolicyVersion` owns retrieval/matcher semantics such as per-signal limits, total candidate cap, broad resolved-story lookback, embedding model/dimensions/purpose, and matcher prompt/config hash.
- Produces `StoryCandidateRetriever.retrieve(conn, *, claim: Claim, claim_embedding: ClaimEmbedding, policy: StoryMatchingPolicyVersion) -> list[StoryCandidate]`, which creates a bounded **union** of independent recall streams: vector top-K, lexical full-text similarity over StoryRevision title/summary/semantic text, and recent active/reopened/resolved-state candidates. Task 8 extends the same retriever with resolved Place hierarchy and normalized Entity overlap.
- `StoryCandidate` contains `story_id: int`, `story_revision_id: int`, `story_revision_embedding_id: int | None`, `retrieval_reasons: frozenset[str]`, `vector_distance: float | None`, `lexical_score: float | None`, `location_overlap: float | None`, `entity_overlap: float | None`, `time_score: float | None`, and `status_score: float | None`; every numeric signal is optional metadata, while revision identity is mandatory.
- Each saved candidate stores retrieval provenance/signals such as `retrieved_by_vector`, `retrieved_by_lexical`, `retrieved_by_state`, later `retrieved_by_place`/`retrieved_by_entity`, plus vector distance and other soft scores when available. No signal is a hard admission threshold.
- Produces `StoryMatchingRun`, frozen candidates, `StoryMatchDecision`, separate relation proposals.
- Procrastinate task signature is `match_claim(claim_id: int, policy_id: int, claim_embedding_id: int)`; a stale rerun keeps the same immutable ClaimEmbedding unless a new policy explicitly selects another embedding model.
- Assignment enum is only `SAME_STORY | NEW_STORY`.
- Procrastinate execution lock: `story-matching-edition:<edition_id>`.
- Claim embedding completion resolves/creates the current StoryMatchingPolicyVersion and defers matching with the exact `policy_id` in the same transaction that makes the ClaimEmbedding visible; retries never silently switch policy version.
- Produces bounded `backfill_story_matching(edition_id: int, policy_id: int, after_claim_embedding_id: int | None)` for compatible ClaimEmbeddings that existed before Task 7 and have no successful/stale-active matching run for that policy.
- Matcher input contains the complete `Claim.normalized_assertion` plus each candidate's exact StoryRevision `title`, `summary`, `current_state`, and `semantic_text`. Retrieval scores/reasons are metadata only; the LLM never receives an isolated matched word as the story meaning.
- Matcher output keeps assignment limited to `SAME_STORY | NEW_STORY` but carries a separate optional `story_update` payload: `semantic_changed: bool` plus proposed title/summary/current_state/semantic_text when a SAME_STORY Claim materially changes Story state. NEW_STORY always creates revision #1.

- [ ] **Step 1: Write failing matching tests**

Cover:

1. hybrid candidate retrieval is frozen before the AI call and saves which retrieval signals produced each candidate;
2. a semantically matching Story returned by vector retrieval is retained even when lexical overlap is weak;
3. a matching Story returned by lexical/state fallback is retained even when it is outside vector top-K or has no completed StoryRevisionEmbedding yet;
4. the matcher prompt contains the full `Claim.normalized_assertion` and full candidate StoryRevision semantic text, not an isolated token/lexical hit;
5. no vector-distance or lexical-score threshold can directly return `irrelevant`, drop the Claim, or decide SAME_STORY; an empty candidate set is a valid input and leads the matcher/domain flow toward `NEW_STORY`;
6. AI selects SAME_STORY + independent `CONSEQUENCE_OF` relation;
7. target revision changes before apply -> run marked `stale`, no claim attachment;
8. NEW_STORY creates Story + revision #1 + story_claim + accepted relations atomically and defers `embed_story_revision` for revision #1 on the same connection;
9. SAME_STORY with `semantic_changed=false` attaches the Claim without a new StoryRevision; with `semantic_changed=true` it creates the proposed revision and atomically defers its embedding;
10. a ClaimEmbedding persisted before Task 7 is picked up by bounded matching backfill exactly once for the selected policy;
11. duplicate task execution cannot create two successful runs for `(claim_id, policy_id)`.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_story_matching.py -q
```

- [ ] **Step 3: Implement retrieval -> LLM -> atomic apply**

When this task lands, extend `EmbeddingService.embed_claim()`: after inserting/reusing the compatible ClaimEmbedding, resolve/create the current StoryMatchingPolicyVersion for the Claim's Edition and defer `match_claim(claim_id, policy_id, claim_embedding_id)` on the same connection. Run `backfill_story_matching` for existing compatible ClaimEmbeddings before relying only on live handoff.

Build candidates through `StoryCandidateRetriever` as a union, not a single vector query. Start with versioned per-signal limits such as `vector_limit=20`, `lexical_limit=10`, `state_fallback_limit=20`, `total_candidate_limit=40`; these are bounded context/resource controls, **not semantic thresholds**. Deduplicate by Story ID, prefer the exact current StoryRevision, retain all retrieval reasons, and rank deterministically for prompt order. Active/reopened Stories are allowed in the state-fallback pool regardless of creation age; recently resolved Stories use the policy's broad last-activity window. If vector retrieval returns nothing or misses a Story, lexical/state candidates still reach the matcher. If every stream returns nothing, persist an empty candidate set and let the matcher/application create `NEW_STORY`; never drop the Claim.

Use a three-boundary service: persist candidates, call matcher outside transaction, then validate/apply:

```python
run = await self._create_run_and_candidates(claim_id, policy_id)
decision = await self.matcher.choose(run.claim, run.candidates)

async with self.uow.transaction() as conn:
    locked = await story_repo.lock_matching_run(conn, run.id)
    if decision.assignment == "SAME_STORY":
        candidate = locked.candidate_for(decision.target_story_id)
        current_revision_id = await story_repo.current_revision_id(conn, decision.target_story_id)
        if candidate.story_revision_id != current_revision_id:
            await story_repo.mark_matching_run_stale(conn, run.id)
            await match_claim.configure(connection=conn).defer_async(
                claim_id=claim_id,
                policy_id=policy_id,
                claim_embedding_id=run.claim_embedding_id,
            )
            return
    created_revision = await self._apply_decision(conn, locked, decision)
    if created_revision is not None:
        await embed_story_revision.configure(connection=conn).defer_async(
            story_revision_id=created_revision.id,
            model=self.embedding_config.model,
            dimensions=self.embedding_config.dimensions,
        )
    await story_repo.mark_matching_run_succeeded(conn, run.id)
```

Never hold the LLM call in a transaction. Save candidate `story_revision_id`, nullable `story_revision_embedding_id`, and retrieval-reason flags/scores. The matcher receives the complete semantic texts for the Claim and candidates; vector distance, lexical rank, and later place/entity overlap are hints, not substitutes for text. On SAME_STORY apply, lock/read the Story row and require `current_revision_id == selected_candidate.story_revision_id`; otherwise mark stale and defer a fresh matching task after commit.

For `SAME_STORY`, `_apply_decision()` attaches the Claim and creates a new StoryRevision only when the separately returned `story_update.semantic_changed` is true; validate the proposed revision against source Claims and current Story state before insert. For `NEW_STORY`, it always returns revision #1. Any returned new revision is embedded by the same transaction as the Story apply, as shown above.

Candidate retrieval must never age active/reopened Stories out merely because `created_at` is old. Active/reopened Stories are eligible regardless of age; resolved Stories use a broad configurable last-activity lookback from the versioned matching policy. Vector, lexical, location/entity/time/status signals remain metadata. Do not introduce a vector/lexical threshold that directly decides assignment or prevents `NEW_STORY`.

- [ ] **Step 4: Run race/stale tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_story_matching.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/processing/story_matching.py src/repositories/story_candidates.py src/processing/embeddings.py src/jobs/processing.py tests/processing/test_story_matching.py
git commit -m "feat(knowledge): match claims into persistent stories"
```

### Task 8: Add Place mentions, versioned resolution, and lightweight entities

**Files:**
- Create: `migrations/0007_places_evidence.sql`
- Create: `src/domain/places.py`
- Create: `src/repositories/places.py`
- Create: `src/processing/places.py`
- Modify: `src/processing/claims.py`
- Modify: `src/processing/embeddings.py`
- Modify: `src/processing/story_matching.py`
- Modify: `src/repositories/story_candidates.py`
- Modify: `src/jobs/processing.py`
- Test: `tests/processing/test_places.py`
- Modify/Test: `tests/processing/test_story_matching.py`

**Interfaces:**
- Tables: `places`, `place_aliases`, `claim_place_mentions`, `place_resolution_policy_versions`, `place_resolution_runs`, `place_resolution_results`, `claim_entities`; migration also adds nullable edition-current place-policy pointer with edition-consistent FK.
- Produces `PlaceResolutionPolicyService.ensure_current(conn, edition_id: int) -> PlaceResolutionPolicyVersion` and task `resolve_place_mention(mention_id: int, policy_id: int)`.
- Produces `StoryMatchingPrerequisiteService.maybe_schedule(conn, claim_id: int) -> bool`; after this task lands, matching is queued only when a compatible ClaimEmbedding exists and every ClaimPlaceMention has a completed result (resolved **or unresolved**) for the current place-resolution policy.
- Extends `StoryCandidateRetriever` with two additional independent candidate streams: Place overlap (including ancestor/descendant `WITHIN` relations) and normalized Entity overlap. These streams add recall but never force SAME_STORY.
- `normalized_alias` is indexed, not globally unique.
- Resolution result may have `place_id NULL`; `NULL` is a completed resolution outcome and never blocks matching/publication.

- [ ] **Step 1: Write failing tests**

Assert two Places may both have alias `"Центр"`; unresolved result remains valid; later policy/run can resolve the same immutable mention; hierarchy gives positive location-overlap signal for street-within-district. Assert a Story missed by vector/lexical top-K can still enter the frozen candidate set through Place overlap, and that same/parent Place alone never forces SAME_STORY. Assert `maybe_schedule()` does not queue matching while a place mention lacks a current-policy result, does queue once that mention has an explicit unresolved (`place_id=NULL`) result, and still queues immediately for Claims with no place mentions. Backfill old mentions and assert at-least-once execution does not duplicate canonical results.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_places.py tests/processing/test_story_matching.py -q
```

- [ ] **Step 3: Implement resolver adapter**

Resolve mentions without mutating Claim rows, and treat unresolved as a successful semantic outcome:

```python
async def resolve_mention(self, mention_id: int, policy_id: int):
    mention = await self.place_repo.get_mention(mention_id)
    candidates = await self.place_repo.alias_candidates(
        normalized_alias=normalize_place_text(mention.original_text)
    )
    choice = await self.resolver.choose(mention, candidates, policy_id=policy_id)
    async with self.uow.transaction() as conn:
        result = await self.place_repo.insert_resolution_result(
            conn,
            mention_id=mention.id,
            policy_id=policy_id,
            place_id=choice.place_id,  # None is valid unresolved
            confidence=choice.confidence,
            reason=choice.reason,
        )
        await matching_prerequisites.maybe_schedule(conn, claim_id=mention.claim_id)
    return result
```

When Task 8 lands, modify ClaimExtraction successful apply to resolve/create the current PlaceResolutionPolicyVersion and defer one `resolve_place_mention(mention_id, policy_id)` per newly-created place mention on the same transaction as Claim visibility; Claims with zero place mentions immediately satisfy the place side of the barrier. Modify Claim embedding completion to call `matching_prerequisites.maybe_schedule()` instead of directly deferring Story Matching. The prerequisite service freezes the current place-policy and compatible ClaimEmbedding, then resolves/creates the exact StoryMatchingPolicyVersion and defers `match_claim` once. Run bounded backfill for old ClaimPlaceMentions lacking current-policy results and for already-embedded Claims that become ready.

Seed/bridge known Berdyansk aliases from existing `data/city_profiles/berdyansk.yaml` through an importer/adapter; do not make the YAML runtime source of truth after Places are persisted. Resolver candidate scoring may use edition context, other mentions, coordinates, and source text. Preserve original mention text always. Extend `StoryCandidateRetriever` so resolved Claim Places and lightweight normalized Entities add bounded candidates to the same frozen union used by Task 7; deduplicate by Story ID and preserve `retrieved_by_place` / `retrieved_by_entity` provenance.

- [ ] **Step 4: Run tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_places.py tests/processing/test_story_matching.py -q
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0007_places_evidence.sql src/domain/places.py src/repositories/places.py src/processing/places.py src/processing/claims.py src/processing/embeddings.py src/processing/story_matching.py src/jobs/processing.py tests/processing/test_places.py tests/processing/test_story_matching.py
git commit -m "feat(knowledge): add versioned place resolution"
```

### Task 9: Add immutable EvidenceAssessment and optional lightweight verification

**Files:**
- Create: `src/domain/evidence.py`
- Create: `src/repositories/evidence.py`
- Create: `src/processing/evidence.py`
- Create: `src/processing/verification.py`
- Modify: `src/processing/story_matching.py`
- Modify: `src/jobs/processing.py`
- Test: `tests/processing/test_evidence.py`

**Interfaces:**
- `EvidenceAssessmentRun` stores Story ID + current StoryRevision ID, exact included Claim IDs, an `input_hash` over the sorted Claim IDs, policy/model metadata, started/completed/status. Canonical success is unique for `(story_revision_id, policy_id, input_hash)`, allowing a new assessment when evidence changes without requiring a semantic StoryRevision.
- Produces `EvidencePolicyService.ensure_current(conn, edition_id: int) -> EvidenceAssessmentPolicyVersion`; Story Matching completion is extended in this task to defer `assess_evidence(story_id, story_revision_id, policy_id)` after each successful Claim attachment, even when no new StoryRevision was needed.
- Produces bounded evidence backfill over active/reopened/recently-resolved Stories whose current `(story_revision, claim-set input_hash, policy)` lacks a successful assessment.
- `EvidenceCluster` is immutable and may point to `supersedes_cluster_id`.
- Member stance: `SUPPORTS | CONTRADICTS | UNCERTAIN | CONTEXTUAL`.
- Optional verification states: `reported | corroborated | officially_supported | disputed | retracted`; optional `risk_level: low | medium | high` is a separate advisory dimension, not a truth score or automatic publication permission.
- `VerificationAssessment` MUST NOT contain `publication_blocking`, `eligible`, `allowed`, `publishable`, or any equivalent admission-gate field. It is descriptive evidence metadata only.
- Produces `VerificationService.assess(run: EvidenceAssessmentRun, clusters: list[EvidenceCluster]) -> list[VerificationAssessment]`; failure returns advisory-unavailable state to orchestration and never marks a Story ineligible.

- [ ] **Step 1: Write failing evidence tests**

Three semantically similar Claims remain three Claims but cluster into one proposition. A contradictory Claim joins with `CONTRADICTS`. A new Claim attached to the same semantic StoryRevision changes `input_hash` and causes a new assessment run/cluster, leaving the old cluster intact. Re-executing with the identical Claim set/policy reuses the canonical successful run.

Test explicitly:

```python
assessments = await verification_service.assess(
    run=single_claim_evidence_run,
    clusters=[single_claim_cluster],
)
assert assessments[0].state == "reported"
assert assessments[0].risk_level == "low"
assert not hasattr(assessments[0], "publication_blocking")
```

- [ ] **Step 2: Run failure**

```bash
pytest tests/processing/test_evidence.py -q
```

- [ ] **Step 3: Implement evidence and optional verification**

Freeze exact input Claims before correlation:

```python
snapshot_time = datetime.now(timezone.utc)
claim_ids = await story_repo.claim_ids_attached_by(
    story_id, as_of=snapshot_time
)
input_hash = hash_sorted_ids(claim_ids)
run = await evidence_repo.get_or_start_run(
    story_id=story_id,
    story_revision_id=story_revision_id,
    policy_id=policy_id,
    input_hash=input_hash,
    started_at=snapshot_time,
)
claims = await claim_repo.get_many(claim_ids)
await evidence_repo.freeze_run_claims(run.id, claim_ids)
clusters = await correlator.cluster(claims)
async with self.uow.transaction() as conn:
    stored = await evidence_repo.insert_clusters(conn, run_id=run.id, clusters=clusters)
    await evidence_repo.mark_run_succeeded(conn, run.id)
    await maybe_verify_evidence.configure(connection=conn).defer_async(
        evidence_assessment_run_id=run.id
    )
```

After the evidence service exists, extend Story Matching's successful apply transaction:

```python
evidence_policy = await evidence_policy_service.ensure_current(
    conn, edition_id=applied.edition_id
)
await assess_evidence.configure(connection=conn).defer_async(
    story_id=applied.story_id,
    story_revision_id=applied.story_revision_id,
    policy_id=evidence_policy.id,
)
```

`maybe_verify_evidence` catches provider availability failures, records an advisory unavailable outcome, and returns success to orchestration; it never changes Story eligibility by itself.

Migration/schema includes `evidence_assessment_policy_versions`, `evidence_assessment_runs`, `evidence_assessment_run_claims`, `evidence_clusters`, `evidence_cluster_members`, plus optional `verification_policy_versions`/`verification_assessments`. Add a partial/canonical uniqueness rule for successful `(story_revision_id, policy_id, input_hash)`. Evidence assessment may use AI correlation but must persist the exact input Claim IDs. Derived fields include `supporting_claims`, `contradicting_claims`, `unique_sources`, `estimated_independent_source_groups`; none become eligibility constraints.

Run bounded backfill immediately after deployment so Stories matched before Task 9 gain evidence assessments for their current exact Claim sets. Verification failure is terminal-advisory for that run and must not mark Story unusable. Do not add a numeric global trust score. Do not expose any verification-derived boolean that downstream eligibility code can treat as publication permission.

- [ ] **Step 4: Run tests**

```bash
pytest tests/processing/test_evidence.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/domain/evidence.py src/repositories/evidence.py src/processing/evidence.py src/processing/verification.py src/processing/story_matching.py src/jobs/processing.py tests/processing/test_evidence.py
git commit -m "feat(knowledge): add non-gating evidence assessments"
```

### Task 10: Add end-to-end source-revision -> Story integration fixture

**Files:**
- Create: `tests/fixtures/multisource_knowledge_day.json`
- Create: `tests/processing/test_knowledge_pipeline.py`

**Interfaces:**
- Exercises the real repositories and service boundaries; AI calls use deterministic fakes.

- [ ] **Step 1: Create fixture**

Include official/news/community Telegram records, one single-source useful resident report, duplicate reposts, contradiction, correction, multiple districts, and one high-risk accusation. Also include:

- a parent/root message `"На АКЗ воду уже дали?"` plus reply `"Всё ещё нет."`;
- a seven-sentence source post containing exactly two independent factual propositions;
- an old/current Story whose wording is semantically related to a new Claim but deliberately constructed so the test fake places it outside vector top-K while lexical/state or later Place retrieval still returns it.

- [ ] **Step 2: Write end-to-end assertions**

Assert the single-source useful report creates an active/candidate Story; reposts are separate Claims but grouped as evidence; correction uses Claim relation; high-risk material is retained as provenance but not promoted to established fact. Assert the reply `"Всё ещё нет."` becomes a self-contained normalized Claim before embedding, the embedding fake receives that complete normalized assertion exactly once, and neither raw reply fragments nor per-sentence chunks are embedded. Assert the seven-sentence source produces exactly two Claim embeddings. Assert the vector-missed Story still reaches the frozen matcher candidate set through a non-vector retrieval reason, and verify the matcher reads the complete Claim and StoryRevision texts before deciding SAME_STORY/NEW_STORY.

- [ ] **Step 3: Run failure, then wire missing orchestration edges**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing/test_knowledge_pipeline.py -q
```

Only implement missing job defers/wiring; do not weaken assertions.

- [ ] **Step 4: Run full Plan 3 suite**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/processing -q
pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/multisource_knowledge_day.json tests/processing/test_knowledge_pipeline.py src/jobs/processing.py
git commit -m "test(knowledge): cover persistent story pipeline"
```

## Plan 3 completion gate

A single concrete community source must successfully traverse:

```text
SourceItemRevision -> relevant -> self-contained Claim -> whole-Claim embedding -> hybrid candidate retrieval -> Story -> EvidenceCluster(reported)
```

without any second-source or official-confirmation requirement. The completion suite must prove that semantic chunking happens at Claim extraction rather than the embedding layer: the reply-context fixture (`parent: На АКЗ воду уже дали?`, `reply: Всё ещё нет`) is embedded as one resolved self-contained Claim, and a multi-sentence source is embedded once per semantic Claim rather than once per sentence. It must also prove hybrid recall: a Story missed by vector top-K can still reach the LLM matcher through lexical/state/Place/entity retrieval, while no retrieval signal can force SAME_STORY or suppress `NEW_STORY`. Verification may be missing, unavailable, or merely `reported`; none of those states may prevent the Story from reaching publication eligibility. Only the separate final light publication audit may block unresolved dangerous/high-risk misrepresentation. `src/jobs/app.py` must include `src.jobs.processing` in Procrastinate `import_paths`, and the worker must consume the `processing` queue.
