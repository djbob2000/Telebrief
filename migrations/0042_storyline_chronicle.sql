-- 0042_storyline_chronicle.sql
-- Storyline chronicle: the permanent editorial memory behind period reviews.
--
-- A storyline is one continuing situation, place or subject inside a theme
-- (power, water, transport, ...). Every Story with a current revision is
-- linked to one storyline; Stories that do not join a named storyline are
-- linked to their theme's catch-all storyline. Chronicle entries summarise a
-- storyline for one period (a calendar week clipped to its month, a month,
-- quarter, half-year or year). Entries are navigation, never citable
-- evidence: every fact keeps the source fragment refs it was grounded in.

CREATE TABLE IF NOT EXISTS storylines (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    theme TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'storyline'
        CONSTRAINT storylines_kind_check CHECK (kind IN ('storyline', 'theme_other')),
    title TEXT NOT NULL,
    areas JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    story_count INTEGER NOT NULL DEFAULT 0 CHECK (story_count >= 0),
    chronicle_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_storylines_theme_other
    ON storylines(edition_id, theme) WHERE kind = 'theme_other';

CREATE INDEX IF NOT EXISTS idx_storylines_active
    ON storylines(edition_id, theme, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS story_storyline_links (
    story_id BIGINT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    storyline_id BIGINT NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    method TEXT NOT NULL
        CONSTRAINT story_storyline_links_method_check
        CHECK (method IN ('chronicle_model', 'theme_other')),
    period_start DATE NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_storyline_links_storyline
    ON story_storyline_links(storyline_id, period_start);

CREATE TABLE IF NOT EXISTS chronicle_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    storyline_id BIGINT NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    period_kind TEXT NOT NULL
        CONSTRAINT chronicle_entries_period_kind_check
        CHECK (period_kind IN ('week', 'month', 'quarter', 'half_year', 'year')),
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    chronicle_version TEXT NOT NULL,
    method TEXT NOT NULL
        CONSTRAINT chronicle_entries_method_check CHECK (method IN ('model', 'deterministic')),
    model TEXT NULL,
    title TEXT NOT NULL,
    trajectory TEXT NOT NULL DEFAULT '',
    facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    details JSONB NOT NULL DEFAULT '[]'::jsonb,
    weight JSONB NOT NULL DEFAULT '{}'::jsonb,
    ref_texts JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chronicle_entries_period_check CHECK (period_start < period_end),
    CONSTRAINT uq_chronicle_entries_period
        UNIQUE (edition_id, storyline_id, period_kind, period_start, chronicle_version)
);

CREATE INDEX IF NOT EXISTS idx_chronicle_entries_period
    ON chronicle_entries(edition_id, period_kind, period_start, chronicle_version);

-- One row per completed chronicle period. A period review is ready when the
-- row for its period exists; weeks are marked when their entries are written.
CREATE TABLE IF NOT EXISTS chronicle_periods (
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    period_kind TEXT NOT NULL
        CONSTRAINT chronicle_periods_period_kind_check
        CHECK (period_kind IN ('week', 'month', 'quarter', 'half_year', 'year')),
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    chronicle_version TEXT NOT NULL,
    story_count INTEGER NOT NULL DEFAULT 0,
    entry_count INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    coverage_start DATE NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chronicle_periods_period_check CHECK (period_start < period_end),
    PRIMARY KEY (edition_id, period_kind, period_start, chronicle_version)
);
