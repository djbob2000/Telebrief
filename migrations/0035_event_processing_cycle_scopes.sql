-- Event processing cycle scopes for parallel authority workers (migration 35).

ALTER TABLE event_processing_cycle_leases
    ADD COLUMN IF NOT EXISTS scope_key TEXT NOT NULL DEFAULT 'default';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.key_column_usage
        WHERE table_name = 'event_processing_cycle_leases'
          AND column_name = 'scope_key'
          AND constraint_name = 'event_processing_cycle_leases_pkey'
    ) THEN
        ALTER TABLE event_processing_cycle_leases
            DROP CONSTRAINT IF EXISTS event_processing_cycle_leases_pkey;
        ALTER TABLE event_processing_cycle_leases
            ADD CONSTRAINT event_processing_cycle_leases_pkey
            PRIMARY KEY (edition_id, scope_key);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_event_processing_cycle_leases_scope_expiry
    ON event_processing_cycle_leases(edition_id, scope_key, lease_expires_at);
