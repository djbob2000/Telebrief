-- 0021_event_first_pipeline.sql
-- Cost-aware Event-First processing pipeline schema (migration 21).

-- 1. Story knowledge source discriminator
ALTER TABLE stories
ADD COLUMN IF NOT EXISTS knowledge_source TEXT NOT NULL DEFAULT 'legacy_claims'
    CONSTRAINT stories_knowledge_source_check
    CHECK (knowledge_source IN ('legacy_claims', 'event_first'));

CREATE INDEX IF NOT EXISTS idx_stories_edition_source_lifecycle
ON stories(edition_id, knowledge_source, lifecycle_state);

-- 2. Deterministic source fragments
CREATE TABLE IF NOT EXISTS source_fragments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id),
    ordinal INTEGER NOT NULL,
    text_content TEXT NOT NULL,
    normalized_hash TEXT NOT NULL,
    fragmenter_version TEXT NOT NULL,
    is_candidate BOOLEAN NOT NULL,
    drop_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_item_revision_id, ordinal, fragmenter_version)
);

CREATE INDEX IF NOT EXISTS idx_source_fragments_normalized_hash
ON source_fragments(normalized_hash);

CREATE INDEX IF NOT EXISTS idx_source_fragments_candidate
ON source_fragments(is_candidate) WHERE is_candidate = TRUE;

-- 3. Reusable deduplicated fragment embedding vectors
CREATE TABLE IF NOT EXISTS fragment_embedding_vectors (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_hash TEXT NOT NULL,
    embedding VECTOR NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (normalized_hash, model, dimensions)
);

CREATE TABLE IF NOT EXISTS source_fragment_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fragment_id BIGINT NOT NULL UNIQUE REFERENCES source_fragments(id),
    vector_id BIGINT NOT NULL REFERENCES fragment_embedding_vectors(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. Embedding provider batch audit
CREATE TABLE IF NOT EXISTS event_embedding_batches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    input_chars BIGINT NOT NULL CHECK (input_chars >= 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error_kind TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);

-- 5. Story fragment membership
CREATE TABLE IF NOT EXISTS story_fragments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id BIGINT NOT NULL REFERENCES stories(id),
    fragment_id BIGINT NOT NULL REFERENCES source_fragments(id),
    fragment_embedding_id BIGINT NOT NULL REFERENCES source_fragment_embeddings(id),
    assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('new_story', 'vector_join', 'manual')),
    similarity FLOAT8 NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fragment_id)
);

CREATE INDEX IF NOT EXISTS idx_story_fragments_story_id ON story_fragments(story_id);

-- 6. Operational Story cluster state
CREATE TABLE IF NOT EXISTS story_cluster_state (
    story_id BIGINT PRIMARY KEY REFERENCES stories(id),
    centroid VECTOR NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    fragment_count INTEGER NOT NULL,
    unique_source_count INTEGER NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    latest_assignment_id BIGINT NOT NULL REFERENCES story_fragments(id),
    last_analyzed_assignment_id BIGINT NULL REFERENCES story_fragments(id),
    last_analyzed_at TIMESTAMPTZ NULL,
    analysis_dirty BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_cluster_state_last_seen
ON story_cluster_state(last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_story_cluster_state_analysis_dirty
ON story_cluster_state(analysis_dirty) WHERE analysis_dirty = TRUE;

-- 7. Auditable Story triage runs and decisions
CREATE TABLE IF NOT EXISTS story_event_triage_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    triage_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    story_count INTEGER NOT NULL CHECK (story_count > 0),
    input_chars BIGINT NOT NULL CHECK (input_chars >= 0),
    output_chars BIGINT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error_kind TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS story_event_triage_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES story_event_triage_runs(id),
    story_id BIGINT NOT NULL REFERENCES stories(id),
    latest_assignment_id BIGINT NOT NULL REFERENCES story_fragments(id),
    triage_version TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ANALYZE', 'IGNORE')),
    exclusion_reason TEXT NULL CHECK (
        exclusion_reason IS NULL
        OR exclusion_reason IN ('commercial_classified', 'obvious_noise')
    ),
    confidence FLOAT8 NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (story_id, latest_assignment_id, triage_version)
);

-- 8. Auditable rich Story-analysis calls
CREATE TABLE IF NOT EXISTS story_event_analysis_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id BIGINT NOT NULL REFERENCES stories(id),
    latest_assignment_id BIGINT NOT NULL REFERENCES story_fragments(id),
    analysis_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_fragment_count INTEGER NOT NULL,
    input_chars BIGINT NOT NULL,
    output_chars BIGINT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error_kind TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_analysis_success_assignment
ON story_event_analysis_runs(story_id, latest_assignment_id, analysis_version)
WHERE status = 'succeeded';

-- 9. Rich event payload on story revisions
ALTER TABLE story_revisions
ADD COLUMN IF NOT EXISTS event_payload JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 10. Frozen publication fragment provenance
CREATE TABLE IF NOT EXISTS publication_input_fragments (
    publication_input_id BIGINT NOT NULL REFERENCES publication_inputs(id),
    fragment_id BIGINT NOT NULL REFERENCES source_fragments(id),
    source_snapshot JSONB NOT NULL,
    PRIMARY KEY (publication_input_id, fragment_id)
);
