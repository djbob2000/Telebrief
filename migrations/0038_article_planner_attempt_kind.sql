-- 0038_article_planner_attempt_kind.sql
-- Record the article-level editorial planner as a first-class generation attempt.

ALTER TABLE publication_generation_attempts
    DROP CONSTRAINT IF EXISTS publication_generation_attempts_kind_check,
    ADD CONSTRAINT publication_generation_attempts_kind_check
        CHECK (
            kind IN (
                'writer',
                'repair',
                'deterministic_fallback',
                'story_renderer_fallback',
                'deterministic_supplement',
                'article_planner'
            )
        );
