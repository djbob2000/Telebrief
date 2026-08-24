-- 0019_publication_input_claims_attribution_snapshot.sql
-- Snapshot and freeze complete source attribution (role, name, url, external_id)
-- at publication input selection time for immutable deterministic retries.

ALTER TABLE publication_input_claims ADD COLUMN IF NOT EXISTS source_name TEXT NULL;
ALTER TABLE publication_input_claims ADD COLUMN IF NOT EXISTS source_snapshot JSONB NULL;

-- Backfill existing rows with source metadata and role
UPDATE publication_input_claims pic
SET source_role = COALESCE(pic.source_role, s.role),
    source_name = COALESCE(pic.source_name, s.name),
    source_snapshot = COALESCE(
        pic.source_snapshot,
        jsonb_build_object(
            'source_id', s.id,
            'platform', s.platform,
            'name', s.name,
            'role', s.role,
            'url', s.url,
            'external_id', s.external_id
        )
    )
FROM claims c
JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
JOIN source_items si ON si.id = sir.source_item_id
JOIN sources s ON s.id = si.source_id
WHERE pic.claim_id = c.id
  AND (pic.source_role IS NULL OR pic.source_name IS NULL OR pic.source_snapshot IS NULL);
