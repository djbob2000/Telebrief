-- 0003_collection_bookkeeping.sql
-- Per-source collection checkpoints and run bookkeeping.
-- SourceItem content tables are intentionally Plan 2 and are not defined here.

CREATE TABLE IF NOT EXISTS collection_checkpoints (
    source_id BIGINT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    last_success_at TIMESTAMPTZ,
    last_scan_at TIMESTAMPTZ,
    cursor JSONB,
    backoff_until TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    adapter_state JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL
        CONSTRAINT collection_runs_trigger_check
        CHECK (trigger IN ('scheduled', 'pre_publish', 'manual', 'backfill')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CONSTRAINT collection_runs_status_check
        CHECK (status IN (
            'running',
            'success',
            'transient',
            'rate_limited',
            'auth_required',
            'account_action_required',
            'access_denied',
            'source_not_found',
            'layout_changed',
            'permanent',
            'failed'
        )),
    seen_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_source_started
ON collection_runs(source_id, started_at DESC);
