-- 0022_event_edition_scope.sql
CREATE TABLE IF NOT EXISTS story_edition_scope_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    triage_run_id BIGINT NOT NULL REFERENCES story_event_triage_runs(id),
    story_id BIGINT NOT NULL REFERENCES stories(id),
    edition_id BIGINT NOT NULL REFERENCES editions(id),
    latest_assignment_id BIGINT NOT NULL REFERENCES story_fragments(id),
    scope_version TEXT NOT NULL,
    scope_config_hash TEXT NOT NULL,
    scope_class TEXT NOT NULL CHECK (
        scope_class IN ('LOCAL', 'DIRECT_IMPACT', 'OUT_OF_SCOPE', 'UNCERTAIN')
    ),
    confidence FLOAT8 NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (story_id, latest_assignment_id, scope_version, scope_config_hash)
);

CREATE INDEX IF NOT EXISTS idx_story_scope_current_lookup
ON story_edition_scope_decisions (
    story_id,
    latest_assignment_id,
    scope_config_hash,
    scope_class
);

CREATE INDEX IF NOT EXISTS idx_story_scope_edition_created
ON story_edition_scope_decisions (edition_id, created_at DESC);
