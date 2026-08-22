-- 0004_source_items.sql
-- Generic ingestion foundation: immutable source items with append-only
-- revisions, per-revision assets, item state events, and bootstrap-vs-
-- database ownership of source rows (sources.management_mode).
--
-- Identity & correctness:
--   * UNIQUE(source_id, external_id) on source_items is THE stable identity
--     constraint; every later pass (relationships, revisions, assets, state
--     events) resolves items through it.
--   * Revisions are immutable. insert flows compare only against the latest
--     revision, so A -> B -> A legitimately creates revision 3 (a source may
--     edit and later revert); no global content-hash uniqueness exists.
--   * revision_no is monotonic per item: callers compute MAX(revision_no)+1
--     inside their inserting transaction; UNIQUE(source_item_id, revision_no)
--     is the correctness backstop under concurrent writers.
--   * Assets bind to an exact revision (source_item_revision_id), never to
--     the mutable head of an item.
--   * State events are append-only by convention: writers only INSERT; no
--     UPDATE/DELETE paths are provided anywhere in the application.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS management_mode TEXT NOT NULL DEFAULT 'bootstrap'
        CONSTRAINT sources_management_mode_check
        CHECK (management_mode IN ('bootstrap', 'database'));

CREATE TABLE IF NOT EXISTS source_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id),
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    parent_item_id BIGINT NULL REFERENCES source_items(id),
    root_item_id BIGINT NULL REFERENCES source_items(id),
    author_name TEXT NULL,
    author_external_id TEXT NULL,
    canonical_url TEXT NULL,
    published_at TIMESTAMPTZ NULL,
    first_collected_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_source_items_source_external_id UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_source_items_parent
ON source_items(parent_item_id);

CREATE TABLE IF NOT EXISTS source_item_revisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash TEXT NOT NULL,
    text_content TEXT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_source_item_revisions_item_revision_no
        UNIQUE (source_item_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_source_item_revisions_item_hash
ON source_item_revisions(source_item_id, content_hash);

CREATE TABLE IF NOT EXISTS source_assets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_revision_id BIGINT NOT NULL REFERENCES source_item_revisions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    external_url TEXT NULL,
    local_storage_ref TEXT NULL,
    mime_type TEXT NULL,
    content_hash TEXT NULL,
    width INTEGER NULL,
    height INTEGER NULL,
    -- Spec §5 duration: BIGINT seconds chosen over INTERVAL for simplicity
    -- (integer arithmetic, direct JSON serialization).
    duration BIGINT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Album-safe asset identity per revision: Telegram albums expose no per-photo
-- URLs, so same-kind assets with NULL external_url are kept apart by content
-- hash instead of collapsing onto one row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_assets_revision_identity
ON source_assets(
    source_item_revision_id,
    kind,
    COALESCE(external_url, ''),
    COALESCE(content_hash, '')
);

CREATE TABLE IF NOT EXISTS source_item_state_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
);
