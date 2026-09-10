-- 0032_fix_publication_lookback_backfill.sql
-- Repair rows that existed before 0031 added the per-run lookback column.
--
-- Migration 0031 was already deployed in some environments with a default
-- lookback of 24 hours for every existing row. Do not rewrite rows created
-- after that migration: their value may be an intentional request override.
-- The ledger timestamp is the only reliable boundary because the old schema
-- had no lookback column in which an override could have been stored.

WITH migration_boundary AS (
    SELECT applied_at
    FROM telebrief_schema_migrations
    WHERE version = 31
)
UPDATE publication_refresh_runs AS refresh
SET lookback_hours = CASE refresh.publication_type
    WHEN 'weekly_article' THEN 168
    WHEN 'monthly_article' THEN 720
END
FROM migration_boundary
WHERE refresh.publication_type IN ('weekly_article', 'monthly_article')
  AND refresh.lookback_hours = 24
  AND refresh.created_at < migration_boundary.applied_at
  AND refresh.status NOT IN ('publication_queued', 'failed');
