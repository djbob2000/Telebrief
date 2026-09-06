# Telebrief: multisource ingestion, persistent knowledge, and publication architecture

**Status:** design approved in chat; GitHub commit blocked by connector permissions

## Goal

Evolve Telebrief from a Telegram-first digest/article generator into a persistent, provider-neutral local-news system that can ingest Telegram, Facebook, and later VK, RSS, websites, and other sources without rebuilding the editorial core for every provider.

The architecture must preserve the most important behavior of the current `custom` branch: **publication is permissive and attribution-aware, not verification-gated**. Better provenance, clustering, contradiction tracking, and historical state must improve editorial understanding without recreating the earlier failure mode where strict verification caused substantive local news to disappear.

```text
Source history != Knowledge history != Publication history != Delivery history
```

```text
richer provenance must NOT mean higher publication threshold
```

A single concrete, useful Facebook comment from a local resident may become a publishable Story when locally relevant and honestly attributed. Source count, official confirmation, or a verification score must never become a universal publication gate.

## Non-goals

- Do not build Facebook on top of the legacy Telegram `Message` model.
- Do not require two or more sources before publication.
- Do not turn source role into a truth score.
- Do not use deterministic geographic keyword filtering as the relevance gate.
- Do not make publication wait for all processing jobs.
- Do not make verification/audit availability mandatory for publication.
- Do not build a custom PostgreSQL queue, leases, or `SKIP LOCKED` workers; use Procrastinate.
- Do not use SQLAlchemy ORM/Core in the primary persistence path.
- Do not mutate legacy `messages` into the new domain by repeated `ALTER TABLE`.
- Do not store Facebook cookies, passwords, localStorage, or sessionStorage in PostgreSQL or Collector artifacts.
- Do not bypass CAPTCHA/checkpoint/account-action flows.
- Do not build a full global entity knowledge graph in v1.
- Do not add HNSW/IVFFlat until exact pgvector retrieval proves insufficient.

## 1. Architectural shape

```text
Telegram   Facebook   VK   RSS   Web   ...
    \         |        |    |     /
             Collectors
                 |
                 v
Source -> SourceItem -> SourceItemRevision -> SourceAsset
                 |
                 v
Edition-specific Relevance
                 |
                 v
Claim Extraction -> Claims -> Embeddings
                 |
                 v
Story Matching -> persistent Stories / StoryRevisions
                 |
                 v
Evidence Assessment / optional light Verification
                 |
                 v
PublicationRun frozen snapshot
                 |
                 v
Editorial Selection -> Generation Attempts -> Publication
                 |
                 v
DeliveryPayload -> Delivery -> DeliveryAttempts
```

Collectors build source history. Processing builds knowledge. Publications snapshot knowledge. Delivery sends already-created publications.

The legacy direct path `Telegram MessageCollector -> Digest/Article` is transitional only and must disappear after migration.

## 2. Source model

`Source` is provider-neutral and global:

```text
Source
- id
- platform                 # telegram | facebook | vk | rss | web | ...
- kind                     # channel | group | page | profile | website | feed | ...
- external_id nullable
- url nullable
- name
- role                     # official | local_media | community | individual | other
- enabled
- collector_options
- created_at
- updated_at
```

Source role is an editorial prior, not proof and not publication permission.

Provider-specific configuration is separate. Facebook uses `FacebookSourceConfig(source_id, facebook_url/external_id, auth_profile_id, options)`. This may later evolve into auth bindings without changing `Source`.

## 3. Source items and immutable revisions

```text
SourceItem
- id
- source_id
- platform
- kind
- external_id
- parent_item_id nullable
- root_item_id nullable
- author metadata
- canonical_url nullable
- published_at nullable
- first_collected_at
- metadata
```

Core identity invariant:

```text
UNIQUE(source_id, external_id)
```

Facebook posts, comments, and replies are all SourceItems. Replies use `parent_item_id` for the immediate parent and `root_item_id` for the root post.

Observed content is immutable:

```text
SourceItemRevision
- id
- source_item_id
- revision_no
- text
- structured metadata
- collected_at
- content_hash
- created_at
```

An edit creates a new revision. Old revisions remain available. `collected_at` belongs to the exact revision.

## 4. Source availability is not claim semantics

```text
SourceItemStateEvent
- id
- source_item_id
- type                     # deleted_at_source | inaccessible | restored | ...
- observed_at
- collector_run_id
- reason                   # auth | permission | transient | unknown | ...
- evidence
- created_at
```

Reliable deletion creates `deleted_at_source`; uncertain 404/timeout/visibility failure creates `inaccessible`; later visibility creates `restored`.

Source deletion does not imply that an extracted Claim is false.

## 5. Media and collector artifacts

Media is provider-neutral:

```text
SourceAsset
- id
- source_item_revision_id
- kind
- external_url nullable
- local_storage_ref nullable
- mime_type nullable
- width / height nullable
- duration nullable
- content_hash nullable
- metadata
- created_at
```

Suggested retention: source text/provenance indefinite; media metadata/hash/URL indefinite; heavy downloaded media ~30 days by default; publication/provenance media longer; derived vision results long-term; AI raw responses ~30 days; debug artifacts ~7-14 days.

`SourceItemRevision` is the durable normalized source-of-record. Raw HTML/DOM/screenshots/traces are optional short-lived diagnostics:

```text
CollectorArtifact
- id
- collector_run_id
- source_item_id nullable
- kind                     # raw_payload | extraction_metadata | html_fragment | screenshot | trace | error_context
- storage_ref
- content_hash
- created_at
- expires_at
```

Collector artifacts are mainly created for layout changes, extraction anomalies, unknown platform states, errors, or manual debug. Credentials/session secrets are never stored there.

Deleting debug artifacts or ordinary retained media must not invalidate Claims, Stories, ProcessingRuns, Publications, or historical replay.

## 6. Collector contract and ingestion

Collectors are dumb adapters:

```text
Collector.scan(source, checkpoint, context) -> CollectionBatch
```

They do not own editorial relevance and do not write domain rows directly. `IngestionOrchestrator` owns persistence, dedup, revisions, checkpoints, run history, and downstream scheduling.

Triggers:

```text
SCHEDULED | PRE_PUBLISH | MANUAL | BACKFILL
```

Checkpoint is an optimization, not correctness:

```text
CollectionCheckpoint
- source_id
- last_success_at
- last_scan_at
- cursor/high_watermark nullable
- backoff_until nullable
- consecutive_failures
- adapter_state
```

```text
CollectionRun
- source_id
- trigger
- started_at
- completed_at
- status
- seen_count
- new_count
- updated_count
- error_kind nullable
```

Typed outcomes include success, transient, rate_limited, auth_required, account_action_required, access_denied, source_not_found, layout_changed, permanent.

## 7. Telegram strategy

Telegram moves to persistent scheduled incremental ingestion, roughly every 30-60 minutes plus optional `PRE_PUBLISH`. Publication never invokes Telegram collection directly. `Message`/`MessageCollector` may remain temporarily only as compatibility/migration adapters.

## 8. Facebook strategy

Facebook is fail-soft. Primary discovery is explicit configured groups/pages/profiles, not Home Feed. Use bounded scheduled scans several times a day plus pre-publication refresh. Browser processes may open/close per scan while persistent profiles remain on disk.

A relevant Facebook post may independently originate a Story.

## 9. Facebook comments: completeness-oriented incremental collection

```text
relevant Facebook post
        -> All/Newest where possible
        -> expand comments
        -> expand replies
        -> collect as much as reasonably possible
        -> persistent SourceItems
        -> incremental refresh + periodic deep sweep
```

Example limits:

```yaml
facebook:
  comments:
    include_replies: true
    max_comments_per_post: 500
    max_replies_per_comment: 100
    max_pages_per_refresh: 20
    max_duration_per_post_seconds: 120
```

Completeness is explicit:

```text
complete | partial | unknown
```

`complete` only when the collector actually reaches the end of the accessible list. Limits/timeouts imply `partial`, never `complete`.

Track coverage:

```text
CommentCollectionState
- post_id
- last_successful_scan_at
- oldest_observed_comment_at
- newest_observed_comment_at
- comments_observed
- replies_observed
- completeness
- continuation_state
```

Correctness relies on persistent IDs/revisions/timestamps/coverage, not unstable Facebook cursors. Normal refreshes check new content; periodic deep sweeps revisit older threads for late replies, edits, and deletions. `Most relevant` is fallback only.

## 10. Enrichment is separate

Comments are enrichment of relevant posts, not part of the base collector contract:

```text
FacebookSourceCollector -> post SourceItem -> relevance -> EnrichmentRequest -> FacebookCommentCollector
```

The same pattern supports future RSS full-body fetches, transcripts, etc.

## 11. Facebook AuthProfile

```text
FacebookAuthProfile
- id
- name
- storage_ref
- status
- last_verified_at
- last_success_at
- last_failure_at
- last_failure_kind
- metadata
```

Use logical `storage_ref` (e.g. `facebook/profile-01`) and server-configured root such as `/var/lib/telebrief/auth/`. Persistent Chromium state lives on disk with owner-only permissions. PostgreSQL stores only the logical ref/status.

Statuses: `unknown`, `ready`, `auth_required`, `checkpoint_required`, `account_action_required`, `disabled`.

`/login` -> auth_required; `/checkpoint` -> checkpoint_required; CAPTCHA/consent/suspicious-login requiring a human -> account_action_required.

Invariant: checkpoint/CAPTCHA/account action stops automation for that profile, with no bypass and no aggressive retry. Other providers/profiles/publication continue.

All jobs sharing an AuthProfile execute at max concurrency 1.

## 12. Edition

`Edition` is publication scope, not geography:

```text
Edition
- id
- slug
- name
- timezone
- language
- profile/config
- current_relevance_policy_id
- enabled
```

Sources are global. Eligibility is many-to-many via `source_editions(source_id, edition_id)`. Edition-specific processing begins at relevance. `Edition != Place`.

## 13. Relevance

No `source_items.is_relevant` flag. Use immutable versioned decisions over exact revisions:

```text
RelevancePolicyVersion(id, edition_id, version, config_hash, prompt_version, created_at)
```

```text
EditionRelevanceDecision
- id
- source_item_revision_id
- edition_id
- relevance_policy_id
- status                   # relevant | irrelevant | uncertain | needs_media
- confidence nullable
- reason
- provider/model metadata
- parent_decision_id nullable
- created_at
```

Deterministic preprocessing removes only technical junk, not geographic relevance. AI decides local relevance. AI outage yields uncertain/pending, not false irrelevance. `needs_media` may lead to a later immutable decision after vision.

## 14. Vision

Modes: `off`, `relevance_only` (default), `full`.

`off`: media-only stays needs-media, not irrelevant. `relevance_only`: vision only when text is absent/weak/uncertain. `full`: deeper enrichment.

Vision results are derived provenance-bearing artifacts; they do not replace raw source. Extract visible text/observations/entities/location/time clues; no face identification. Resource limits cap assets, sizes, and calls.

## 15. Claim extraction and immutable Claims

Edition-specific, policy-versioned processing:

```text
SourceItemRevision -> EditionRelevanceDecision -> ClaimExtractionRun -> Claims
```

Canonical invariant:

```text
(source_item_revision_id, edition_id, extraction_policy_id)
-> at most one successful canonical result
```

Downstream claims become visible atomically only after the run succeeds. Timeout/provider retries are attempts of the same semantic run; reinterpretation requires new policy version.

Claims are immutable source-bound assertions. Corrections create new Claims and explicit relations:

```text
CORRECTS | SUPERSEDES | RETRACTS
```

Operational states may be cached, but every transition must be reconstructable from immutable relations/state events.

## 16. Temporal model

Three distinct times:

```text
published_at  # source publication
collected_at  # Telebrief observed exact revision
event_time    # described event actual/planned time
```

Event time supports interval, precision, confidence, and original text.

Historical snapshot `T` may only use source revisions and derived artifacts known/completed by `T`. Historical replay never reruns today's model over old material and pretends the new result existed historically.

## 17. Embeddings / pgvector

Embed only `Claim` and `StoryRevision`. Separate tables `claim_embeddings` and `story_revision_embeddings`. Embeddings are immutable; content/model changes create new rows. Use content hashes to avoid unnecessary calls.

Target PostgreSQL 18 + pgvector `vector` float32. No `halfvec`. No ANN index initially. Exact retrieval is preferred for recall and expected scale. Retrieval always filters compatible purpose/model/dimensions.

## 18. Persistent Stories

Lifecycle:

```text
candidate -> active -> resolved -> archived
                  ^       |
                  |-------| reopened
```

`StoryRevision` is immutable and created only when semantic state materially changes.

```text
Story(id, edition_id, current_revision_id, lifecycle_state, created_at)
StoryRevision(id, story_id, revision_no, title, summary, current_state, semantic_text, content_hash, created_at, reason)
```

DB guarantees current revision belongs to the same Story.

## 19. Story Matching

Two stages: exact semantic retrieval saves exact candidates; AI chooses assignment.

Assignment is only:

```text
SAME_STORY -> target_story_id
NEW_STORY
```

Relations are separate optional proposals: `CAUSED_BY`, `CONSEQUENCE_OF`, `RELATED_TO`, `FOLLOW_UP_TO`, `CORRECTS`.

Persist `StoryMatchingRun` including claim embedding/model/dimensions/metric/retrieval limit and `StoryMatchingCandidate` including exact story revision + embedding, rank, vector distance, location/entity/time/status/retrieval scores.

No LLM call is held inside an open PostgreSQL transaction.

Apply flow: retrieve -> persist run/candidates -> LLM outside transaction -> begin -> validate -> insert immutable decision -> apply domain changes -> defer next stage on same transaction -> mark succeeded -> commit.

For SAME_STORY, selected candidate revision must still equal `story.current_revision_id`; otherwise mark stale and rerun. For NEW_STORY, Story Matching is sequential per Edition initially to reduce duplicate races.

Canonical invariant:

```text
(claim_id, matching_policy_id)
-> at most one successful canonical run
```

Vector/location/entity signals never make the merge decision by themselves.

## 20. Places and lightweight Entities

Places are strong global reference data:

```text
Place(id, canonical_name, kind, parent_place_id, latitude, longitude, metadata)
PlaceAlias(place_id, alias, normalized_alias)
```

Alias text is not globally unique. A simple hierarchy/`WITHIN` relationship supports location overlap across street/district/building levels.

Do not store canonical place IDs inside immutable Claims. Preserve mentions:

```text
ClaimPlaceMention(id, claim_id, role, original_text, created_at)
PlaceResolutionRun(... policy/model/version ..., status)
PlaceResolutionResult(mention_id, resolution_run_id, place_id nullable, confidence, reason, created_at)
```

Unresolved geography never blocks Claim/publication. Improved resolvers may create new results later without rewriting old history.

Other entities remain lightweight normalized mentions with optional canonical identity; no required global knowledge graph in v1.

## 21. EvidenceCluster

Corroborating Claims never collapse into a single canonical Claim. Evidence assessment is immutable and revisioned:

```text
StoryRevision -> EvidenceAssessmentRun -> EvidenceCluster(s)
```

Each EvidenceAssessmentRun freezes the exact input Claim set it assessed, plus policy/model versions. Cluster members have stance `SUPPORTS`, `CONTRADICTS`, `UNCERTAIN`, or `CONTEXTUAL`.

A new Claim or StoryRevision creates a new assessment run and new immutable cluster set. Clusters may reference earlier clusters via `supersedes_cluster_id`.

Derived signals may include supporting claims, contradictions, unique sources, and estimated independent source groups. The latter is explicitly heuristic. No count is a publication threshold.

## 22. Verification is lightweight, optional, advisory

The current `custom` permissive behavior is the compatibility baseline. Optional immutable versioned verification may use soft states such as `reported`, `corroborated`, `officially_supported`, `disputed`, `retracted`.

Avoid binary truth labels and complex trust formulas in v1.

Verification is not mandatory critical path. If unavailable/not run, otherwise eligible material may proceed.

A single resident comment can be a valid publication candidate with honest attribution. More independent reports change wording/confidence, not allowed/forbidden status.

Risk is separate from verification state. Low-risk useful operational reports can publish with attribution from one source. High-risk unsupported accusations, casualty/medical claims, fabricated official orders, invented dangerous numbers/contacts, or unsupported cause/responsibility cannot be transformed into established fact.

Safety question:

```text
Can this be published safely with honest attribution and uncertainty?
```

not:

```text
Has it crossed a verification threshold?
```

## 23. Durable orchestration: Procrastinate

Infrastructure:

```text
PostgreSQL
├── Telebrief domain tables
├── pgvector
└── Procrastinate schema/tables

Python
├── psycopg 3 async
├── psycopg_pool.AsyncConnectionPool
├── pgvector-python
└── Procrastinate(PsycopgConnector)
```

No Telebrief-owned `processing_jobs`, leases, or `SKIP LOCKED` worker framework.

Central invariant:

```text
BEGIN
write domain artifacts
write canonical run result
Procrastinate.defer_async(next_task, connection=SAME_CONNECTION)
COMMIT
```

Procrastinate Job is operational orchestration, not a replacement for ClaimExtractionRun/StoryMatchingRun/PublicationRun.

Use queueing locks to suppress duplicate pending work where useful, but DB canonical constraints are the correctness boundary. Execution is at-least-once + idempotent domain application.

Facebook uses execution lock `facebook-auth-profile:<id>` for max concurrency 1. Configure explicit stalled-job recovery using Procrastinate heartbeats/stalled job support. PRE_PUBLISH is the same queue with higher priority.

## 24. Psycopg 3 async persistence boundary

Psycopg 3 async is canonical. SQLAlchemy ORM/Core is excluded from primary persistence.

Application services own transactions and pass the same `psycopg.AsyncConnection` to repositories and Procrastinate. Repositories execute explicit SQL/map rows but do not acquire their own write connection or commit.

Use `psycopg_pool.AsyncConnectionPool`, initially about `min_size=1`, `max_size=4` on the 1 GB host, then tune. Register pgvector types on each connection.

## 25. PostgreSQL resource profile

Initial recommendation for 1 GB RAM / 3 GB swap / 1 CPU:

```text
shared_buffers = 64MB
effective_cache_size = 256MB
work_mem = 2MB
hash_mem_multiplier = 1.0
maintenance_work_mem = 32MB
autovacuum_work_mem = 16MB
temp_buffers = 4MB
max_connections = 10
max_parallel_workers = 0
max_parallel_workers_per_gather = 0
max_parallel_maintenance_workers = 0
jit = off
autovacuum_max_workers = 1
```

Application pool max ~3-4. Browser-heavy Facebook collection remains low-concurrency. Swap is OOM safety, not normal working memory; low swappiness (e.g. 10) is reasonable.

## 26. SQL-first migrations

The schema is versioned application state, not a Python startup side effect.

```text
migrations/
├── 0001_extensions.sql
├── 0002_core_sources.sql
├── 0003_editions_relevance.sql
├── 0004_claims.sql
├── 0005_stories.sql
├── 0006_embeddings.sql
├── 0007_evidence.sql
├── 0008_publications.sql
└── ...
```

No ORM-generated schema and no normal-start `CREATE TABLE IF NOT EXISTS` management. Forward-only migrations; rollback is backup restore or corrective forward migration.

Use NOT NULL, CHECK, UNIQUE, partial UNIQUE, composite FK, and DEFERRABLE FK where genuinely needed. Data migrations use expansion -> backfill -> validation -> constraint tightening.

Transactional by default, with explicitly marked non-transactional migration support for rare PostgreSQL operations such as `CREATE INDEX CONCURRENTLY`.

Application startup checks compatible minimum/maximum domain schema versions and fails fast otherwise.

## 27. Procrastinate schema lifecycle

Telebrief domain migrations and Procrastinate migrations are separate. Prefer separate PostgreSQL schema/namespace for Procrastinate. Do not copy Procrastinate tables into Telebrief `000x` migrations; use official Procrastinate migrations and documented upgrade flow.

## 28. Legacy Telegram migration

Legacy `messages` is imported once into Source/SourceItem/SourceItemRevision, with explicit legacy provenance/import timestamp. Do not invent historical `collected_at` if unavailable; mark limited temporal fidelity.

After cutover: new writes only to normalized schema; legacy table read-only during validation; drop later via migration.

## 29. Publication snapshots persistent knowledge

Publication never reads providers directly. Morning digest and daily article use the same Claims/Stories and differ through publication policies.

PRE_PUBLISH accelerates normal ingestion. Example: 19:45 high-priority scans, 19:58 snapshot cutoff. A job finishing 20:03 is not an error; it simply belongs to later knowledge.

Publication never waits for queue empty. Existing knowledge publishes even if Facebook fails immediately before cutoff.

## 30. Frozen publication candidates and selection

`PublicationRun` fixes edition, type, snapshot_at, and exact policy versions.

```text
PublicationRun(snapshot)
-> deterministic eligibility
-> FROZEN candidate set
-> AI editorial selection
-> immutable selection decisions
-> FROZEN selected inputs
-> generation attempts
```

Candidate rows freeze exact StoryRevision and snapshot features. Selection decisions are INCLUDE/OMIT with presentation/rank/confidence/reason and must reference exact frozen candidate revision.

No publication stale-candidate protection: a newer StoryRevision after snapshot is intentionally excluded.

Verification is not an eligibility gate.

Responsibilities are distinct: relevance, matching, evidence, verification, eligibility, selection, writer, audit.

## 31. Publication generation attempts

`PublicationRun` is workflow/frozen state. `PublicationGenerationAttempt` is a technical attempt. `Publication` exists only after a successful attempt; no `Publication(body=NULL,status=generating)`.

Attempt kinds: writer, repair, deterministic_fallback, story_renderer_fallback.

After selected inputs are sealed, no generation path may rerun selection, add a Story, read a newer StoryRevision, expand snapshot, or call collectors.

Retries/fallbacks use identical sealed input. New snapshot/selection/policy/intentional rerun requires a new PublicationRun.

Publication is immutable and points to `winning_generation_attempt_id`. A deliberate corrected edition creates a new Publication with `supersedes_publication_id`.

## 32. Fail-open publication behavior

Preserve current `custom` behavior:

```text
Writer succeeds + light audit unavailable -> Publication
Writer unavailable + safe fallback succeeds -> Publication
```

Technical auxiliary-stage failure is not enough to suppress a useful release. Only unresolved dangerous high-risk misrepresentation can block/remove/rewrite content.

## 33. Delivery is separate

```text
Publication -> immutable DeliveryPayload -> PublicationDelivery -> immutable DeliveryAttempts
```

Publication has no Telegram/Telegraph/site-specific IDs or URLs.

Retry uses the same immutable destination payload, never AI/formatter regeneration.

Delivery statuses include `pending`, `in_progress`, `succeeded`, `failed`, `outcome_unknown`. Network timeout can be outcome_unknown; reconcile before retry to avoid duplicates, using destination idempotency where available.

Delivery failure never creates a new Publication. A site reading the same PostgreSQL does not need a fake WebsiteDelivery.

## 34. Retention

Central configurable cleanup. Suggested defaults:

```text
SourceItemRevision / Claims / Stories / StoryRevisions / Publications: indefinite
media metadata/hash/url: indefinite
vision observations: indefinite
heavy media: ~30 days default
publication/provenance media: longer/indefinite by policy
raw AI responses: ~30 days
collector/browser debug artifacts: ~7-14 days
```

Normal flows never destroy immutable source revisions. Legal/compliance/admin purge is separate explicit machinery.

## 35. Database correctness examples

At minimum enforce:

- Source identity uniqueness.
- Edition-policy consistency via composite FKs.
- Story current revision belongs to same Story.
- Claim/Story relations do not cross Editions.
- One canonical successful ClaimExtraction per revision+edition+policy.
- One canonical successful StoryMatching per claim+policy.
- Frozen candidate/selection revision consistency.
- Winning generation attempt belongs to same PublicationRun.
- Delivery payload belongs to same Publication as delivery.

Use transactional application validation where SQL cannot conveniently express the full invariant, plus strongest practical DB constraint.

## 36. Error handling

Classify by meaning:

```text
transient/timeout -> bounded retry/backoff
rate_limited -> retry later
provider/model temporary outage -> retry/failover by stage
invalid_input/impossible domain state -> permanent failure + operator visibility
Facebook auth/checkpoint/account action -> update AuthProfile, stop dependent retries, wait for human
```

Failures remain localized.

## 37. Testing

Required coverage includes:

- source identity, edits, revisions, availability state;
- Facebook auth classification/serialization/no bypass;
- honest comment completeness and deep sweep;
- edition/revision/policy-specific relevance;
- no deterministic geographic false-negative gate;
- Claim canonical idempotency and correction relations;
- exact-vector-only candidate retrieval and stale SAME_STORY rejection;
- sequential NEW_STORY matching per Edition initially;
- immutable StoryRevision semantics;
- EvidenceCluster stances and exact assessed Claim inputs;
- single useful unverified comment remains publishable;
- verification outage remains fail-open;
- historical snapshot excludes later source/derived artifacts;
- frozen publication candidate/selection inputs;
- generation retry/fallback identical sealed input;
- audit timeout publishes safe writer output;
- Publication only after successful attempt and winner consistency;
- immutable delivery payload retry + outcome_unknown reconciliation;
- atomic domain write + Procrastinate defer commit/rollback;
- duplicate at-least-once execution remains idempotent;
- schema compatibility and independent Procrastinate/domain migrations.

## 38. Migration and cutover sequence

Suggested phases:

```text
1. PostgreSQL domain schema + migrations + repositories + Procrastinate
2. Generic Source/SourceItem/Revision ingestion core
3. Telegram adapter -> persistent ingestion
4. Edition relevance + Claim extraction + embeddings
5. Story/StoryRevision + Story Matching
6. Evidence/Places/light verification
7. PublicationRun snapshot + selection + generation
8. DeliveryPayload/Delivery cutover
9. Facebook AuthProfile + sources + comments/replies/deep refresh
10. Legacy import/validation + remove direct Telegram publication collection
```

The implementation plan may reorder substeps for testability but must preserve dependency direction.

## 39. Relationship to existing Telebrief design

This architecture supersedes Telegram-centric assumptions while preserving the useful protocol/isolation intent of the existing extensibility ADR.

It also preserves the behavioral intent of the current Story Cards design: source role is not truth; community observations are legitimate reporting material; uncertainty is preserved; fact-check failure alone does not suppress a usable article; dangerous unsupported high-risk assertions remain conservative; safe degraded publication remains available.

The persistent Claim/Evidence/Story model is **not** a return to a strict claim-registry publication gate. Claims structure provenance/knowledge; they are not permission tokens.

## 40. External implementation references

Use `viseshrp/fbn` as a donor for persistent Playwright profile/session handling, bootstrap, single-profile concurrency, explicit checkpoint/CAPTCHA/account-action states, safe logging, and durable monitoring patterns.

Use `0xEnc0der/fbcli` as a donor for cookie/session handling, login/checkpoint guards, semantic/ARIA selectors, and Facebook navigation/comment-reading patterns.

Do not fork either as the Telebrief domain foundation, and do not adopt security bypass/stealth behavior.

Infrastructure references: Procrastinate, Psycopg 3 async + psycopg_pool, pgvector + pgvector-python.

## Acceptance criteria

1. Telegram and Facebook enter the same provider-neutral persistent ingestion core.
2. Publications never call provider collectors directly.
3. A relevant single Facebook comment can independently create a Story and publish with honest attribution.
4. No source-count/verification threshold is required for publication.
5. Current `custom` fail-open light-audit behavior is preserved or made more permissive, never accidentally stricter.
6. Source edits/deletions, Claim corrections, Story revisions, publication snapshots, and delivery attempts have separate immutable histories.
7. Historical replay uses only knowledge visible at the original snapshot time.
8. Story Matching freezes exact candidate revisions/embeddings and rejects stale SAME_STORY application.
9. Publication freezes candidate set and selected inputs and never reads newer knowledge during retries/fallbacks.
10. PostgreSQL constraints defend canonical semantic results against at-least-once execution.
11. Domain writes and Procrastinate scheduling can commit atomically on the same `psycopg.AsyncConnection`.
12. Facebook auth failure is localized and never becomes a CAPTCHA/checkpoint bypass or retry storm.
13. Comment collection records `complete | partial | unknown` honestly and supports incremental + deep refresh.
14. Initial architecture runs without Redis, SQLAlchemy, custom queue tables, or ANN vector indexes.
15. Legacy Telegram storage is imported into normalized schema rather than becoming the new model.
16. Delivery retries never rerun article generation.
17. Expiration of browser/debug artifacts does not destroy logical knowledge/publication replay.

## Implementation boundary

This document defines target architecture and migration constraints. Exact Python class names, SQL statements, migration filenames, and Procrastinate task signatures belong in the implementation plan after this specification is reviewed and approved.
