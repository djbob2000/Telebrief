-- 0006_embeddings_stories.sql
-- Plan 3 Task 5: immutable semantic embeddings (claims + story revisions)
-- and the story aggregate (stories / story_revisions / state events /
-- relations) plus story-matching scaffolding consumed by Task 7.
--
-- Design choices where the plan leaves freedom:
--   * embedding VECTOR columns carry NO typmod: pgvector requires a fixed
--     dimensionality per typmod, which would freeze one vector space per
--     column. Model changes must create NEW rows in the SAME table (never
--     mutate old ones), so each row self-describes via the `dimensions`
--     INTEGER column and every read filters model + dimensions before any
--     `<=>` distance is evaluated. Exact scans tolerate heterogeneous
--     dimensionality; ANN indexes (ivfflat/hnsw) would not — and this plan
--     mandates exact retrieval only.
--   * UNIQUE (object_id, model, dimensions, purpose, content_hash) encodes
--     the "one immutable row per semantic object + vector space" invariant;
--     writers use ON CONFLICT DO NOTHING and re-read the winner, so the
--     table stays append-only forever.
--   * stories.current_revision_id is a composite DEFERRABLE FK
--     (id, current_revision_id) -> story_revisions(story_id, id): the
--     pointer can only ever reference a revision OF THE SAME STORY, and a
--     story may exist briefly without a pointer (NULL) while its first
--     revision is inserted in the same transaction.
--   * story_matching_runs carries the spec's partial unique index
--     uq_story_match_success ON (claim_id, policy_id) WHERE status =
--     'succeeded' — at most one successful match per (claim, policy),
--     mirroring uq_claim_extraction_success.
--   * story_match_decisions is UNIQUE per run: one matcher verdict per run.
--   * story_relations / story_relation_proposals / story_state_events /
--     story_matching_candidates are append-only history; no UPDATE paths.

-- ---------------------------------------------------------------------------
-- Immutable claim embeddings
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claim_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    embedding VECTOR NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL
        CONSTRAINT claim_embeddings_dimensions_check CHECK (dimensions > 0),
    purpose TEXT NOT NULL
        CONSTRAINT claim_embeddings_purpose_check
        CHECK (purpose IN ('claim_query', 'story_document')),
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_claim_embedding_space
        UNIQUE (claim_id, model, dimensions, purpose, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_claim_embeddings_claim
ON claim_embeddings(claim_id);

-- ---------------------------------------------------------------------------
-- Story aggregate
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stories (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    current_revision_id BIGINT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'candidate'
        CONSTRAINT stories_lifecycle_state_check
        CHECK (lifecycle_state IN ('candidate', 'active', 'resolved', 'reopened', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stories_edition_lifecycle
ON stories(edition_id, lifecycle_state);

CREATE TABLE IF NOT EXISTS story_revisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id BIGINT NOT NULL REFERENCES stories(id),
    revision_no INTEGER NOT NULL,
    title TEXT NULL,
    summary TEXT NULL,
    current_state TEXT NOT NULL,
    semantic_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_story_revisions_story_no UNIQUE (story_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_story_revisions_story
ON story_revisions(story_id);

-- Referential target for the composite pointer FK below.
CREATE UNIQUE INDEX IF NOT EXISTS uq_story_revisions_story_id
ON story_revisions(story_id, id);

ALTER TABLE stories
    DROP CONSTRAINT IF EXISTS fk_stories_current_revision;

-- current_revision_id must reference a revision OF THE SAME STORY.
-- DEFERRABLE so a story and its first revision can be created in one
-- transaction with the pointer set after the revision insert.
ALTER TABLE stories
    ADD CONSTRAINT fk_stories_current_revision
    FOREIGN KEY (id, current_revision_id)
    REFERENCES story_revisions(story_id, id)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE TABLE IF NOT EXISTS story_revision_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_revision_id BIGINT NOT NULL REFERENCES story_revisions(id),
    embedding VECTOR NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL
        CONSTRAINT story_revision_embeddings_dimensions_check CHECK (dimensions > 0),
    purpose TEXT NOT NULL
        CONSTRAINT story_revision_embeddings_purpose_check
        CHECK (purpose IN ('claim_query', 'story_document')),
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_story_revision_embedding_space
        UNIQUE (story_revision_id, model, dimensions, purpose, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_story_revision_embeddings_revision
ON story_revision_embeddings(story_revision_id);

CREATE TABLE IF NOT EXISTS story_state_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id BIGINT NOT NULL REFERENCES stories(id),
    type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_state_events_story
ON story_state_events(story_id);

-- Immutable cross-story relation graph (append-only).
CREATE TABLE IF NOT EXISTS story_relations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_story_id BIGINT NOT NULL REFERENCES stories(id),
    to_story_id BIGINT NOT NULL REFERENCES stories(id),
    relation_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_relations_from ON story_relations(from_story_id);
CREATE INDEX IF NOT EXISTS idx_story_relations_to ON story_relations(to_story_id);

-- ---------------------------------------------------------------------------
-- Story matching scaffolding (consumed by Plan 3 Task 7)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS story_matching_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    vector_limit INTEGER NOT NULL DEFAULT 20,
    lexical_limit INTEGER NOT NULL DEFAULT 10,
    state_fallback_limit INTEGER NOT NULL DEFAULT 20,
    total_candidate_limit INTEGER NOT NULL DEFAULT 40,
    resolved_lookback_days INTEGER NOT NULL DEFAULT 30,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_story_matching_policy_edition_version UNIQUE (edition_id, version)
);

-- Composite FK target for edition-consistent policy references.
CREATE UNIQUE INDEX IF NOT EXISTS uq_story_matching_policy_id_edition
ON story_matching_policy_versions(id, edition_id);

CREATE TABLE IF NOT EXISTS story_matching_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    policy_id BIGINT NOT NULL,
    claim_embedding_id BIGINT NULL REFERENCES claim_embeddings(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL
        CONSTRAINT story_matching_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed', 'stale')),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_story_matching_runs_policy
        FOREIGN KEY (policy_id, edition_id)
        REFERENCES story_matching_policy_versions(id, edition_id)
);

-- Canonical matching invariant: at most one successful match per
-- (claim, policy); failed/stale runs never occupy the slot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_story_match_success
ON story_matching_runs(claim_id, policy_id)
WHERE status = 'succeeded';

CREATE INDEX IF NOT EXISTS idx_story_matching_runs_claim
ON story_matching_runs(claim_id);

CREATE TABLE IF NOT EXISTS story_matching_candidates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES story_matching_runs(id),
    story_id BIGINT NOT NULL REFERENCES stories(id),
    story_revision_id BIGINT NOT NULL REFERENCES story_revisions(id),
    story_revision_embedding_id BIGINT NULL REFERENCES story_revision_embeddings(id),
    retrieved_by_vector BOOLEAN NOT NULL DEFAULT false,
    retrieved_by_lexical BOOLEAN NOT NULL DEFAULT false,
    retrieved_by_state BOOLEAN NOT NULL DEFAULT false,
    vector_distance FLOAT8 NULL,
    lexical_score FLOAT8 NULL,
    location_overlap FLOAT8 NULL,
    entity_overlap FLOAT8 NULL,
    time_score FLOAT8 NULL,
    status_score FLOAT8 NULL,
    rank INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_matching_candidates_run
ON story_matching_candidates(run_id);

-- One matcher verdict per run (UNIQUE run_id: the "UNIQUE-ish" ruling).
CREATE TABLE IF NOT EXISTS story_match_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES story_matching_runs(id),
    assignment TEXT NOT NULL
        CONSTRAINT story_match_decisions_assignment_check
        CHECK (assignment IN ('SAME_STORY', 'NEW_STORY')),
    target_story_id BIGINT NULL REFERENCES stories(id),
    story_update JSONB NULL,
    confidence NUMERIC NULL,
    reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_story_match_decision_run
ON story_match_decisions(run_id);

-- Append-only matcher-proposed relations awaiting confirmation.
CREATE TABLE IF NOT EXISTS story_relation_proposals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES story_matching_runs(id),
    from_story_id BIGINT NOT NULL REFERENCES stories(id),
    to_story_id BIGINT NOT NULL REFERENCES stories(id),
    relation_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_relation_proposals_run
ON story_relation_proposals(run_id);
