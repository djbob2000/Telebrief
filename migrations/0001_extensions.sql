-- 0001_extensions.sql
-- PostgreSQL extensions required by the Telebrief domain schema.
-- pgvector is not a trusted extension: on clusters where it is missing, a
-- superuser must install it once; where present this statement no-ops.
CREATE EXTENSION IF NOT EXISTS vector;
