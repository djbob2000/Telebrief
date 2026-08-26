-- 0020_historical_archive.sql
-- Storage and vector index for external historical news archives (Temporal RAG)

CREATE TABLE IF NOT EXISTS archive_articles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_slug TEXT NOT NULL DEFAULT 'berdyansk',
    source_name TEXT NOT NULL,
    source_url TEXT,
    external_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    category TEXT,
    tags TEXT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_archive_articles_external UNIQUE (edition_slug, source_name, external_id)
);

CREATE INDEX IF NOT EXISTS idx_archive_articles_published_at
ON archive_articles(edition_slug, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_archive_articles_source
ON archive_articles(source_name);

CREATE TABLE IF NOT EXISTS archive_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES archive_articles(id) ON DELETE CASCADE,
    embedding VECTOR NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CONSTRAINT archive_embeddings_dimensions_check CHECK (dimensions > 0),
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_archive_embedding_space UNIQUE (article_id, model, dimensions, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_archive_embeddings_article
ON archive_embeddings(article_id);

CREATE INDEX IF NOT EXISTS idx_archive_embeddings_lookup
ON archive_embeddings(model, dimensions, article_id);
