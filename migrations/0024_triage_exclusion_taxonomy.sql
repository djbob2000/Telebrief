-- 0024_triage_exclusion_taxonomy.sql
-- Extend exclusion_reason check constraint for Gate V2 taxonomy (migration 24).

ALTER TABLE story_event_triage_decisions
    DROP CONSTRAINT IF EXISTS story_event_triage_decisions_exclusion_reason_check,
    ADD CONSTRAINT story_event_triage_decisions_exclusion_reason_check
        CHECK (
            exclusion_reason IS NULL
            OR exclusion_reason IN (
                'commercial_classified',
                'private_classified',
                'directory_payload',
                'obvious_noise'
            )
        );
