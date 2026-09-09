-- 0030_unified_publication_intents.sql
-- Generalize refresh readiness records into the durable publication-intent barrier.

ALTER TABLE publication_refresh_runs
    ADD COLUMN IF NOT EXISTS trigger TEXT,
    ADD COLUMN IF NOT EXISTS request_key TEXT,
    ADD COLUMN IF NOT EXISTS freshness_cutoff_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS requested_by_user_id BIGINT NULL;

UPDATE publication_refresh_runs
SET trigger = COALESCE(trigger, 'scheduled'),
    request_key = COALESCE(request_key, 'legacy-refresh:' || id::text),
    freshness_cutoff_at = COALESCE(freshness_cutoff_at, requested_at);

ALTER TABLE publication_refresh_runs
    ALTER COLUMN trigger SET NOT NULL,
    ALTER COLUMN request_key SET NOT NULL,
    ALTER COLUMN freshness_cutoff_at SET NOT NULL;

ALTER TABLE publication_refresh_runs
    DROP CONSTRAINT IF EXISTS publication_refresh_runs_trigger_check,
    DROP CONSTRAINT IF EXISTS publication_refresh_runs_status_check;

UPDATE publication_refresh_runs
SET status = 'failed',
    error_kind = COALESCE(error_kind, 'legacy_fallback_disabled'),
    fallback_used = FALSE
WHERE status = 'fallback_ready';

ALTER TABLE publication_refresh_runs
    ADD CONSTRAINT publication_refresh_runs_trigger_check
        CHECK (trigger IN ('manual', 'scheduled')),
    ADD CONSTRAINT publication_refresh_runs_status_check
        CHECK (status IN (
            'collecting', 'processing', 'ready_waiting_slot',
            'ready_for_preparation', 'preparing', 'publication_queued', 'failed'
        ));

CREATE UNIQUE INDEX IF NOT EXISTS uq_publication_refresh_runs_request_key
    ON publication_refresh_runs(request_key);

CREATE TABLE IF NOT EXISTS publication_failure_notifications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refresh_run_id BIGINT NOT NULL
        REFERENCES publication_refresh_runs(id) ON DELETE CASCADE,
    recipient_user_id BIGINT NOT NULL,
    failure_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT NULL,
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (refresh_run_id, recipient_user_id, failure_kind)
);
