-- 0033_publication_knowledge_snapshot.sql
-- Persist the exact knowledge snapshot accepted by the readiness barrier.

ALTER TABLE publication_refresh_runs
    ADD COLUMN knowledge_snapshot_at TIMESTAMPTZ NULL;
