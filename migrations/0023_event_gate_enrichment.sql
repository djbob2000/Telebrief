-- 0023_event_gate_enrichment.sql
-- Gate V2 enrichment persistence and last_analyzed_* repair (migration 23).

ALTER TABLE story_event_triage_decisions
    ADD COLUMN IF NOT EXISTS scope_config_hash TEXT,
    ADD COLUMN IF NOT EXISTS retention TEXT,
    ADD COLUMN IF NOT EXISTS enrichment TEXT,
    ADD COLUMN IF NOT EXISTS brief_payload JSONB;

UPDATE story_event_triage_decisions
SET retention = CASE WHEN decision = 'IGNORE' THEN 'DROP' ELSE 'KEEP' END,
    enrichment = CASE WHEN decision = 'IGNORE' THEN 'NONE' ELSE 'ANALYZE' END,
    scope_config_hash = COALESCE(scope_config_hash, 'legacy')
WHERE retention IS NULL OR enrichment IS NULL OR scope_config_hash IS NULL;

ALTER TABLE story_event_triage_decisions
    ALTER COLUMN scope_config_hash SET NOT NULL,
    ALTER COLUMN retention SET NOT NULL,
    ALTER COLUMN enrichment SET NOT NULL;

ALTER TABLE story_event_triage_decisions
    DROP CONSTRAINT IF EXISTS story_event_triage_decisions_retention_check,
    ADD CONSTRAINT story_event_triage_decisions_retention_check
        CHECK (retention IN ('KEEP', 'DROP'));

ALTER TABLE story_event_triage_decisions
    DROP CONSTRAINT IF EXISTS story_event_triage_decisions_enrichment_check,
    ADD CONSTRAINT story_event_triage_decisions_enrichment_check
        CHECK (enrichment IN ('NONE', 'BRIEF', 'ANALYZE'));

ALTER TABLE story_event_triage_decisions
    DROP CONSTRAINT IF EXISTS story_event_triage_decisions_retention_enrichment_combo_check,
    ADD CONSTRAINT story_event_triage_decisions_retention_enrichment_combo_check
        CHECK (
            (retention = 'DROP' AND enrichment = 'NONE') OR
            (retention = 'KEEP' AND enrichment IN ('BRIEF', 'ANALYZE'))
        );

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (
        SELECT conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'story_event_triage_decisions'
          AND con.contype = 'u'
          AND con.conname != 'story_event_triage_decisions_gate_v2_key'
    ) LOOP
        EXECUTE 'ALTER TABLE story_event_triage_decisions DROP CONSTRAINT IF EXISTS ' || quote_ident(r.conname);
    END LOOP;
END $$;

ALTER TABLE story_event_triage_decisions
    DROP CONSTRAINT IF EXISTS story_event_triage_decisions_gate_v2_key;

ALTER TABLE story_event_triage_decisions
    ADD CONSTRAINT story_event_triage_decisions_gate_v2_key
    UNIQUE (story_id, latest_assignment_id, triage_version, scope_config_hash);


-- Repair story_cluster_state.last_analyzed_assignment_id/last_analyzed_at from actual successful rich-analysis runs.
-- For a Story with no successful story_event_analysis_runs, set both fields to NULL.
UPDATE story_cluster_state scs
SET last_analyzed_assignment_id = runs.latest_assignment_id,
    last_analyzed_at = runs.completed_at
FROM (
    SELECT DISTINCT ON (story_id)
        story_id,
        latest_assignment_id,
        completed_at
    FROM story_event_analysis_runs
    WHERE status = 'succeeded'
    ORDER BY story_id, completed_at DESC, id DESC
) runs
WHERE scs.story_id = runs.story_id;

UPDATE story_cluster_state
SET last_analyzed_assignment_id = NULL,
    last_analyzed_at = NULL
WHERE story_id NOT IN (
    SELECT DISTINCT story_id
    FROM story_event_analysis_runs
    WHERE status = 'succeeded'
);
