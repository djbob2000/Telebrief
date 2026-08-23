-- 0009_facebook.sql
-- Facebook provider foundation: auth profiles (credentials-free), source configs,
-- comment collection state, and short-lived collector diagnostic artifacts.

-- ---------------------------------------------------------------------------
-- Facebook Auth Profiles
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS facebook_auth_profiles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    storage_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN ('unknown', 'ready', 'auth_required', 'checkpoint_required', 'account_action_required', 'disabled')
    ) DEFAULT 'unknown',
    last_verified_at TIMESTAMPTZ NULL,
    error_kind TEXT NULL,
    error_message TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Facebook Source Configurations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS facebook_source_configs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE UNIQUE,
    auth_profile_id BIGINT NOT NULL REFERENCES facebook_auth_profiles(id),
    group_or_page_id TEXT NULL,
    url TEXT NOT NULL,
    scan_times TEXT[] NOT NULL DEFAULT ARRAY['08:00', '12:00', '16:00', '19:30']::text[],
    timezone TEXT NOT NULL DEFAULT 'UTC',
    collector_options JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_facebook_source_configs_auth ON facebook_source_configs(auth_profile_id);

-- ---------------------------------------------------------------------------
-- Facebook Comment Collection State
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS facebook_comment_collection_state (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE UNIQUE,
    last_scanned_at TIMESTAMPTZ NULL,
    oldest_comment_published_at TIMESTAMPTZ NULL,
    newest_comment_published_at TIMESTAMPTZ NULL,
    total_comments_observed INTEGER NOT NULL DEFAULT 0,
    completeness TEXT NOT NULL CHECK (
        completeness IN ('partial', 'complete')
    ) DEFAULT 'partial',
    continuation_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fb_comments_completeness ON facebook_comment_collection_state(completeness, last_scanned_at);

-- ---------------------------------------------------------------------------
-- Diagnostic Collector Artifacts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collector_artifacts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK (
        artifact_type IN ('screenshot', 'dom_snapshot', 'network_har', 'trace')
    ),
    storage_path TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_collector_artifacts_expires ON collector_artifacts(expires_at);
