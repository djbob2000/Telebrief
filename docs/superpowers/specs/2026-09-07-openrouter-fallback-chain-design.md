# OpenRouter Multi-Model Fallback Chain Design

## Problem Statement
When using OpenRouter as the primary AI provider (especially with free or rate-limited tiers such as Minimax M3), daily or per-minute rate limits (e.g., HTTP 429 `limit_rpd`) can interrupt pipeline execution (triage, summarization, publication). Currently, the system supports at most two hardcoded models (`OPENROUTER_MODEL` and `OPENROUTER_MODEL_2`). When both fail or hit quotas, execution fails.

The goal is to support an arbitrary prioritized sequence of models configured via a single comma-separated `OPENROUTER_MODEL` environment variable, enabling automatic failover through the cascade without code modification.

## Configuration & Environment
- In `.env.example`:
  `OPENROUTER_MODEL=` (empty, no default model names or examples committed).
- In `.env`:
  `OPENROUTER_MODEL=minimax/minimax-m3:free,minimax/minimax-m2.7:free,thinkingmachines/inkling:free,google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free,thinkingmachines/inkling-small:free,nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3.5-lightning:free`

## System Behavior

### 1. Config Loader & Schemas
- In `src/config/schemas/root.py`:
  - `openrouter_models: list[str] = Field(default_factory=list)`
  - `openrouter_model: str = "openrouter/free"` (holds first model in chain for backward compatibility)
- In `src/config/loader.py`:
  - Parse `os.getenv("OPENROUTER_MODEL", "")`:
    Split on commas `,`, strip whitespace, filter non-empty items.
    If multiple items exist:
      `openrouter_models = parsed_list`
      `openrouter_model = parsed_list[0]`
    If single item or empty:
      `openrouter_models = [raw_model]` if raw_model else []
      `openrouter_model = raw_model or "openrouter/free"`
    Backward compatibility: if `OPENROUTER_MODEL_2` is set and not already in `openrouter_models`, append it to `openrouter_models`.

### 2. Runtime Model Allowlist
- In `src/ai_providers.py`:
  - Update `get_allowed_ai_models()`:
    When reading `OPENROUTER_MODEL`, split by `,` and add all stripped entries to the allowed set.
    All models declared in the comma-separated list pass `validate_model_allowed()`.

### 3. Provider Construction & Cascade
- In `src/ai_providers.py:create_provider()`:
  - Add parameter `openrouter_models: Sequence[str] | None = None`.
  - If `openrouter_models` is not explicitly provided:
    Derive it from `openrouter_model` (splitting by comma if needed) plus `openrouter_model_2` if present.
  - If `len(openrouter_models) > 1`:
    Construct a `ProviderCascade` with slots named `openrouter-1`, `openrouter-2`, ..., `openrouter-N`.
    Each slot wraps an `OpenAIProvider` targeting `openrouter_base_url` with that slot's specific model.
    Validate that each model in the sequence is in the allowlist.
    Return the `ProviderCascade`.
  - If `len(openrouter_models) == 1`:
    Return a single `OpenAIProvider` with `openrouter_models[0]`.

### 4. Callers in Application
- In `src/jobs/event_processing.py`, `src/publication/editorializer.py`, `src/publication/selection_ai.py`, `src/summarizer.py`, `src/grouper.py`, `src/article_generator.py`:
  Pass `openrouter_models=getattr(config, "openrouter_models", None)` to `create_provider()`.

## Failure Semantics & Cooldown
- When a slot in `ProviderCascade` receives a rate limit error (HTTP 429) or connection failure, the existing `ProviderCascade` mechanism puts that slot into cooldown (default 900s / 15m) and proceeds immediately to the next slot in the chain.
- If all slots fail, `ProviderUnavailableError` is raised.

## Verification Plan
- Unit tests verifying:
  1. Comma-separated `OPENROUTER_MODEL` parses correctly into `config.openrouter_models` and `config.openrouter_model`.
  2. `get_allowed_ai_models()` returns all models from the comma-separated list.
  3. `create_provider("openrouter", ...)` creates a `ProviderCascade` with N slots when N > 1 models are provided.
  4. Cascade failover: slot 1 fails with RateLimitError -> request seamlessly served by slot 2.
