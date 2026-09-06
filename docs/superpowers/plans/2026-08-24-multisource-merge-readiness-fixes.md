# Multisource Merge Readiness Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all 3 Critical and 4 Important review findings plus documentation gap on the `multisource-roadmap` branch to achieve full `merge-ready` status with green PostgreSQL tests.

**Architecture:** 
1. Fix PostgreSQL SQL syntax and column bindings for `story_state_events` (`sse.type`, `sse.observed_at`) in `src/publication/repository.py` and test fixtures.
2. Propagate and respect `processing_mode` in `PlaceResolutionService` -> `StoryMatchingPrerequisiteService` to unblock `knowledge_no_embeddings` place resolution.
3. Fix enum attributes and profile lookup in `scripts/bootstrap_facebook_profile.py`.
4. Eliminate unsafe post ID hijacking of comment identity, capture comment `author_name` during DOM scanning, and scope synthetic fingerprints to author.
5. Strict isolation of disabled editorial platforms from `last_activity_at` and `has_recent_revision` candidate eligibility.
6. Guard Facebook deep comment refreshes against disabled sources in both DB sweep query and queued task execution.
7. Enrich `KnowledgeEditorialAdapter` attribution for comments with author names and container context.
8. Update `config.yaml.example` with `editorial_enabled`.

**Tech Stack:** Python 3.11+, PostgreSQL 16+ (pgvector), Playwright, Procrastinate, Pytest.

---

### Task 1: Fix Historical Lifecycle Candidate Query and Valid Test Setup (Critical Issue 1)

**Files:**
- Modify: `src/publication/repository.py:370-495`
- Modify: `tests/publication/test_facebook_editorial_enabled.py`

- [ ] **Step 1: Fix `eligible_story_revisions` SQL in `src/publication/repository.py`**
  - Replace `sse.to_state` with `sse.type` in the subquery selecting historical state at or before `snapshot_at`.
  - Use `sse.observed_at <= %s` for point-in-time lifecycle ordering and filtering.
  - Use `sse2.observed_at >= %s AND sse2.observed_at <= %s` in `has_recent_event`.
  - Use `sse.observed_at <= %s` in `last_activity_at` union.

- [ ] **Step 2: Fix `tests/publication/test_facebook_editorial_enabled.py`**
  - Fix test setup helper `_setup_sources_and_claims`:
    - Insert `source_items` with required `kind='msg'` and `first_collected_at=_PAST`.
    - Insert `relevance_policy_versions` + `edition_relevance_decisions`.
    - Insert `claim_extraction_policy_versions` + `claim_extraction_runs`.
    - Insert `claims` with valid `claim_extraction_run_id`, `source_item_revision_id`, `edition_id`, `assertion_text`, `normalized_assertion`, `created_at`.
  - Fix `story_state_events` inserts in `test_facebook_editorial_enabled.py`:
    - Replace `from_state, to_state` with `type, observed_at, reason, created_at`.

- [ ] **Step 3: Run pytest on `test_facebook_editorial_enabled.py`**
  - Verify all test cases in `test_facebook_editorial_enabled.py` pass on PostgreSQL.

---

### Task 2: Unblock `knowledge_no_embeddings` for Claims with `place_mentions` (Critical Issue 2)

**Files:**
- Modify: `src/processing/places.py:238-356`
- Modify: `src/jobs/processing.py:242-250`
- Modify: `tests/processing/test_story_matching.py`

- [ ] **Step 1: Accept `processing_mode` in `PlaceResolutionService`**
  - In `src/processing/places.py`:
    - Add `processing_mode: str = "knowledge_full"` and `prerequisites: StoryMatchingPrerequisiteService | None = None` to `PlaceResolutionService.__init__`.
    - In `_maybe_schedule_matching(conn, mention)`, construct or use `StoryMatchingPrerequisiteService(processing_mode=self._processing_mode)`.
- [ ] **Step 2: Update `build_place_resolution_service()` in `src/jobs/processing.py`**
  - Read `processing_mode` from config (`telegram.processing_mode` or default `"knowledge_full"`) and pass to `PlaceResolutionService`.
- [ ] **Step 3: Add end-to-end regression test in `tests/processing/test_story_matching.py`**
  - Test flow:
    1. Create Claim in edition with `knowledge_no_embeddings`.
    2. Add `claim_place_mentions` row to the claim.
    3. Run `PlaceResolutionService(uow=uow, processing_mode="knowledge_no_embeddings").resolve_mention(mention_id, policy_id)`.
    4. Assert `match_claim` is deferred in Procrastinate queue with `claim_embedding_id=None`.
    5. Run `StoryMatchingService.run(claim.id, policy.id, claim_embedding_id=None)` and assert match/story assignment succeeds with lexical retrieval mode.

- [ ] **Step 4: Run pytest on `tests/processing/test_story_matching.py` and `tests/processing/test_places.py`**

---

### Task 3: Fix Facebook Bootstrap Script Enum Members and Profile Resolution (Critical Issue 3)

**Files:**
- Modify: `scripts/bootstrap_facebook_profile.py`
- Modify: `tests/providers/facebook/test_browser_state.py` or new script test

- [ ] **Step 1: Fix `FacebookAuthState` member checks**
  - In `scripts/bootstrap_facebook_profile.py`:
    - Replace invalid `.CHECKPOINT`, `.BLOCKED`, `.ACTION_REQUIRED` with `FacebookAuthState.CHECKPOINT_REQUIRED`, `FacebookAuthState.ACCOUNT_ACTION_REQUIRED`, `FacebookAuthState.DISABLED`.
    - Handle `FacebookAuthState.AUTH_REQUIRED` as non-fatal waiting state ("Waiting for login completion...").
- [ ] **Step 2: Support configured profile resolution by name or storage_ref**
  - Load config and find matching `FacebookAuthProfileBootstrap` by `p.name == profile_arg` or `p.storage_ref == profile_arg`.
  - Use `name` and `storage_ref` from config when updating `facebook_auth_profiles` in database.
- [ ] **Step 3: Add unit tests verifying `bootstrap_facebook_profile.py` logic**

---

### Task 4: Fix Comment Identity and Capture Author Name (Important Issue 4)

**Files:**
- Modify: `src/providers/facebook/comments.py`
- Modify: `src/providers/facebook/collector.py`
- Modify: `tests/providers/facebook/test_collector.py`
- Modify: `tests/providers/facebook/test_comments.py`

- [ ] **Step 1: Remove `extract_post_id_from_url` as comment ID source**
  - In `src/providers/facebook/comments.py`:
    - Only `extract_comment_id_from_url()` provides native `comment_id`.
    - Do NOT call `extract_post_id_from_url()` on links inside comment nodes.
- [ ] **Step 2: Extract `author_name` during DOM scanning**
  - Scan links in comment node to extract `author_name` (e.g. from user profile anchor text).
  - Pass `author_name` into `parse_comment_from_data(..., author_name=author_name)`.
- [ ] **Step 3: Update synthetic fingerprint to include `author_name`**
  - Fingerprint: `f"{post_external_id}:{parent_comment_id or ''}:{author_name or ''}:{norm_text}"`.
- [ ] **Step 4: Add tests for comment identity discrimination and author capture**
  - Test that two identical texts "Да" by different authors produce distinct synthetic comment IDs.
  - Test that profile links containing numbers are not mistakenly parsed as native comment IDs.

---

### Task 5: Strictly Isolate `facebook.editorial_enabled=false` from Eligibility/Ranking (Important Issue 5)

**Files:**
- Modify: `src/publication/repository.py:370-455`
- Modify: `tests/publication/test_facebook_editorial_enabled.py`

- [ ] **Step 1: Gate `has_recent_revision` and `last_activity_at` on platform exclusions**
  - In `src/publication/repository.py:eligible_story_revisions()`:
    - Set `has_recent_revision` to `false` when `cardinality(%s::text[]) > 0` (`cardinality(excluded_platforms) > 0`).
    - In `last_activity_at`, only include `lr.revision_created_at` when `cardinality(%s::text[]) = 0`.
- [ ] **Step 2: Add regression test for the scenario in `test_facebook_editorial_enabled.py`**
  - Test: Telegram Claim was created 2 days ago (outside recent window). Facebook Claim created today (inside recent window) updates semantic `StoryRevision`. `facebook.editorial_enabled=False`.
  - Assert that this Story is NOT marked eligible and is NOT included as a candidate in today's run.

---

### Task 6: Prevent Deep Comment Refreshes on Disabled Sources (Important Issue 6)

**Files:**
- Modify: `src/repositories/facebook.py:292-320`
- Modify: `src/jobs/facebook.py:62-80`
- Modify: `tests/jobs/test_facebook_jobs.py` / `tests/repositories/test_facebook_repository.py`

- [ ] **Step 1: Add `AND s.enabled = true` to `list_posts_due_for_deep_refresh`**
  - In `src/repositories/facebook.py`, update SQL join `JOIN sources s ON s.id = si.source_id AND s.platform = 'facebook' AND s.enabled = true`.
- [ ] **Step 2: Add `if not source.enabled: return` in `refresh_facebook_comments`**
  - In `src/jobs/facebook.py`, after loading `source = Source.from_row(...)`, check `if not source.enabled: logger.info(...); return`.
- [ ] **Step 3: Add tests verifying disabled sources are skipped by deep refresh query and execution**

---

### Task 7: Preserve Comment Author Attribution in `KnowledgeEditorialAdapter` (Important Issue 7)

**Files:**
- Modify: `src/publication/editorial_adapter.py:195-310`
- Modify: `tests/publication/test_editorial_adapter.py`

- [ ] **Step 1: Retrieve `si.kind` and `si.author_name` in `editorial_adapter.py` query**
  - Update `SELECT ... si.kind, si.author_name ... FROM claims c ... JOIN source_items si ...`.
- [ ] **Step 2: Build accurate attribution for comments**
  - For comments (`kind in ('comment', 'facebook_comment')` or when `author_name` is present):
    - `author_label = author_name or "участник сообщества"`
    - `container_label = src_name or platform`
    - `attribution = f"{author_label} ({container_label})"`
    - Set `sender = f"{author_label} ({container_label})"` and `source_type = "comment"`.
- [ ] **Step 3: Update `test_editorial_adapter.py` to assert comment attribution format**

---

### Task 8: Document `facebook.editorial_enabled` in `config.yaml.example`

**Files:**
- Modify: `config.yaml.example:215-225`

- [ ] **Step 1: Add `editorial_enabled: true` under `facebook:` section in `config.yaml.example`**

---

### Task 9: Full Test Suite Verification

- [ ] **Step 1: Run all test suites across the repository**
  - `pytest tests/publication/ -v`
  - `pytest tests/processing/ -v`
  - `pytest tests/jobs/ -v`
  - `pytest tests/providers/facebook/ -v`
  - `pytest tests/integration/ -v`
  - `pytest tests/db/ -v`
