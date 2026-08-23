-- 0012: retention provenance preservation and enrichment collection trigger.
--
-- collector_artifacts.deleted_at lets the retention job delete physical
-- files while preserving DB provenance metadata as configured
-- (Plan 5 Task 6: "delete physical artifact/media files while preserving DB
-- provenance metadata").
ALTER TABLE collector_artifacts
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;

DROP INDEX IF EXISTS idx_collector_artifacts_expired;
CREATE INDEX IF NOT EXISTS idx_collector_artifacts_expires_live
ON collector_artifacts(expires_at)
WHERE deleted_at IS NULL;

-- ENRICHMENT trigger: Facebook comment refreshes are enrichment work, not
-- scheduled source scans; recording them under 'scheduled' misstates why a
-- collection ran.
ALTER TABLE collection_runs
    DROP CONSTRAINT IF EXISTS collection_runs_trigger_check;
ALTER TABLE collection_runs
    ADD CONSTRAINT collection_runs_trigger_check
    CHECK (trigger IN ('scheduled', 'pre_publish', 'manual', 'backfill', 'enrichment'));
