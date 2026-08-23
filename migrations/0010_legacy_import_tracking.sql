-- Migration 0010: Legacy Telegram message import tracking (Plan 5 Task 8)

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    channel_name TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    link TEXT NOT NULL,
    has_media BOOLEAN NOT NULL DEFAULT FALSE,
    media_type TEXT NOT NULL DEFAULT '',
    collected_at TIMESTAMPTZ NULL DEFAULT now()
);

ALTER TABLE messages ALTER COLUMN collected_at DROP NOT NULL;

CREATE TABLE IF NOT EXISTS legacy_imported_messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_message_id BIGINT NOT NULL UNIQUE,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id) ON DELETE CASCADE,
    temporal_fidelity TEXT NOT NULL CHECK (temporal_fidelity IN ('exact', 'limited')),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_legacy_imported_source_item
ON legacy_imported_messages(source_item_id);
