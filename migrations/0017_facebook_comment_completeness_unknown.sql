-- 0017_facebook_comment_completeness_unknown.sql
-- Allow 'unknown' in facebook_comment_collection_state.completeness

DO $$
DECLARE
    r RECORD;
BEGIN
    IF to_regclass('facebook_comment_collection_state') IS NOT NULL THEN
        FOR r IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'facebook_comment_collection_state'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%completeness%'
        LOOP
            EXECUTE 'ALTER TABLE facebook_comment_collection_state DROP CONSTRAINT ' || quote_ident(r.conname);
        END LOOP;

        ALTER TABLE facebook_comment_collection_state
            ADD CONSTRAINT facebook_comment_collection_state_completeness_check
            CHECK (completeness IN ('complete', 'partial', 'unknown'));
    END IF;
END $$;
