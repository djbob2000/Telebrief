# Provider fallback cascade

## Goal

Keep article generation available when the active model account reaches a quota,
rate limit, authentication failure, transient network error, server error, or
returns an empty response.

## Design

`create_provider()` will build a provider cascade in this order:

1. `GOOGLE_API_KEY` with the configured Gemini model;
2. `GOOGLE_API_KEY_2` with the same model;
3. `GOOGLE_API_KEY_3` with the same model;
4. `OPENROUTER_API_KEY` with `openrouter/free` (overridable in settings).

The cascade implements the existing `AIProvider` interface. Each completion is
attempted once on the current slot; recoverable provider failures move to the
next slot. Error messages are logged without API keys. If every slot fails, the
cascade raises one error containing provider names and the last failure, allowing
the article retry/fallback path to handle the outage.

## Configuration

Existing single-key configuration remains valid. The additional keys are optional
environment variables. OpenRouter uses its own key and base URL, with model and
URL configurable under the article/AI settings while defaulting to
`openrouter/free` and `https://openrouter.ai/api/v1`.

## Safety and compatibility

No article prompt or source-processing behavior changes. Provider selection is
transparent to `ArticleGenerator`; tests cover ordering, skipped empty slots,
recoverable errors, and all-slots failure.
