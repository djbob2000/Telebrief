-- 0028_publication_refresh_readiness.sql
-- Durable pre-publication source collection and Event-First processing barrier.

ALTER TABLE publication_runs
    ADD COLUMN IF NOT EXISTS source_cutoff_at TIMESTAMPTZ NULL;

UPDATE publication_runs
SET source_cutoff_at = snapshot_at
WHERE source_cutoff_at IS NULL;

ALTER TABLE publication_runs
    ALTER COLUMN source_cutoff_at SET NOT NULL;

CREATE TABLE IF NOT EXISTS publication_refresh_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    publication_type TEXT NOT NULL,
    slot_at TIMESTAMPTZ NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    normal_source_cutoff_at TIMESTAMPTZ NOT NULL,
    fallback_snapshot_at TIMESTAMPTZ NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'collecting',
        'processing',
        'ready_for_preparation',
        'fallback_ready',
        'preparing',
        'publication_queued',
        'failed'
    )),
    collection_ready_at TIMESTAMPTZ NULL,
    processing_ready_at TIMESTAMPTZ NULL,
    prepared_at TIMESTAMPTZ NULL,
    publication_run_id BIGINT NULL REFERENCES publication_runs(id),
    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (edition_id, publication_type, slot_at)
);

CREATE INDEX IF NOT EXISTS idx_publication_refresh_runs_open
    ON publication_refresh_runs(status, slot_at, deadline_at)
    WHERE status NOT IN ('publication_queued', 'failed');

CREATE TABLE IF NOT EXISTS publication_refresh_sources (
    refresh_run_id BIGINT NOT NULL
        REFERENCES publication_refresh_runs(id) ON DELETE CASCADE,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    required_since_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'succeeded', 'degraded', 'skipped'
    )),
    collection_run_id BIGINT NULL REFERENCES collection_runs(id),
    collection_outcome TEXT NULL,
    last_enqueue_attempt_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (refresh_run_id, source_id)
);
