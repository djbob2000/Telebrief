-- 0002_domain_foundation.sql
-- Provider-neutral foundation tables: sources, editions, source_editions.
-- Relevance policy tables (and editions.current_relevance_policy_id) are
-- deliberately NOT here; they belong to Plan 3's 0005_relevance_claims.sql.

CREATE TABLE IF NOT EXISTS sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT,
    url TEXT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'other'
        CONSTRAINT sources_role_check
        CHECK (role IN ('official', 'local_media', 'community', 'individual', 'other')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    collector_options JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One registration per (platform, kind, external identity); rows without an
-- external identity (URL-only sources) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_platform_kind_external_id
ON sources(platform, kind, external_id)
WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS editions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    language TEXT NOT NULL DEFAULT 'ru',
    profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Binding table only: deleting a source or edition removes the binding row;
-- it never cascades into any historical records.
CREATE TABLE IF NOT EXISTS source_editions (
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, edition_id)
);
