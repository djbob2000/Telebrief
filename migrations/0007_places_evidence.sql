-- 0007_places_evidence.sql
-- Plan 3 Task 8: places + aliases, immutable claim place mentions and
-- lightweight entities, versioned place-resolution scaffolding, and the
-- nullable edition-current place-policy pointer.
--
-- Design choices where the plan leaves freedom:
--   * place_aliases.normalized_alias is INDEXED, never unique: several real
--     Berdyansk areas share colloquial labels (the city profile repeats
--     «на горе» under two different areas), so global alias uniqueness is
--     wrong by construction. Idempotent seeding is per-place via NOT EXISTS.
--   * claim_place_mentions / claim_entities are append-only provenance rows;
--     the ORIGINAL mention text is always preserved verbatim and resolution
--     never mutates claims. Legacy mentions still live in claims.metadata —
--     the bounded backfill migrates them to rows without touching metadata.
--   * place_resolution_policy_versions mirrors 0005's policy pattern:
--     UNIQUE (edition_id, version) plus a UNIQUE (id, edition_id) target for
--     edition-consistent composite FKs; "current" resolves by identity with
--     latest-version-wins semantics, and editions.current_place_policy_id
--     (nullable pointer, composite-FK-guarded like current_relevance_policy_id)
--     records the edition's active choice.
--   * place_resolution_runs carries an explicit NOT NULL edition_id because
--     the mandated composite policy FK (policy_id, edition_id) is impossible
--     without it (same ruling as vision_analysis_runs in 0005).
--   * place_resolution_results gets `status` TEXT CHECK resolved|unresolved
--     (RULING): NULL place_id with status 'unresolved' is a COMPLETED outcome,
--     so uq_place_resolution_canonical ON (mention_id, policy_id) WHERE
--     status IN ('resolved','unresolved') pins at most one canonical result
--     per (mention, policy) while failed runs never occupy the slot. A newer
--     policy version starts a new canonical key and may re-resolve the same
--     immutable mention.

-- ---------------------------------------------------------------------------
-- Places + aliases
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS places (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    kind TEXT NULL,
    parent_place_id BIGINT NULL REFERENCES places(id),
    latitude NUMERIC NULL,
    longitude NUMERIC NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_places_parent ON places(parent_place_id);
CREATE INDEX IF NOT EXISTS idx_places_canonical_name ON places(canonical_name);

CREATE TABLE IF NOT EXISTS place_aliases (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    place_id BIGINT NOT NULL REFERENCES places(id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deliberately NOT unique: alias collisions across places are expected.
CREATE INDEX IF NOT EXISTS idx_place_aliases_normalized
ON place_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_place_aliases_place ON place_aliases(place_id);

-- ---------------------------------------------------------------------------
-- Immutable claim evidence: place mentions + lightweight entities
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claim_place_mentions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    role TEXT NULL,
    original_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_place_mentions_claim
ON claim_place_mentions(claim_id);

CREATE TABLE IF NOT EXISTS claim_entities (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES claims(id),
    normalized_text TEXT NOT NULL,
    entity_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_entities_claim ON claim_entities(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_entities_normalized_text
ON claim_entities(normalized_text);

-- ---------------------------------------------------------------------------
-- Versioned place-resolution policy + runs + canonical results
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS place_resolution_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_place_resolution_policy_edition_version UNIQUE (edition_id, version)
);

-- Composite FK target for edition-consistent policy references (0005 pattern).
CREATE UNIQUE INDEX IF NOT EXISTS uq_place_resolution_policy_id_edition
ON place_resolution_policy_versions(id, edition_id);

ALTER TABLE editions
    ADD COLUMN IF NOT EXISTS current_place_policy_id BIGINT NULL;

ALTER TABLE editions
    DROP CONSTRAINT IF EXISTS fk_editions_current_place_policy;

ALTER TABLE editions
    ADD CONSTRAINT fk_editions_current_place_policy
    FOREIGN KEY (current_place_policy_id, id)
    REFERENCES place_resolution_policy_versions(id, edition_id);

CREATE TABLE IF NOT EXISTS place_resolution_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mention_id BIGINT NOT NULL REFERENCES claim_place_mentions(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    policy_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL
        CONSTRAINT place_resolution_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed')),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_place_resolution_runs_policy
        FOREIGN KEY (policy_id, edition_id)
        REFERENCES place_resolution_policy_versions(id, edition_id)
);

CREATE INDEX IF NOT EXISTS idx_place_resolution_runs_mention
ON place_resolution_runs(mention_id);

CREATE TABLE IF NOT EXISTS place_resolution_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES place_resolution_runs(id),
    mention_id BIGINT NOT NULL REFERENCES claim_place_mentions(id),
    policy_id BIGINT NOT NULL,
    place_id BIGINT NULL REFERENCES places(id),
    status TEXT NOT NULL
        CONSTRAINT place_resolution_results_status_check
        CHECK (status IN ('resolved', 'unresolved')),
    confidence NUMERIC NULL,
    reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Canonical invariant (RULING): at most one completed result per
-- (mention, policy); both outcomes occupy the slot, failed runs never do.
CREATE UNIQUE INDEX IF NOT EXISTS uq_place_resolution_canonical
ON place_resolution_results(mention_id, policy_id)
WHERE status IN ('resolved', 'unresolved');

CREATE INDEX IF NOT EXISTS idx_place_resolution_results_mention_policy
ON place_resolution_results(mention_id, policy_id);
CREATE INDEX IF NOT EXISTS idx_place_resolution_results_place
ON place_resolution_results(place_id)
WHERE place_id IS NOT NULL;
