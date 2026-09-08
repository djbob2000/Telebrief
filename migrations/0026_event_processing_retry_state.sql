-- 0026_event_processing_retry_state.sql
-- Durable per-assignment retry/quarantine state for Event-First processing.

CREATE TABLE IF NOT EXISTS story_event_processing_retries (
    story_id BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    latest_assignment_id BIGINT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('triage', 'analysis')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_retry_at TIMESTAMPTZ,
    exhausted_at TIMESTAMPTZ,
    last_error_kind TEXT,
    last_prompt_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (story_id, latest_assignment_id, stage)
);

CREATE INDEX IF NOT EXISTS story_event_processing_retries_due_idx
    ON story_event_processing_retries(stage, next_retry_at)
    WHERE exhausted_at IS NULL;
