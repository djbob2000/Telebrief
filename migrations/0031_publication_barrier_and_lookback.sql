-- 0031_publication_barrier_and_lookback.sql
-- Keep collection-run observation membership separate from revision creation.

CREATE TABLE IF NOT EXISTS collection_run_revision_observations (
    collection_run_id BIGINT NOT NULL
        REFERENCES collection_runs(id) ON DELETE CASCADE,
    source_item_revision_id BIGINT NOT NULL
        REFERENCES source_item_revisions(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_run_id, source_item_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_run_revision_observations_revision
    ON collection_run_revision_observations(source_item_revision_id, collection_run_id);

-- Preserve the historical relationship for revisions created before this
-- membership table existed. Future collection runs record observations even
-- when revision creation is deduplicated.
INSERT INTO collection_run_revision_observations (
    collection_run_id, source_item_revision_id
)
SELECT sir.collection_run_id, sir.id
FROM source_item_revisions sir
WHERE sir.collection_run_id IS NOT NULL
ON CONFLICT DO NOTHING;

ALTER TABLE publication_refresh_runs
    ADD COLUMN IF NOT EXISTS lookback_hours INTEGER DEFAULT 24;

UPDATE publication_refresh_runs
SET lookback_hours = COALESCE(lookback_hours, 24);

ALTER TABLE publication_refresh_runs
    ALTER COLUMN lookback_hours SET NOT NULL;

ALTER TABLE publication_refresh_runs
    DROP CONSTRAINT IF EXISTS publication_refresh_runs_lookback_hours_check;

ALTER TABLE publication_refresh_runs
    ADD CONSTRAINT publication_refresh_runs_lookback_hours_check
        CHECK (lookback_hours > 0);
