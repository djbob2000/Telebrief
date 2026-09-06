# Telebrief Multisource Architecture Implementation Roadmap

> **For agentic workers:** Execute the detailed plans in order. Each detailed plan is a reviewable, independently testable delivery boundary. Do not skip ahead when an earlier plan's interfaces are not merged.

**Goal:** Migrate Telebrief from direct Telegram collection/publication to the approved persistent multisource knowledge architecture without a big-bang rewrite.

**Architecture:** Build the new PostgreSQL/Procrastinate foundation beside the current `custom` path, migrate Telegram into persistent ingestion, add the edition knowledge pipeline, cut publication over to frozen knowledge snapshots, then add Facebook and retire the legacy Telegram-centric path. Existing permissive editorial behavior is a compatibility requirement throughout.

**Tech Stack:** Python 3.14+, PostgreSQL 18, pgvector, psycopg 3 async, psycopg_pool, Procrastinate, Telethon, Playwright (Facebook phase), existing AI provider cascade.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- Preserve current `custom` publication-first behavior: unverified content is not blocked merely because it is single-source or unofficial.
- PostgreSQL + pgvector are the canonical persistence layer; no SQLite branch for the new domain.
- Psycopg 3 async is the canonical data-access layer; repositories never own commits.
- Procrastinate owns durable orchestration; do not add a Telebrief `processing_jobs` table, leases, or a `SKIP LOCKED` worker framework.
- Domain write + scheduling of the next Procrastinate job must be atomic when the next stage is required.
- `SourceItemRevision`, Claims, StoryRevisions, Evidence assessments, Publication inputs, Publications, and Delivery attempts are immutable history.
- No source-count threshold may become a publication gate.
- Facebook security checkpoints/CAPTCHA/account-action states are hard stops requiring manual action; no bypass behavior.
- No ANN vector index in the initial implementation; use exact pgvector retrieval.
- The old Telegram `messages` table is legacy input for a one-time importer, not the schema to evolve into the new domain.

---

## Plan 1 — Persistence and durable orchestration foundation

**File:** `docs/superpowers/plans/2026-08-22-multisource-foundation-persistence-orchestration.md`

Delivers:

- Psycopg 3 async pool and pgvector registration.
- SQL-first migration runner and schema compatibility checks.
- Initial `sources`, `editions`, source-edition bindings, and collection bookkeeping schema; semantic policy-version tables begin in Plan 3 so they are defined only once.
- Procrastinate in its own PostgreSQL schema.
- Shared-transaction `domain write + defer_async(connection=conn)` proof.
- Thin Source/Edition repositories.
- Application bootstrap that opens/closes DB and Procrastinate infrastructure without changing current digest/article behavior yet.

**Exit gate:** Existing digest/article tests still pass; Postgres integration tests prove migration, constraints, and atomic job defer.

## Plan 2 — Generic ingestion and Telegram cutover

**File:** `docs/superpowers/plans/2026-08-22-multisource-ingestion-telegram.md`

Delivers:

- Provider-neutral Collector protocol and collection batch types.
- `SourceItem`, immutable `SourceItemRevision`, SourceAssets, state events, checkpoints, CollectionRuns.
- `IngestionService` as transaction owner.
- Telegram adapter producing provider-neutral observations.
- DB-backed Source registry bootstrapped from current Telegram config.
- Procrastinate scheduled/incremental Telegram collection, a single provider-aware scan-enqueue helper, and the `PRE_PUBLISH` priority path used later by publication scheduling.
- Read adapter that can reconstruct legacy `Message` objects from new persisted revisions during compatibility period.

**Exit gate:** Telegram collection persists once and downstream callers can read the same data without invoking Telethon again; current publication path can temporarily consume the compatibility reader.

## Plan 3 — Edition knowledge: relevance, Claims, embeddings, Stories, Places, evidence

**File:** `docs/superpowers/plans/2026-08-22-knowledge-story-evidence.md`

Delivers:

- Immutable versioned relevance decisions.
- ClaimExtractionPolicy/Run/attempts and immutable Claims.
- Claim semantic relations/state events.
- Claim and StoryRevision embeddings with exact pgvector retrieval.
- Persistent Story lifecycle, meaningful StoryRevision creation, StoryMatchingRun/candidates/decision/stale protection.
- Place mentions + versioned resolution + simple containment; lightweight entity mentions.
- EvidenceAssessmentRun + immutable EvidenceClusters.
- Optional lightweight VerificationAssessment that is never required for publication.

**Exit gate:** A persisted Telegram item can flow through relevance → Claim → Story → evidence, including a single community report with no confirmation.

## Plan 4 — Frozen publication snapshots and destination-independent delivery

**File:** `docs/superpowers/plans/2026-08-22-publication-snapshot-delivery.md`

Delivers:

- Publication policy versions and PublicationRun lifecycle.
- Deterministic eligibility, frozen candidate set, immutable AI selection decisions, sealed selected inputs.
- Generation attempts using only sealed input.
- Adapter from frozen knowledge inputs into the existing Story Cards/writer/light-audit pipeline while preserving fail-open behavior.
- Immutable Publication with winning attempt.
- Immutable delivery payloads, deliveries, attempts, `outcome_unknown` reconciliation.
- Procrastinate-owned publication clock with pre-publish lead refresh, digest/article and on-demand paths cut over to knowledge snapshots, and direct provider reads removed from publication code.

**Exit gate:** Scheduled/on-demand publication works with provider collectors offline, using already-persisted knowledge; retrying delivery never regenerates content.

## Plan 5 — Facebook provider, comments, legacy import, and final cutover

**File:** `docs/superpowers/plans/2026-08-22-facebook-and-legacy-cutover.md`

Delivers:

- FacebookSourceConfig and FacebookAuthProfile with `storage_ref` and operational state.
- Persistent Playwright profile bootstrap/verification and Procrastinate auth-profile execution locks.
- Explicit Facebook source scans.
- Completeness-oriented comments/replies collection with `complete|partial|unknown`, coverage state, incremental refresh, periodic deep sweep, and `Most relevant` fallback metadata.
- CollectorArtifact diagnostics and retention hooks.
- Legacy `messages` one-time importer with limited temporal fidelity flags.
- Remove direct Telegram collection from publication path, retire old storage writes, validate, then drop legacy table in a later forward migration.
- Production stalled-job recovery and retention cleanup tasks.

**Exit gate:** Telegram + Facebook use the same persistent core; a single relevant Facebook comment can create/publish an attributed Story; legacy direct publication collection path is gone.

## Program-level verification after every plan

Run at least:

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
```

PostgreSQL/Procrastinate integration tests are environment-gated but mandatory in CI once Plan 1 lands. Every plan ends with a full suite run before merge.
