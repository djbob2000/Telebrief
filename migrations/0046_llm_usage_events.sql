-- One row per completed model request: which stage made it, which model answered,
-- token counts and the cost OpenRouter reports in usage.cost (USD).
-- scripts/llm_cost_report.py reads it to show where the money goes.
CREATE TABLE IF NOT EXISTS llm_usage_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL,
    stage text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    origin text NOT NULL,
    response_id text,
    prompt_tokens integer,
    cached_tokens integer,
    completion_tokens integer,
    reasoning_tokens integer,
    cost_usd numeric(14, 8),
    story_count integer,
    finish_reason text,
    elapsed_ms integer
);

CREATE INDEX IF NOT EXISTS llm_usage_events_created_at_idx
    ON llm_usage_events (created_at);
