-- Durable Event-First execution guards and provenance (migration 27).

ALTER TABLE source_item_revisions
    ADD COLUMN IF NOT EXISTS collection_run_id BIGINT NULL
        REFERENCES collection_runs(id);

CREATE INDEX IF NOT EXISTS idx_source_item_revisions_collection_run
    ON source_item_revisions(collection_run_id)
    WHERE collection_run_id IS NOT NULL;

ALTER TABLE story_revisions
    ADD COLUMN IF NOT EXISTS event_assignment_id BIGINT NULL
        REFERENCES story_fragments(id);

CREATE INDEX IF NOT EXISTS idx_story_revisions_event_assignment
    ON story_revisions(story_id, event_assignment_id, created_at DESC)
    WHERE event_assignment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS event_processing_cycle_leases (
    edition_id BIGINT PRIMARY KEY REFERENCES editions(id) ON DELETE CASCADE,
    claim_token TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    rich_calls_started INTEGER NOT NULL DEFAULT 0 CHECK (rich_calls_started >= 0),
    triage_split_calls_started INTEGER NOT NULL DEFAULT 0
        CHECK (triage_split_calls_started >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_processing_cycle_leases_expiry
    ON event_processing_cycle_leases(lease_expires_at);

CREATE TABLE IF NOT EXISTS story_event_processing_claims (
    story_id BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    latest_assignment_id BIGINT NOT NULL REFERENCES story_fragments(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('triage', 'analysis')),
    claim_token TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (story_id, latest_assignment_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_story_event_processing_claims_expiry
    ON story_event_processing_claims(stage, lease_expires_at);

CREATE TABLE IF NOT EXISTS event_revision_processing_state (
    source_item_revision_id BIGINT PRIMARY KEY
        REFERENCES source_item_revisions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_kind TEXT NULL,
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_revision_processing_state_status
    ON event_revision_processing_state(status, updated_at);
