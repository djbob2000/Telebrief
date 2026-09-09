-- 0029_event_processing_semantic_reuse.sql
-- Persist the versioned Event-First input identity separately from audit identity.

ALTER TABLE source_item_revisions
    ADD COLUMN IF NOT EXISTS event_processing_hash TEXT NULL,
    ADD COLUMN IF NOT EXISTS event_input_version TEXT NULL;

ALTER TABLE event_revision_processing_state
    ADD COLUMN IF NOT EXISTS processing_mode TEXT NOT NULL DEFAULT 'full',
    ADD COLUMN IF NOT EXISTS reused_from_revision_id BIGINT NULL
        REFERENCES source_item_revisions(id) ON DELETE SET NULL;

ALTER TABLE event_revision_processing_state
    DROP CONSTRAINT IF EXISTS event_revision_processing_state_processing_mode_check;

ALTER TABLE event_revision_processing_state
    ADD CONSTRAINT event_revision_processing_state_processing_mode_check
    CHECK (processing_mode IN ('full', 'reused'));

CREATE INDEX IF NOT EXISTS idx_source_item_revisions_event_processing_hash
    ON source_item_revisions(source_item_id, revision_no, event_processing_hash)
    WHERE event_processing_hash IS NOT NULL;
