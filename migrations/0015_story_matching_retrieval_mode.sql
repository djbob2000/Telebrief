-- 0015_story_matching_retrieval_mode.sql
-- Add retrieval_mode to story_matching_runs to record retrieval mode provenance

ALTER TABLE story_matching_runs
    ADD COLUMN IF NOT EXISTS retrieval_mode TEXT NOT NULL DEFAULT 'knowledge_full'
    CONSTRAINT story_matching_runs_retrieval_mode_check
    CHECK (retrieval_mode IN ('knowledge_full', 'knowledge_no_embeddings'));
