-- 0025_publication_attempt_deterministic_supplement.sql
-- Allow 'deterministic_supplement' in publication_generation_attempts.kind (migration 25).

ALTER TABLE publication_generation_attempts
    DROP CONSTRAINT IF EXISTS publication_generation_attempts_kind_check,
    ADD CONSTRAINT publication_generation_attempts_kind_check
        CHECK (
            kind IN (
                'writer',
                'repair',
                'deterministic_fallback',
                'story_renderer_fallback',
                'deterministic_supplement'
            )
        );
