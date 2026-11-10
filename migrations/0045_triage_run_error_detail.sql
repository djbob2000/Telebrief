-- A failed Gate batch keeps its HTTP status and a short, token-free message.
-- Without them, a payment or quota failure is indistinguishable from an unknown one.
ALTER TABLE story_event_triage_runs
    ADD COLUMN IF NOT EXISTS error_status integer,
    ADD COLUMN IF NOT EXISTS error_type text,
    ADD COLUMN IF NOT EXISTS error_message text;
