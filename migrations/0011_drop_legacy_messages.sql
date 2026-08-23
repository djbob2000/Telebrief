-- Migration 0011: Drop legacy messages table (Plan 5 Task 9)
-- Safety guard: only drop messages if table exists and all rows are imported into legacy_imported_messages.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'messages'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'legacy_imported_messages'
        ) THEN
            IF EXISTS (
                SELECT 1 FROM messages m
                WHERE NOT EXISTS (
                    SELECT 1 FROM legacy_imported_messages lim WHERE lim.legacy_message_id = m.id
                )
            ) THEN
                RAISE EXCEPTION 'Migration 0011 halted: table "messages" contains unimported rows. Run scripts/import_legacy_messages.py first.';
            END IF;
        END IF;

        DROP TABLE messages CASCADE;
    END IF;
END $$;
