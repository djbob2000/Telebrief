-- 0005_relevance_claims.sql
-- Plan 3 Task 1: edition-scoped relevance decisions, vision/claim-extraction
-- policy scaffolding, processing attempts audit, and immutable Claims with
-- relations and state events (spec §12-16).
--
-- Design choices where the plan leaves freedom:
--   * Every semantic table carries edition_id so policy references use
--     COMPOSITE FKs (policy_id, edition_id) -> policy(id, edition_id); a row
--     can never reference another edition's policy. The composite targets are
--     backed by UNIQUE indexes on the policy tables.
--   * vision_analysis_runs gains an explicit NOT NULL edition_id column (the
--     task's column list omitted it, but the mandated composite policy FK is
--     impossible without it).
--   * claim_extraction_runs.relevance_decision_id is NOT NULL and guarded by
--     a composite FK (relevance_decision_id, edition_id) -> decisions(id,
--     edition_id), enforcing the spec §15 chain decision -> run inside one
--     edition. Requires UNIQUE (id, edition_id) on decisions.
--   * reason TEXT on decisions is NOT NULL: fail-open outcomes must always
--     explain themselves; provider/model stay nullable (deterministic or
--     fallback paths).
--   * claims.assertion_text / normalized_assertion are NOT NULL: an immutable
--   * assertion without text has no meaning, and normalized_assertion is the
--     canonical embedding input downstream.
--   * processing_attempts has NO FK to any run table: stage discriminates the
--     semantic target polymorphically; it is audit history, not a queue, and
--     PRIMARY KEY (stage, semantic_run_id, attempt_no) is its only invariant.
--   * Immutable history everywhere: writers INSERT only. The documented
--     exceptions are editions.current_relevance_policy_id (pointer) and
--     run status transitions (running -> succeeded|failed|unavailable).
--   * No prompt/config content is hard-coded here; policy rows are created by
--     application code (Task 2+ ensure_current services).

ALTER TABLE editions
    ADD COLUMN IF NOT EXISTS current_relevance_policy_id BIGINT NULL;

CREATE TABLE IF NOT EXISTS relevance_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_relevance_policy_edition_version UNIQUE (edition_id, version)
);

-- Referential target for composite FKs (policy id, owning edition).
CREATE UNIQUE INDEX IF NOT EXISTS uq_relevance_policy_id_edition
ON relevance_policy_versions(id, edition_id);

-- Edition pointer consistency: the current relevance policy of an edition
-- must itself belong to that edition. NULL permitted (no policy chosen yet).
ALTER TABLE editions
    ADD CONSTRAINT fk_editions_current_relevance_policy
    FOREIGN KEY (current_relevance_policy_id, id)
    REFERENCES relevance_policy_versions(id, edition_id);

CREATE TABLE IF NOT EXISTS edition_relevance_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    relevance_policy_id BIGINT NOT NULL,
    status TEXT NOT NULL
        CONSTRAINT edition_relevance_decisions_status_check
        CHECK (status IN ('relevant', 'irrelevant', 'uncertain', 'needs_media')),
    confidence NUMERIC NULL,
    reason TEXT NOT NULL,
    provider TEXT NULL,
    model TEXT NULL,
    parent_decision_id BIGINT NULL REFERENCES edition_relevance_decisions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Edition-consistent policy reference (see uq_relevance_policy_id_edition).
    CONSTRAINT fk_edition_relevance_decisions_policy
        FOREIGN KEY (relevance_policy_id, edition_id)
        REFERENCES relevance_policy_versions(id, edition_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_root_relevance_decision
ON edition_relevance_decisions(source_item_revision_id, edition_id, relevance_policy_id)
WHERE parent_decision_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_edition_relevance_decisions_id_edition
ON edition_relevance_decisions(id, edition_id);

-- Decision lookups by exact revision + edition (latest-for-revision queries).
CREATE INDEX IF NOT EXISTS idx_edition_relevance_decisions_revision_edition
ON edition_relevance_decisions(source_item_revision_id, edition_id);

CREATE TABLE IF NOT EXISTS vision_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    version INTEGER NOT NULL,
    mode TEXT NOT NULL
        CONSTRAINT vision_policy_versions_mode_check
        CHECK (mode IN ('off', 'relevance_only', 'full')),
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_vision_policy_edition_version UNIQUE (edition_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vision_policy_id_edition
ON vision_policy_versions(id, edition_id);

CREATE TABLE IF NOT EXISTS vision_analysis_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    relevance_decision_id BIGINT NULL REFERENCES edition_relevance_decisions(id),
    policy_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL
        CONSTRAINT vision_analysis_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed', 'unavailable')),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_vision_analysis_runs_policy
        FOREIGN KEY (policy_id, edition_id)
        REFERENCES vision_policy_versions(id, edition_id)
);

CREATE INDEX IF NOT EXISTS idx_vision_analysis_runs_revision_edition
ON vision_analysis_runs(source_item_revision_id, edition_id);

-- Derived provenance artifacts: never replace raw source text; append-only.
CREATE TABLE IF NOT EXISTS vision_observations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vision_run_id BIGINT NOT NULL REFERENCES vision_analysis_runs(id),
    source_asset_id BIGINT NULL REFERENCES source_assets(id),
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    kind TEXT NOT NULL,
    text TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vision_observations_run
ON vision_observations(vision_run_id);

CREATE TABLE IF NOT EXISTS claim_extraction_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_claim_extraction_policy_edition_version UNIQUE (edition_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_extraction_policy_id_edition
ON claim_extraction_policy_versions(id, edition_id);

CREATE TABLE IF NOT EXISTS claim_extraction_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    extraction_policy_id BIGINT NOT NULL,
    relevance_decision_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL
        CONSTRAINT claim_extraction_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed')),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_claim_extraction_runs_policy
        FOREIGN KEY (extraction_policy_id, edition_id)
        REFERENCES claim_extraction_policy_versions(id, edition_id),
    CONSTRAINT fk_claim_extraction_runs_decision_edition
        FOREIGN KEY (relevance_decision_id, edition_id)
        REFERENCES edition_relevance_decisions(id, edition_id)
);

-- THE canonical ClaimExtraction invariant (spec §15): at most one successful
-- run per (revision, edition, extraction policy). Failed runs never occupy
-- the slot; a new policy version starts a new canonical key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_extraction_success
ON claim_extraction_runs(source_item_revision_id, edition_id, extraction_policy_id)
WHERE status = 'succeeded';

CREATE INDEX IF NOT EXISTS idx_claim_extraction_runs_revision_edition
ON claim_extraction_runs(source_item_revision_id, edition_id);

-- Operational audit history for every semantic stage; no FK by design
-- (stage discriminates the target run table). Never used as a queue.
CREATE TABLE IF NOT EXISTS processing_attempts (
    stage TEXT NOT NULL,
    semantic_run_id BIGINT NOT NULL,
    attempt_no INTEGER NOT NULL,
    provider TEXT NULL,
    model TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL
        CONSTRAINT processing_attempts_status_check
        CHECK (status IN ('running', 'succeeded', 'failed', 'unavailable')),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (stage, semantic_run_id, attempt_no)
);

-- Immutable source-bound assertions (spec §15/§16 temporal model).
CREATE TABLE IF NOT EXISTS claims (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_extraction_run_id BIGINT NOT NULL REFERENCES claim_extraction_runs(id),
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    assertion_text TEXT NOT NULL,
    normalized_assertion TEXT NOT NULL,
    event_time_start TIMESTAMPTZ NULL,
    event_time_end TIMESTAMPTZ NULL,
    event_time_precision TEXT NULL,
    event_time_confidence NUMERIC NULL,
    event_time_original_text TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claims_extraction_run ON claims(claim_extraction_run_id);
CREATE INDEX IF NOT EXISTS idx_claims_revision_edition
ON claims(source_item_revision_id, edition_id);

CREATE TABLE IF NOT EXISTS claim_relations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_claim_id BIGINT NOT NULL REFERENCES claims(id),
    to_claim_id BIGINT NOT NULL REFERENCES claims(id),
    relation_type TEXT NOT NULL
        CONSTRAINT claim_relations_type_check
        CHECK (relation_type IN ('CORRECTS', 'SUPERSEDES', 'RETRACTS')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_relations_from ON claim_relations(from_claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_relations_to ON claim_relations(to_claim_id);

-- Operational state cache; every transition must be reconstructable from the
-- immutable relations above plus these events (append-only).
CREATE TABLE IF NOT EXISTS claim_state_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_state_events_claim ON claim_state_events(claim_id);
