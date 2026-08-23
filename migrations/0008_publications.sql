-- 0008_publications.sql
-- Plan 4 Task 1: PublicationRun, frozen candidate/input sets, generation attempts,
-- immutable publications, and delivery destinations/payloads/attempts.

-- ---------------------------------------------------------------------------
-- Policy versions for eligibility, editorial selection, and writer
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS eligibility_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    version INT NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_eligibility_policy_version UNIQUE (edition_id, version),
    CONSTRAINT uq_eligibility_policy_edition UNIQUE (id, edition_id)
);

CREATE TABLE IF NOT EXISTS editorial_selection_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    version INT NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_editorial_selection_policy_version UNIQUE (edition_id, version),
    CONSTRAINT uq_editorial_selection_policy_edition UNIQUE (id, edition_id)
);

CREATE TABLE IF NOT EXISTS writer_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    version INT NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_writer_policy_version UNIQUE (edition_id, version),
    CONSTRAINT uq_writer_policy_edition UNIQUE (id, edition_id)
);

CREATE TABLE IF NOT EXISTS publication_policy_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    publication_type TEXT NOT NULL,
    eligibility_policy_id BIGINT NOT NULL,
    selection_policy_id BIGINT NOT NULL,
    writer_policy_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (eligibility_policy_id, edition_id) REFERENCES eligibility_policy_versions(id, edition_id) ON DELETE RESTRICT,
    FOREIGN KEY (selection_policy_id, edition_id) REFERENCES editorial_selection_policy_versions(id, edition_id) ON DELETE RESTRICT,
    FOREIGN KEY (writer_policy_id, edition_id) REFERENCES writer_policy_versions(id, edition_id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Publication Runs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publication_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    publication_type TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    snapshot_at TIMESTAMPTZ NOT NULL,
    eligibility_policy_id BIGINT NOT NULL,
    selection_policy_id BIGINT NOT NULL,
    writer_policy_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN ('created', 'candidates_sealed', 'selected_inputs_sealed', 'generating', 'succeeded', 'failed')
    ),
    error_kind TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    FOREIGN KEY (eligibility_policy_id, edition_id) REFERENCES eligibility_policy_versions(id, edition_id) ON DELETE RESTRICT,
    FOREIGN KEY (selection_policy_id, edition_id) REFERENCES editorial_selection_policy_versions(id, edition_id) ON DELETE RESTRICT,
    FOREIGN KEY (writer_policy_id, edition_id) REFERENCES writer_policy_versions(id, edition_id) ON DELETE RESTRICT,
    CONSTRAINT uq_publication_runs_id_edition UNIQUE (id, edition_id)
);

CREATE INDEX IF NOT EXISTS idx_publication_runs_edition_status ON publication_runs(edition_id, status);
CREATE INDEX IF NOT EXISTS idx_publication_runs_snapshot ON publication_runs(snapshot_at);

-- ---------------------------------------------------------------------------
-- Candidates (sealed before selection)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publication_candidates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_run_id BIGINT NOT NULL REFERENCES publication_runs(id) ON DELETE CASCADE,
    story_id BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    story_revision_id BIGINT NOT NULL REFERENCES story_revisions(id) ON DELETE CASCADE,
    deterministic_rank INT NOT NULL,
    snapshot_features JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_publication_candidates_run_story UNIQUE (publication_run_id, story_id),
    CONSTRAINT uq_publication_candidates_run_revision UNIQUE (publication_run_id, story_revision_id),
    CONSTRAINT uq_publication_candidates_id_run UNIQUE (id, publication_run_id)
);

CREATE INDEX IF NOT EXISTS idx_publication_candidates_run ON publication_candidates(publication_run_id);

-- ---------------------------------------------------------------------------
-- Selection Decisions & Selected Inputs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publication_selection_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_run_id BIGINT NOT NULL REFERENCES publication_runs(id) ON DELETE CASCADE,
    candidate_id BIGINT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('INCLUDE', 'OMIT')),
    presentation_intent TEXT NULL CHECK (
        presentation_intent IS NULL OR presentation_intent IN ('lead', 'normal', 'brief', 'unverified_operational', 'follow_up')
    ),
    confidence NUMERIC(4, 3) NULL,
    reason TEXT NULL,
    rank INT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (candidate_id, publication_run_id) REFERENCES publication_candidates(id, publication_run_id) ON DELETE CASCADE,
    CONSTRAINT uq_publication_selection_decision_candidate UNIQUE (publication_run_id, candidate_id),
    CONSTRAINT uq_publication_selection_decisions_id_run UNIQUE (id, publication_run_id)
);

CREATE TABLE IF NOT EXISTS publication_inputs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_run_id BIGINT NOT NULL REFERENCES publication_runs(id) ON DELETE CASCADE,
    story_id BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    story_revision_id BIGINT NOT NULL REFERENCES story_revisions(id) ON DELETE CASCADE,
    selection_decision_id BIGINT NOT NULL,
    presentation_intent TEXT NULL,
    rank INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (selection_decision_id, publication_run_id) REFERENCES publication_selection_decisions(id, publication_run_id) ON DELETE CASCADE,
    CONSTRAINT uq_publication_inputs_run_story UNIQUE (publication_run_id, story_id),
    CONSTRAINT uq_publication_inputs_run_revision UNIQUE (publication_run_id, story_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_publication_inputs_run ON publication_inputs(publication_run_id);

CREATE TABLE IF NOT EXISTS publication_input_claims (
    publication_input_id BIGINT NOT NULL REFERENCES publication_inputs(id) ON DELETE CASCADE,
    claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    PRIMARY KEY (publication_input_id, claim_id)
);

CREATE TABLE IF NOT EXISTS publication_input_evidence_clusters (
    publication_input_id BIGINT NOT NULL REFERENCES publication_inputs(id) ON DELETE CASCADE,
    evidence_cluster_id BIGINT NOT NULL REFERENCES evidence_clusters(id) ON DELETE CASCADE,
    PRIMARY KEY (publication_input_id, evidence_cluster_id)
);

-- ---------------------------------------------------------------------------
-- Generation Attempts & Publications
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publication_generation_attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_run_id BIGINT NOT NULL REFERENCES publication_runs(id) ON DELETE CASCADE,
    attempt_no INT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('writer', 'repair', 'deterministic_fallback', 'story_renderer_fallback')
    ),
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'succeeded', 'failed')
    ),
    error_kind TEXT NULL,
    provider TEXT NULL,
    model TEXT NULL,
    prompt_hash TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_publication_generation_attempts UNIQUE (publication_run_id, attempt_no),
    CONSTRAINT uq_publication_generation_attempts_id_run UNIQUE (id, publication_run_id)
);

CREATE TABLE IF NOT EXISTS publications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_run_id BIGINT NOT NULL UNIQUE REFERENCES publication_runs(id) ON DELETE CASCADE,
    winning_generation_attempt_id BIGINT NOT NULL,
    publication_type TEXT NOT NULL,
    title TEXT NOT NULL,
    lead TEXT NULL,
    body TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (winning_generation_attempt_id, publication_run_id) REFERENCES publication_generation_attempts(id, publication_run_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_publications_type_created ON publications(publication_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- Delivery Destinations, Payloads, Deliveries, and Delivery Attempts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS delivery_destinations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (
        platform IN ('telegram_channel', 'telegraph', 'facebook_page')
    ),
    destination_key TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_delivery_destinations_key UNIQUE (edition_id, platform, destination_key),
    CONSTRAINT uq_delivery_destinations_id_edition UNIQUE (id, edition_id)
);

CREATE TABLE IF NOT EXISTS publication_delivery_payloads (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_id BIGINT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    destination_id BIGINT NOT NULL REFERENCES delivery_destinations(id) ON DELETE CASCADE,
    payload_format TEXT NOT NULL CHECK (
        payload_format IN ('telegram_html', 'telegraph_nodes', 'facebook_post')
    ),
    rendered_content JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_publication_delivery_payloads UNIQUE (publication_id, destination_id),
    CONSTRAINT uq_publication_delivery_payloads_id_pub UNIQUE (id, publication_id)
);

CREATE TABLE IF NOT EXISTS publication_deliveries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_id BIGINT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    destination_id BIGINT NOT NULL REFERENCES delivery_destinations(id) ON DELETE CASCADE,
    payload_id BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'in_progress', 'succeeded', 'failed', 'outcome_unknown')
    ),
    external_delivery_id TEXT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    FOREIGN KEY (payload_id, publication_id) REFERENCES publication_delivery_payloads(id, publication_id) ON DELETE RESTRICT,
    CONSTRAINT uq_publication_deliveries UNIQUE (publication_id, destination_id)
);

CREATE TABLE IF NOT EXISTS publication_delivery_attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publication_delivery_id BIGINT NOT NULL REFERENCES publication_deliveries(id) ON DELETE CASCADE,
    attempt_no INT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'succeeded', 'failed', 'outcome_unknown')
    ),
    error_kind TEXT NULL,
    error_message TEXT NULL,
    response JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_publication_delivery_attempts UNIQUE (publication_delivery_id, attempt_no)
);
