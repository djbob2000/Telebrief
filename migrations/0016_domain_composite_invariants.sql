-- 0016_domain_composite_invariants.sql
-- Enforce cross-edition domain invariants and Story/Revision composite integrity

-- 1. Composite UNIQUE targets
CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_id_edition
ON claims(id, edition_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_id_revision_edition
ON claims(id, source_item_revision_id, edition_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_extraction_runs_id_revision_edition
ON claim_extraction_runs(id, source_item_revision_id, edition_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_stories_id_edition
ON stories(id, edition_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_story_revisions_story_id_id
ON story_revisions(story_id, id);

-- 2. Exact composite FK from claims to its extraction run
ALTER TABLE claims
    DROP CONSTRAINT IF EXISTS fk_claims_extraction_run_composite,
    ADD CONSTRAINT fk_claims_extraction_run_composite
    FOREIGN KEY (claim_extraction_run_id, source_item_revision_id, edition_id)
    REFERENCES claim_extraction_runs(id, source_item_revision_id, edition_id);

-- 3. claim_relations edition isolation
ALTER TABLE claim_relations
    ADD COLUMN IF NOT EXISTS edition_id BIGINT;

CREATE OR REPLACE FUNCTION trg_claim_relations_set_edition()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.edition_id IS NULL THEN
        SELECT edition_id INTO NEW.edition_id FROM claims WHERE id = NEW.from_claim_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_claim_relations_edition ON claim_relations;
CREATE TRIGGER trg_claim_relations_edition
BEFORE INSERT OR UPDATE ON claim_relations
FOR EACH ROW EXECUTE FUNCTION trg_claim_relations_set_edition();

UPDATE claim_relations cr
SET edition_id = c.edition_id
FROM claims c
WHERE cr.from_claim_id = c.id AND cr.edition_id IS NULL;

ALTER TABLE claim_relations
    ALTER COLUMN edition_id SET NOT NULL;

ALTER TABLE claim_relations
    DROP CONSTRAINT IF EXISTS fk_claim_relations_from_edition,
    ADD CONSTRAINT fk_claim_relations_from_edition
    FOREIGN KEY (from_claim_id, edition_id)
    REFERENCES claims(id, edition_id);

ALTER TABLE claim_relations
    DROP CONSTRAINT IF EXISTS fk_claim_relations_to_edition,
    ADD CONSTRAINT fk_claim_relations_to_edition
    FOREIGN KEY (to_claim_id, edition_id)
    REFERENCES claims(id, edition_id);

-- 4. story_claims edition isolation
ALTER TABLE story_claims
    ADD COLUMN IF NOT EXISTS edition_id BIGINT;

CREATE OR REPLACE FUNCTION trg_story_claims_set_edition()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.edition_id IS NULL THEN
        SELECT edition_id INTO NEW.edition_id FROM stories WHERE id = NEW.story_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_story_claims_edition ON story_claims;
CREATE TRIGGER trg_story_claims_edition
BEFORE INSERT OR UPDATE ON story_claims
FOR EACH ROW EXECUTE FUNCTION trg_story_claims_set_edition();

UPDATE story_claims sc
SET edition_id = s.edition_id
FROM stories s
WHERE sc.story_id = s.id AND sc.edition_id IS NULL;

ALTER TABLE story_claims
    ALTER COLUMN edition_id SET NOT NULL;

ALTER TABLE story_claims
    DROP CONSTRAINT IF EXISTS fk_story_claims_story_edition,
    ADD CONSTRAINT fk_story_claims_story_edition
    FOREIGN KEY (story_id, edition_id)
    REFERENCES stories(id, edition_id);

ALTER TABLE story_claims
    DROP CONSTRAINT IF EXISTS fk_story_claims_claim_edition,
    ADD CONSTRAINT fk_story_claims_claim_edition
    FOREIGN KEY (claim_id, edition_id)
    REFERENCES claims(id, edition_id);

-- 5. story_relations edition isolation
ALTER TABLE story_relations
    ADD COLUMN IF NOT EXISTS edition_id BIGINT;

CREATE OR REPLACE FUNCTION trg_story_relations_set_edition()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.edition_id IS NULL THEN
        SELECT edition_id INTO NEW.edition_id FROM stories WHERE id = NEW.from_story_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_story_relations_edition ON story_relations;
CREATE TRIGGER trg_story_relations_edition
BEFORE INSERT OR UPDATE ON story_relations
FOR EACH ROW EXECUTE FUNCTION trg_story_relations_set_edition();

UPDATE story_relations sr
SET edition_id = s.edition_id
FROM stories s
WHERE sr.from_story_id = s.id AND sr.edition_id IS NULL;

ALTER TABLE story_relations
    ALTER COLUMN edition_id SET NOT NULL;

ALTER TABLE story_relations
    DROP CONSTRAINT IF EXISTS fk_story_relations_from_edition,
    ADD CONSTRAINT fk_story_relations_from_edition
    FOREIGN KEY (from_story_id, edition_id)
    REFERENCES stories(id, edition_id);

ALTER TABLE story_relations
    DROP CONSTRAINT IF EXISTS fk_story_relations_to_edition,
    ADD CONSTRAINT fk_story_relations_to_edition
    FOREIGN KEY (to_story_id, edition_id)
    REFERENCES stories(id, edition_id);

-- 6. (story_id, story_revision_id) composite FKs
ALTER TABLE story_matching_candidates
    DROP CONSTRAINT IF EXISTS fk_story_matching_candidates_story_revision,
    ADD CONSTRAINT fk_story_matching_candidates_story_revision
    FOREIGN KEY (story_id, story_revision_id)
    REFERENCES story_revisions(story_id, id);

ALTER TABLE publication_candidates
    DROP CONSTRAINT IF EXISTS fk_publication_candidates_story_revision,
    ADD CONSTRAINT fk_publication_candidates_story_revision
    FOREIGN KEY (story_id, story_revision_id)
    REFERENCES story_revisions(story_id, id)
    ON DELETE CASCADE;

ALTER TABLE publication_inputs
    DROP CONSTRAINT IF EXISTS fk_publication_inputs_story_revision,
    ADD CONSTRAINT fk_publication_inputs_story_revision
    FOREIGN KEY (story_id, story_revision_id)
    REFERENCES story_revisions(story_id, id)
    ON DELETE CASCADE;
