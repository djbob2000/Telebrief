# Facebook Provider and Legacy Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe persistent Facebook post/comment ingestion to the shared core, migrate legacy Telegram rows, and remove the old direct/provider-specific storage and publication path.

**Architecture:** Facebook uses explicit configured sources, separate `FacebookSourceConfig` and `FacebookAuthProfile`, persistent Playwright profiles referenced by logical `storage_ref`, Procrastinate execution locks per auth profile, and completeness-oriented comment/reply enrichment. Browser automation stops on checkpoint/CAPTCHA/account-action states. After validation, a one-time importer moves legacy Telegram `messages` into normalized SourceItems and obsolete storage/direct collection code is retired.

**Tech Stack:** Playwright Python, PostgreSQL 18, Psycopg 3 async, Procrastinate, existing ingestion/knowledge/publication stack.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- Explicit Facebook Source registry is primary; Home Feed is not a completeness source.
- Facebook posts can originate Stories without Telegram support.
- Comments/replies are ordinary SourceItems and may contain independent Story candidates.
- Prefer `All`/`Newest` comment modes where available; `Most relevant` is fallback and must be recorded as such.
- `partial != complete`; only reaching the accessible end permits `complete`.
- Incremental refresh is supplemented by periodic deep sweep.
- CAPTCHA/checkpoint/account-action => hard stop/manual action; no bypass or stealth implementation.
- Credentials/session state live only in private filesystem browser profiles, never normal DB fields/artifacts.
- One auth profile executes at max concurrency 1 via Procrastinate `lock`.
- `queueing_lock` prevents redundant pending refresh work but is not the execution-concurrency mechanism.
- Collector DOM/screenshots/traces are short-lived diagnostics only.
- Legacy Telegram rows with unknown collection time are marked limited-temporal-fidelity; do not invent timestamps.

---

## File structure locked by this plan

Create:

```text
migrations/0009_facebook.sql
migrations/0010_legacy_import_tracking.sql
migrations/0011_drop_legacy_messages.sql
src/providers/facebook/
  __init__.py
  models.py
  auth.py
  browser.py
  collector.py
  comments.py
  diagnostics.py
src/repositories/facebook.py
src/jobs/facebook.py
src/retention.py
scripts/bootstrap_facebook_profile.py
scripts/import_legacy_messages.py
tests/providers/facebook/
  test_auth.py
  test_browser_state.py
  test_collector.py
  test_comments.py
  test_deep_refresh.py
  test_diagnostics.py
tests/migration/test_legacy_import.py
tests/integration/test_facebook_knowledge_publication.py
```

Modify:

```text
pyproject.toml
requirements.txt
uv.lock
src/config_loader.py
src/ingestion/registry.py
src/jobs/ingestion.py
src/jobs/maintenance.py
src/worker.py
src/storage.py
src/core.py
src/collector.py
src/scheduler.py
main.py
Dockerfile
docker-compose.yml
.env.example
config.yaml.example
README.md
```

### Task 1: Add Facebook dependency, auth/source schema, and config

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `uv.lock`
- Create: `migrations/0009_facebook.sql`
- Create: `src/providers/facebook/models.py`
- Create: `src/repositories/facebook.py`
- Modify: `src/config_loader.py`
- Modify: `src/ingestion/registry.py`
- Modify: `src/ingestion/schedule.py`
- Modify: `src/jobs/ingestion.py`
- Test: `tests/providers/facebook/test_auth.py`
- Modify/Test: `tests/ingestion/test_registry.py`
- Modify/Test: `tests/ingestion/test_schedule.py`

**Interfaces:**
- Tables `facebook_auth_profiles`, `facebook_source_configs`, `facebook_comment_collection_state`, `collector_artifacts`.
- `FacebookAuthProfile.storage_ref` is logical, not absolute.
- Config produces `FacebookCommentsConfig(include_replies: bool = True, max_comments_per_post: int = 500, max_replies_per_comment: int = 100, max_pages_per_refresh: int = 20, max_duration_per_post_seconds: int = 120)`.
- Config produces bootstrap-only `FacebookAuthProfileBootstrap(name, storage_ref)` and `FacebookSourceBootstrap(name, kind, url, role, auth_profile, enabled, scan_times, timezone)` plus `FacebookConfig(auth_root: str = "/var/lib/telebrief/auth", auth_profiles: list[FacebookAuthProfileBootstrap], sources: list[FacebookSourceBootstrap], comments: FacebookCommentsConfig)`. Default `scan_times` may be `08:00, 12:00, 16:00, 19:30` in the configured source timezone, but every source may override them.
- `SourceRegistry.bootstrap_from_config()` is extended to create/update bootstrap-managed generic Sources plus `FacebookSourceConfig`/AuthProfile rows idempotently; DB-managed Sources are never overwritten.

- [ ] **Step 1: Write failing schema/config tests**

Assert no credential columns (`cookie`, `password`, `local_storage`, `session_storage`) exist in auth table. Assert source config FK references generic Source and AuthProfile. Assert comment limits parse and validate positive bounds. Bootstrap the same Facebook profile/source twice and assert one generic Source + one provider config row; switch the Source to `management_mode='database'`, change YAML role/schedule, bootstrap again, and assert DB-managed values are preserved. Assert daily scan times parse as valid local `HH:MM` values with an IANA timezone.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/providers/facebook/test_auth.py -q
```

- [ ] **Step 3: Implement schema/config**

Add `playwright>=1.62.0` to both dependency manifests:

```toml
# pyproject.toml [project].dependencies
"playwright>=1.62.0",
```

```text
# requirements.txt
playwright>=1.62.0
```

Regenerate the project lock:

```bash
uv lock
```

Install the matching Chromium build during deployment/image build. Do not add anti-detection/stealth libraries.

Auth status CHECK:

```text
unknown | ready | auth_required | checkpoint_required | account_action_required | disabled
```

Comment state fields include last scan time, oldest/newest observed comment time, observed counters, `completeness`, and JSON continuation state. Extend `CollectionSchedulePolicy` from Plan 2 with a `daily_times` policy: for a Source with `08:00/12:00/16:00/19:30`, it is due once for each local-time slot and never repeatedly within the same slot; persist the last successful scheduled slot in checkpoint adapter state. `PRE_PUBLISH` ignores the normal daily-time gate and uses the same source-scan task at high priority.

- [ ] **Step 4: Run tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/providers/facebook/test_auth.py -q
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt uv.lock migrations/0009_facebook.sql src/providers/facebook/models.py src/repositories/facebook.py src/config_loader.py src/ingestion/registry.py src/ingestion/schedule.py src/jobs/ingestion.py tests/providers/facebook/test_auth.py tests/ingestion/test_registry.py tests/ingestion/test_schedule.py
git commit -m "feat(facebook): add auth profile and source schema"
```

### Task 2: Implement persistent browser profile bootstrap and session-state classification

**Files:**
- Create: `src/providers/facebook/auth.py`
- Create: `src/providers/facebook/browser.py`
- Create: `scripts/bootstrap_facebook_profile.py`
- Test: `tests/providers/facebook/test_browser_state.py`

**Interfaces:**
- Produces `FacebookBrowserSession.open(profile: FacebookAuthProfile)` async context manager.
- Produces `classify_facebook_state(page) -> FacebookSessionState`.
- Browser profile path resolves as `Path(config.auth_root) / storage_ref` and must remain inside `auth_root`.

- [ ] **Step 1: Write failing classification/security tests**

Use fake pages/URLs/visible markers:

```text
/login -> auth_required
/checkpoint -> checkpoint_required
CAPTCHA marker -> account_action_required
normal authenticated group page -> ready
```

Test `storage_ref="../../etc"` is rejected. Test newly-created profile directories are mode `0700` (or stricter) and profile files are owner-only where the platform allows. Test logs do not contain cookie/header values.

- [ ] **Step 2: Run failure**

```bash
pytest tests/providers/facebook/test_browser_state.py -q
```

- [ ] **Step 3: Implement dedicated persistent context**

Resolve the logical `storage_ref` under the configured root and classify state before any source navigation:

```python
def profile_dir(auth_root: Path, storage_ref: str) -> Path:
    relative = PurePosixPath(storage_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid Facebook auth storage_ref")
    return auth_root.joinpath(*relative.parts)


async def open_authenticated_context(self, profile: FacebookAuthProfile):
    path = profile_dir(self.config.auth_root, profile.storage_ref)
    ensure_owner_only_directory(path)
    context = await self.playwright.chromium.launch_persistent_context(str(path), headless=True)
    state = await self.classify_session_state(context)
    if state != FacebookAuthState.READY:
        await context.close()
        raise FacebookHumanActionRequired(state)
    return context
```

Launch Chromium persistent context with the profile directory. Do not store exported session data in PostgreSQL. Bootstrap script may import user-provided cookies into the dedicated profile, then discard the import file only when explicitly requested by operator; document that cookies are credentials.

State classifier is conservative: uncertain platform screens become `account_action_required` or `unknown_platform_state`, never automated bypass attempts.

- [ ] **Step 4: Run tests**

```bash
pytest tests/providers/facebook/test_browser_state.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/providers/facebook/auth.py src/providers/facebook/browser.py scripts/bootstrap_facebook_profile.py tests/providers/facebook/test_browser_state.py
git commit -m "feat(facebook): add persistent browser auth profiles"
```

### Task 3: Implement explicit Facebook post collector with semantic selectors

**Files:**
- Create: `src/providers/facebook/collector.py`
- Modify: `src/jobs/facebook.py`
- Modify: `src/jobs/ingestion.py`
- Test: `tests/providers/facebook/test_collector.py`

**Interfaces:**
- Produces `FacebookCollector(Collector)` returning `ObservedItem(kind="facebook_post")`.
- Uses explicit Source URL, not Home Feed.
- Extend the generic `enqueue_source_scan()` execution-lock resolver so Facebook source scans use `lock=facebook-auth-profile:<profile_id>`.
- Keep the generic queueing lock `scan-source:<source_id>` so normal and PRE_PUBLISH requests coalesce consistently.

- [ ] **Step 1: Write failing HTML/page abstraction tests**

Mock page abstraction with ARIA-role/name queries and stable links. Assert canonical post ID extraction, canonical URL, author, timestamp, text, media references. Normalize Facebook `SourceItem.external_id` with an object-kind namespace (`post:<provider-id>`, `comment:<provider-id>`) so the global per-Source uniqueness rule cannot collide if Facebook reuses numeric ID spaces across object kinds. Generated/obfuscated CSS class names must not appear in selector constants.

Add source-state cases: a reliable provider marker/permalink result proving deletion emits `ObservedStateEvent(type="deleted_at_source")`; login wall, permission denial, timeout, or ambiguous missing content emits `inaccessible` with a reason and never silently marks deletion.

- [ ] **Step 2: Run failure**

```bash
pytest tests/providers/facebook/test_collector.py -q
```

- [ ] **Step 3: Implement collector**

Borrow principles from `fbcli`/`fbn`: semantic/ARIA selectors, stable URL/ID parsing, bounded scrolling, explicit state guards. If implementation copies MIT-licensed code rather than only patterns, preserve required copyright/license notices in source/docs.

Before enabling Facebook scheduling, register a provider-specific lock resolver used by Plan 2's `enqueue_source_scan()`:

```python
async def facebook_execution_lock(conn, source_id: int) -> str:
    cfg = await facebook_repo.get_source_config(conn, source_id)
    return f"facebook-auth-profile:{cfg.auth_profile_id}"
```

This guarantees normal scheduled scans and Plan 4 `PRE_PUBLISH` scans share the same AuthProfile execution lock. On auth/checkpoint/account action, update AuthProfile operational state and return a typed collection outcome; do not allow generic retry policy to hammer the profile.

Keep availability semantics explicit in the adapter output:

```python
if page_state == PageState.DELETED_CONFIRMED:
    state_events.append(ObservedStateEvent(item_external_id=post_id, type="deleted_at_source", reason="provider_marker"))
elif page_state in {PageState.PERMISSION_DENIED, PageState.TRANSIENT_ERROR, PageState.NOT_VISIBLE}:
    state_events.append(ObservedStateEvent(item_external_id=post_id, type="inaccessible", reason=page_state.value))
```

Never infer `deleted_at_source` merely because an item disappeared from a list scan.

- [ ] **Step 4: Run tests**

```bash
pytest tests/providers/facebook/test_collector.py tests/providers/facebook/test_auth.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/providers/facebook/collector.py src/jobs/facebook.py src/jobs/ingestion.py tests/providers/facebook/test_collector.py
git commit -m "feat(facebook): collect explicit source posts"
```

### Task 4: Implement completeness-oriented comments/replies collection

**Files:**
- Create: `src/providers/facebook/comments.py`
- Modify: `src/jobs/facebook.py`
- Test: `tests/providers/facebook/test_comments.py`

**Interfaces:**
- Produces `FacebookCommentCollector.scan_post(post_item_id, mode) -> CommentCollectionBatch`.
- Produces `FacebookCommentRefreshService.refresh(post_item_id: int, mode: str) -> CommentRefreshResult`; it performs browser collection before DB work, then co-commits generic SourceItems/Revisions and `facebook_comment_collection_state` using `IngestionService.ingest_batch_in_transaction(conn, source_id, trigger, batch)`.
- `mode = incremental | deep | pre_publish`.
- Output includes `requested_sort`, `effective_sort`, `completeness`, `stop_reason`, observed counts, and normal `ObservedItem`s for comments/replies.

- [ ] **Step 1: Write failing completeness tests**

Cases:

```text
exhaust accessible list -> complete/exhausted
hit max_comments -> partial/max_comments
hit max_pages -> partial/max_pages
timeout -> partial/timeout
cannot determine end -> unknown/platform_behavior
Most relevant fallback -> never infer complete solely from lack of more visible rows
```

Reply SourceItem parent/root identity must be correct using the same namespaced IDs: comment parent is `post:<id>`, reply parent is `comment:<id>`, and `root_external_id` is `post:<id>`. A comment missing from a later thread traversal must **not** become deleted merely by absence; only a reliable explicit deletion/unavailable signal may emit a state event, with ambiguous states represented as `inaccessible`.

- [ ] **Step 2: Run failure**

```bash
pytest tests/providers/facebook/test_comments.py -q
```

- [ ] **Step 3: Implement comment traversal**

Use one bounded loop that records why it stopped instead of guessing completeness:

```python
while True:
    if counters.comments >= limits.max_comments_per_post:
        return batch.partial("max_comments")
    if counters.pages >= limits.max_pages_per_refresh:
        return batch.partial("max_pages")
    if monotonic() - started >= limits.max_duration_per_post_seconds:
        return batch.partial("timeout")

    observed, reached_end = await self._read_visible_comments_and_expand_replies(page)
    batch.add(observed)
    if reached_end:
        return batch.complete("exhausted")
    if not await self._load_more(page):
        return batch.unknown("platform_behavior")
```

Prefer UI controls equivalent to All/Newest when available; record the effective mode. Expand comments then replies using semantic controls. Stop on configured limits and persist partial coverage. Use persistent comment IDs + timestamps/coverage as primary incremental correctness, not opaque Facebook cursor alone.

The collector only returns observations. `FacebookCommentRefreshService` first finishes the browser scan, then persists observations and coverage atomically:

```python
batch = await comment_collector.scan_post(post_item_id, mode)
async with self.uow.transaction() as conn:
    ingestion = await ingestion_service.ingest_batch_in_transaction(
        conn, source_id=batch.source_id, trigger=CollectionTrigger.ENRICHMENT,
        batch=batch.as_collection_batch(),
    )
    await facebook_repo.update_comment_collection_state(conn, batch.coverage)
return CommentRefreshResult(ingestion=ingestion, coverage=batch.coverage)
```

Add `ENRICHMENT` as a provider-neutral collection trigger in this task (or name it `ENRICHMENT_REFRESH` consistently across enum/schema/tests). A failed transaction cannot advance coverage without the corresponding comments/replies. When the UI exposes an explicit deleted-comment placeholder tied to a known comment ID, emit `deleted_at_source`; on permission/auth/transient ambiguity emit `inaccessible`; absence from an incomplete/partial scan is not evidence of deletion.

- [ ] **Step 4: Run tests**

```bash
pytest tests/providers/facebook/test_comments.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/providers/facebook/comments.py src/jobs/facebook.py tests/providers/facebook/test_comments.py
git commit -m "feat(facebook): collect comments and replies incrementally"
```

### Task 5: Add relevance-triggered enrichment and periodic deep refresh

**Files:**
- Create: `src/ingestion/enrichment.py`
- Modify: `src/jobs/facebook.py`
- Modify: `src/jobs/processing.py`
- Modify: `src/jobs/ingestion.py`
- Modify: `src/processing/relevance.py`
- Modify: `src/config_loader.py`
- Create/Test: `tests/ingestion/test_enrichment.py`
- Test: `tests/providers/facebook/test_deep_refresh.py`

**Interfaces:**
- Produces provider-neutral `EnrichmentRequest(kind: str, source_item_revision_id: int, mode: str, metadata: dict[str, JSONValue])`.
- Produces `EnrichmentPlanner.requests_for(decision, revision) -> list[EnrichmentRequest]` and `pre_publish_requests(source_id: int, *, limit: int) -> list[EnrichmentRequest]`; provider modules register enrichment rules, so generic relevance/scan orchestration does not import Facebook.
- Produces `EnrichmentDispatcher.defer(conn, request: EnrichmentRequest, *, priority: int = 0) -> int | None` and `defer_without_domain_transaction(request, *, priority: int = 0) -> int | None`; the dispatcher maps request kind/provider to a task and applies the same provider/AuthProfile execution-lock resolver used by source scans.
- The Facebook rule produces `EnrichmentRequest(kind="facebook_comments", source_item_revision_id=<post revision>, mode="incremental", metadata={"post_item_id": <post id>})` for relevant Facebook posts rather than calling the comment collector directly.
- Normal refresh checks recent/new comments/replies.
- Deep refresh revisits older thread coverage.
- PRE_PUBLISH uses bounded deeper mode for significant/recent active Facebook posts even when the parent post itself has no new revision in the latest source scan.

- [ ] **Step 1: Write failing orchestration tests**

Assert irrelevant post does not automatically schedule expensive comment scan; the Facebook enrichment rule turns a relevant post into one generic `EnrichmentRequest`. Assert generic relevance invokes `EnrichmentPlanner` without importing Facebook. Assert the dispatcher resolves the request to the Facebook comment task and applies the AuthProfile execution lock. Assert periodic deep dispatcher selects eligible recent/significant posts without duplicating pending jobs. Assert a `PRE_PUBLISH` source scan schedules bounded deep requests for an already-known relevant active post even when that scan produced no new parent-post revision. Assert late reply to old comment appears after deep refresh.

- [ ] **Step 2: Run failure**

```bash
pytest tests/ingestion/test_enrichment.py tests/providers/facebook/test_deep_refresh.py -q
```

- [ ] **Step 3: Implement enrichment jobs**

Keep relevance provider-neutral: it asks the planner for requests, and provider registration supplies the Facebook rule:

```python
for request in enrichment_planner.requests_for(decision, revision):
    await enrichment_dispatcher.defer(conn, request, priority=0)

# registered by the Facebook provider module
def plan_facebook_comments(decision, revision) -> EnrichmentRequest | None:
    if decision.status != "relevant":
        return None
    if revision.platform != "facebook" or revision.kind != "facebook_post":
        return None
    return EnrichmentRequest(
        kind="facebook_comments",
        source_item_revision_id=revision.id,
        mode="incremental",
        metadata={"post_item_id": revision.source_item_id},
    )
```

`EnrichmentDispatcher` resolves `facebook_comments` to `refresh_facebook_comments`, sets `queueing_lock=f"facebook-comments:{post_item_id}:{mode}"`, and sets `lock=f"facebook-auth-profile:{auth_profile_id}"`. Future RSS full-text or transcript enrichment adds a planner/dispatcher registration without changing relevance.

Keep deep sweep independent but route its work through the same dispatcher:

```python
@procrastinate_app.periodic(cron="0 */6 * * *", periodic_id="facebook-deep-sweep")
@procrastinate_app.task(queue="maintenance", queueing_lock="facebook-deep-sweep")
async def dispatch_facebook_deep_sweep(timestamp: int) -> None:
    runtime = get_runtime()
    candidates = await facebook_repo.list_posts_due_for_deep_refresh(
        runtime.pool, scheduled_at=datetime.fromtimestamp(timestamp, timezone.utc)
    )
    for post in candidates:
        request = EnrichmentRequest(
            kind="facebook_comments",
            source_item_revision_id=post.current_revision_id,
            mode="deep",
            metadata={"post_item_id": post.source_item_id},
        )
        await enrichment_dispatcher.defer_without_domain_transaction(request, priority=10)
```

`defer_without_domain_transaction()` is only the dispatcher convenience for periodic orchestration where there is no accompanying domain write; it still uses the same provider lock/queueing-lock policy. Use queueing locks keyed by post/mode to avoid backlog and execution lock keyed by auth profile. Deep refresh policy should be configurable by age/story activity rather than hard-coded to every historical post.
After a Facebook `scan_source` job completes with `trigger=PRE_PUBLISH`, invoke the generic planner's `pre_publish_requests(source_id, limit=config.facebook.pre_publish_comment_posts)` and defer each request at priority `100` through the same `EnrichmentDispatcher`. The Facebook registration queries only relevant recent/active posts, ordered by active Story/publication signals when available and recency otherwise. Default the bound to a small value such as 10 and make it configurable. This path must not call `refresh_facebook_comments` directly, so AuthProfile execution locks remain unavoidable.


- [ ] **Step 4: Run tests**

```bash
pytest tests/ingestion/test_enrichment.py tests/providers/facebook/test_deep_refresh.py tests/jobs -q
```

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/enrichment.py src/jobs/facebook.py src/jobs/processing.py src/jobs/ingestion.py src/processing/relevance.py src/config_loader.py tests/ingestion/test_enrichment.py tests/providers/facebook/test_deep_refresh.py
git commit -m "feat(facebook): add comment enrichment and deep sweeps"
```

### Task 6: Add diagnostic CollectorArtifacts and retention rules

**Files:**
- Create: `src/providers/facebook/diagnostics.py`
- Create: `src/retention.py`
- Modify: `src/jobs/maintenance.py`
- Modify: `src/jobs/app.py`
- Test: `tests/providers/facebook/test_diagnostics.py`

**Interfaces:**
- Produces `CollectorArtifactStore.record(run_id: int, *, source_item_id: int | None, kind: str, content: bytes | str, expires_at: datetime) -> CollectorArtifact` only for configured anomaly/debug states.
- Produces retention cleanup that can delete physical artifact/media files while preserving DB provenance metadata as configured.

- [ ] **Step 1: Write failing safety tests**

Assert successful normal scan does not persist DOM by default. `layout_changed` may store a redacted HTML fragment/screenshot ref. Attempt to record cookies/localStorage/session storage fields is rejected/redacted. Expiring artifact does not delete SourceItemRevision/Claim/Story/Publication rows.

- [ ] **Step 2: Run failure**

```bash
pytest tests/providers/facebook/test_diagnostics.py -q
```

- [ ] **Step 3: Implement artifacts and retention**

Keep physical deletion behind explicit retention decisions:

```python
async def cleanup_expired_files(now: datetime) -> CleanupResult:
    artifacts = await artifact_repo.list_expired(now)
    media = await asset_repo.list_expired_unpinned(now)
    deleted = 0
    for record in [*artifacts, *media]:
        if record.storage_ref:
            await storage.delete(record.storage_ref)
            deleted += 1
        await retention_repo.record_physical_cleanup(record.id, cleaned_at=now)
    return CleanupResult(files_deleted=deleted)
```

Default debug retention 7-14 days; heavy ordinary media ~30 days; published/provenance-pinned media exempt according to retention policy. Store files under configured data root using logical refs, not arbitrary absolute paths supplied by provider data. Register one Procrastinate maintenance task and import it through `src/jobs/app.py`:

```python
@procrastinate_app.periodic(cron="15 3 * * *", periodic_id="retention-cleanup")
@procrastinate_app.task(queue="maintenance", queueing_lock="retention-cleanup")
async def retention_cleanup(timestamp: int) -> None:
    await retention_service.cleanup(
        now=datetime.fromtimestamp(timestamp, timezone.utc)
    )
```

The periodic timestamp is the cleanup cutoff; repeated dispatcher execution is idempotent because already-cleaned records have no physical ref to delete again.

- [ ] **Step 4: Run tests**

```bash
pytest tests/providers/facebook/test_diagnostics.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/providers/facebook/diagnostics.py src/retention.py src/jobs/maintenance.py src/jobs/app.py tests/providers/facebook/test_diagnostics.py
git commit -m "feat(retention): add short-lived collector diagnostics"
```

### Task 7: Prove single Facebook comment can become an attributed Publication

**Files:**
- Create: `tests/integration/test_facebook_knowledge_publication.py`
- Create: `tests/fixtures/facebook_single_comment_story.json`

**Interfaces:**
- Uses the real ingestion/knowledge/publication service boundaries with fake browser/provider responses.

- [ ] **Step 1: Build fixture**

Post is locally relevant; one comment says the specific useful operational fact `"На Мелитопольском у Кабельщиков движение перекрыто, машины разворачивают"`, with no second source. Include one separate high-risk accusation comment to ensure it is not promoted to established fact.

- [ ] **Step 2: Write end-to-end assertions**

Assert:

```text
comment SourceItem created
Claim created
Story created/activated
Evidence state reported/single-source
Publication candidate exists
selector may INCLUDE
writer wording retains resident attribution/uncertainty
high-risk accusation is not rendered as established fact
```

- [ ] **Step 3: Run and fix only architecture gaps**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/integration/test_facebook_knowledge_publication.py -q
```

- [ ] **Step 4: Run full Facebook + publication suite**

```bash
pytest tests/providers/facebook tests/publication -q
```

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_facebook_knowledge_publication.py tests/fixtures/facebook_single_comment_story.json
git commit -m "test(facebook): publish useful single-source comment"
```

### Task 8: Add one-time legacy `messages` importer with temporal-fidelity marker

**Files:**
- Create: `migrations/0010_legacy_import_tracking.sql`
- Create: `scripts/import_legacy_messages.py`
- Test: `tests/migration/test_legacy_import.py`

**Interfaces:**
- Import is idempotent using explicit legacy identity mapping.
- Produces SourceItem/Revision records marked `legacy_imported=true` and temporal fidelity (`exact | limited`).

- [ ] **Step 1: Write failing importer tests**

Create a legacy row with published timestamp and one with/without trustworthy `collected_at`. Assert known timestamps are preserved and unknown collection time is not fabricated as historical fact. Running importer twice creates no duplicate SourceItems.

- [ ] **Step 2: Run failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/migration/test_legacy_import.py -q
```

- [ ] **Step 3: Implement importer**

Make import identity deterministic and rerunnable:

```python
for row in legacy_repo.iter_messages(batch_size=500):
    external_id = parse_telegram_message_id(row.link)
    if external_id is None:
        external_id = f"legacy-message:{row.id}"
    result = await ingestion_service.import_legacy_message(
        source_key=legacy_source_key(row.channel_name),
        external_id=str(external_id),
        text=row.text,
        published_at=row.timestamp,
        collected_at=row.collected_at if row.collected_at_is_trustworthy else None,
        temporal_fidelity="exact" if row.collected_at_is_trustworthy else "limited",
        legacy_row_id=row.id,
    )
    report.record(result)
```

Read legacy rows in batches. First parse the legacy `link` field for the Telegram message ID and public/private chat identity; when available, use that stable provider identity so repeated legacy rows for the same Telegram message collapse into one SourceItem and only content changes create revisions. Only when the link cannot provide stable identity use `legacy-message:<legacy_row_id>` and mark that limitation. Store `imported_at` separately from any claimed historical `collected_at`.

- [ ] **Step 4: Run tests and dry-run report**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/migration/test_legacy_import.py -q
python scripts/import_legacy_messages.py --dry-run
```

Dry run prints counts only; no source text/credentials in logs.

- [ ] **Step 5: Commit**

```bash
git add migrations/0010_legacy_import_tracking.sql scripts/import_legacy_messages.py tests/migration/test_legacy_import.py
git commit -m "feat(migration): import legacy telegram messages"
```

### Task 9: Remove legacy direct storage/collection publication path after validation

**Files:**
- Create: `migrations/0011_drop_legacy_messages.sql`
- Modify: `src/storage.py`
- Modify: `src/core.py`
- Modify: `src/collector.py`
- Modify: `src/scheduler.py`
- Modify: `src/config_loader.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `uv.lock`
- Modify: `main.py`
- Modify: `README.md`
- Update corresponding tests

**Interfaces:**
- New publication facade from Plan 4 is the only digest/article production path.
- Interactive Telegram session bootstrap may remain in `src/collector.py`; direct digest/article collection functions do not.

- [ ] **Step 1: Write tests that forbid legacy path**

Search/import tests must fail if production publication modules import `create_storage`, `SQLiteBackend`, `PostgresBackend`, or instantiate `MessageCollector`. Keep explicit interactive auth test exceptions outside publication packages.

- [ ] **Step 2: Run tests and confirm current legacy references fail the new guard**

```bash
pytest tests/test_core.py tests/test_scheduler.py -q
```

- [ ] **Step 3: Delete retired code/dependencies**

The resulting production facade should have no provider/storage imports:

```python
# src/core.py after cutover
async def build_digest(config, logger, hours):
    return await request_publication(
        publication_type="digest",
        edition_slug="berdyansk",
        snapshot_at=datetime.now(timezone.utc),
    )

async def generate_and_publish_article(config, logger, hours, dry_run=False):
    return await request_publication(
        publication_type="daily_article",
        edition_slug="berdyansk",
        snapshot_at=datetime.now(timezone.utc),
        dry_run=dry_run,
    )
```

Remove new-production use of `src/storage.py`; once importer/validation period is complete, remove `aiosqlite` and `asyncpg` from both `pyproject.toml` and runtime `requirements.txt`, then regenerate `uv.lock`. `apscheduler` should already have been removed in Plan 4 when Procrastinate became the publication clock; add an import/dependency guard here so it cannot regress. Delete obsolete `_save_to_storage`, live `_collect_messages` publication path, the transitional `settings.persistent_ingestion` switch, and legacy storage config only after config migration docs are present. At this final cutover the normalized database path is unconditional for publication; disabling `database.enabled` is no longer a valid production configuration.

Create `migrations/0011_drop_legacy_messages.sql` containing `DROP TABLE messages;`, but mark it in deployment documentation as a **separate post-validation release**. The commit may contain the migration; operators apply it only after the legacy importer report is accepted and a database backup is verified. Never run it in the same deployment that first stops legacy reads.

- [ ] **Step 4: Run full suite**

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
```

- [ ] **Step 5: Commit**

```bash
git add migrations/0011_drop_legacy_messages.sql src pyproject.toml requirements.txt uv.lock main.py README.md tests
git commit -m "refactor: retire legacy direct telegram publication path"
```

### Task 10: Production hardening and operational documentation

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `config.yaml.example`
- Modify: `README.md`
- Modify: `src/jobs/maintenance.py`
- Test: `tests/jobs/test_maintenance.py`

**Interfaces:**
- Document separate app/worker processes and PostgreSQL/Procrastinate migrations.
- Periodic stalled-job recovery is enabled.
- Auth profile filesystem permissions/root path are documented.

- [ ] **Step 1: Add maintenance tests**

Use Procrastinate test app/fakes to verify stalled-job recovery defers/retries only detected stalled jobs and retention task uses queueing lock so maintenance does not accumulate.

- [ ] **Step 2: Add deployment shape**

Document/run services equivalent to:

```text
telebrief-app     -> bot/MCP/request facade
telebrief-worker  -> Procrastinate worker
postgres          -> PostgreSQL 18 + pgvector
```

Keep worker count/concurrency conservative on the 1 GB/1 CPU host; Facebook browser jobs share auth-profile locks. Extend `src/jobs/app.py` `import_paths` with `src.jobs.facebook`. Document initial PostgreSQL settings exactly as the spec: `shared_buffers=64MB`, `effective_cache_size=256MB`, `work_mem=2MB`, `maintenance_work_mem=32MB`, `autovacuum_work_mem=16MB`, `temp_buffers=4MB`, `max_connections=10`, parallel workers disabled, `jit=off`, `autovacuum_max_workers=1`; recommend `vm.swappiness=10` as host tuning.

Because the current image is `python:3.14.3-slim`, install Chromium and its system dependencies during image build while still running as root, then run Telebrief as the existing non-root user. Keep the auth/profile volume separate from the image:

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

RUN mkdir -p logs sessions data /var/lib/telebrief/auth \
    && chown -R telebrief:telebrief logs sessions data /var/lib/telebrief/auth \
    && chmod 700 /var/lib/telebrief/auth
```

In `docker-compose.yml`, mount the auth root as a persistent private volume and give the worker a conservative process/concurrency configuration; do not start a separate Chromium daemon.

- [ ] **Step 3: Document operator procedures**

Document executable command sequences, not prose-only hints:

```bash
python scripts/migrate.py
PYTHONPATH=. procrastinate --app=src.jobs.app.procrastinate_app schema --apply
python scripts/bootstrap_facebook_profile.py --profile profile-01
python scripts/import_legacy_messages.py --dry-run
python scripts/import_legacy_messages.py --apply
```

Include exact sequences for domain migrations, Procrastinate official migrations, browser-profile bootstrap/manual reauth, enabling a Facebook Source, legacy import dry run/apply, and recovery from checkpoint/account-action state. Explicitly state no CAPTCHA bypass. Also document that a disappeared post/comment is not marked deleted unless the adapter has a reliable deletion signal; ambiguous provider visibility remains `inaccessible`.

- [ ] **Step 4: Run full verification**

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example config.yaml.example README.md src/jobs/maintenance.py tests/jobs/test_maintenance.py
git commit -m "docs(ops): document multisource production deployment"
```

## Plan 5 completion gate

Acceptance must demonstrate:

1. Telegram and Facebook both create normal SourceItems/Revisions.
2. Facebook comment collection reports completeness honestly and deep refresh finds late replies.
3. Checkpoint/CAPTCHA pauses only the affected AuthProfile and does not retry-loop.
4. A single useful Facebook comment can produce an attributed Publication.
5. Publications still generate if Facebook is unavailable at publication time.
6. Legacy `messages` is no longer part of the production publication path.
7. No credentials appear in PostgreSQL or CollectorArtifacts.
