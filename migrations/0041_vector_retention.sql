-- 0041_vector_retention.sql
-- Let old embedding vectors and Story centroids be evicted, keep one
-- permanent semantic vector per published Story.
--
-- Vectors only matter while a Story can still absorb fragments (the active
-- clustering window). Eviction sets the vector to NULL instead of deleting
-- the row: source_fragment_embeddings and story_fragments keep their foreign
-- keys, and a later fragment with the same normalized hash refills the row.

ALTER TABLE fragment_embedding_vectors
    ALTER COLUMN embedding DROP NOT NULL;

ALTER TABLE fragment_embedding_vectors
    ADD COLUMN IF NOT EXISTS evicted_at TIMESTAMPTZ NULL;

ALTER TABLE story_cluster_state
    ALTER COLUMN centroid DROP NOT NULL;

-- Retention scans old vectors by age; this keeps each nightly batch cheap.
CREATE INDEX IF NOT EXISTS idx_fragment_embedding_vectors_live_created
    ON fragment_embedding_vectors(created_at, id)
    WHERE embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_source_fragment_embeddings_vector
    ON source_fragment_embeddings(vector_id);

CREATE TABLE IF NOT EXISTS story_semantic_vectors (
    story_id BIGINT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    embedding HALFVEC NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    source TEXT NOT NULL DEFAULT 'cluster_centroid',
    source_updated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
