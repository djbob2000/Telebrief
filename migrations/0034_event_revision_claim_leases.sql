-- Durable ownership leases for Event-First source-revision processing.

ALTER TABLE event_revision_processing_state
    ADD COLUMN IF NOT EXISTS claim_token UUID NULL,
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_event_revision_processing_claim_expiry
    ON event_revision_processing_state(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL;

-- Rows written by the pre-lease worker must be reclaimable after deployment.
UPDATE event_revision_processing_state
SET claim_expires_at = now()
WHERE status = 'running' AND claim_expires_at IS NULL;
