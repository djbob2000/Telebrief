-- Migration 0011: Drop legacy messages table (Plan 5 Task 9)
-- NOTE: Operators apply this only after legacy migration verification and database backup.
DROP TABLE IF EXISTS messages CASCADE;
