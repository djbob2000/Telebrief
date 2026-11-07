-- 0043_refresh_slot_barrier.sql
-- One live readiness barrier per publication slot, with preview rows exempt.
--
-- The original UNIQUE (edition_id, publication_type, slot_at) also counted
-- manual preview rows, so a preview at a scheduled slot collided with the
-- real refresh run and raised UniqueViolation. Replace it with a partial
-- unique index that covers only live (non-failed, non-preview) barriers.
--
-- Refresh rows whose publication run already failed were left in
-- publication_queued by the old code. Mark them failed so they stop blocking
-- their slot.

UPDATE publication_refresh_runs AS refresh
SET status = 'failed',
    error_kind = COALESCE(refresh.error_kind, 'publication_run_failed'),
    updated_at = now()
FROM publication_runs AS run
WHERE refresh.status = 'publication_queued'
  AND refresh.publication_run_id = run.id
  AND run.status = 'failed';

ALTER TABLE publication_refresh_runs
    DROP CONSTRAINT IF EXISTS publication_refresh_runs_edition_id_publication_type_slot_a_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_publication_refresh_runs_live_slot
    ON publication_refresh_runs(edition_id, publication_type, slot_at)
    WHERE status <> 'failed' AND request_key NOT LIKE 'preview:%';
