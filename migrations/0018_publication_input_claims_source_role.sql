-- 0018_publication_input_claims_source_role.sql
-- Snapshot and freeze editorial source role at publication input selection time for deterministic retries.

ALTER TABLE publication_input_claims ADD COLUMN IF NOT EXISTS source_role TEXT NULL;
