-- 0014_story_matching_retrieval_seal.sql
-- Add candidates_retrieved_at to story_matching_runs to seal empty candidate sets durably

ALTER TABLE story_matching_runs
    ADD COLUMN IF NOT EXISTS candidates_retrieved_at TIMESTAMPTZ NULL;
