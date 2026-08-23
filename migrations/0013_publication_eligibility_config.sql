-- 0013_publication_eligibility_config.sql
-- Add config JSONB to eligibility, editorial selection, and writer policy versions

ALTER TABLE eligibility_policy_versions
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE editorial_selection_policy_versions
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE writer_policy_versions
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;
