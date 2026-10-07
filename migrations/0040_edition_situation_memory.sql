-- 0040_edition_situation_memory.sql
-- Immutable snapshots of an edition's running-story memory.
--
-- Each update writes a new row derived from the previous snapshot and the
-- Stories revised since it. Publications reference the snapshot they used, so
-- a frozen run can be replayed with exactly the same background.

CREATE TABLE IF NOT EXISTS edition_situation_memory_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    previous_snapshot_id BIGINT NULL
        REFERENCES edition_situation_memory_snapshots(id) ON DELETE SET NULL,
    as_of TIMESTAMPTZ NOT NULL,
    input_since TIMESTAMPTZ NOT NULL,
    memory_version TEXT NOT NULL,
    model TEXT NULL,
    situations JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT edition_situation_memory_window_check CHECK (input_since <= as_of)
);

CREATE INDEX IF NOT EXISTS idx_edition_situation_memory_latest
    ON edition_situation_memory_snapshots(edition_id, as_of DESC, id DESC);
