-- Migration 0011 (Deferred): Drop legacy messages table (Plan 5 Task 9)
-- Safety guard: only drop messages if table exists, legacy_imported_messages exists, and all rows are imported into legacy_imported_messages.
DO $$
BEGIN
    IF to_regclass('messages') IS NOT NULL THEN
        IF to_regclass('legacy_imported_messages') IS NULL THEN
            RAISE EXCEPTION 'Cannot drop messages: legacy_imported_messages tracking table does not exist. Run scripts/import_legacy_messages.py first.';
        END IF;

        IF EXISTS (
            SELECT 1 FROM messages m
            WHERE NOT EXISTS (
                SELECT 1 FROM legacy_imported_messages lim WHERE lim.legacy_message_id = m.id
            )
        ) THEN
            RAISE EXCEPTION 'Migration 0011 halted: table "messages" contains unimported rows. Run scripts/import_legacy_messages.py first.';
        END IF;

        DROP TABLE messages CASCADE;
    END IF;
END $$;
