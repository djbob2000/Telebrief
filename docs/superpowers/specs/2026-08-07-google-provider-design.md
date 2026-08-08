# Google Gemini Provider Design

## Goal

Add Google Gemini as an explicit AI provider in Telebrief while preserving the
current DeepSeek/OpenAI, Ollama, and Anthropic behavior.

## Configuration

The provider is selected in `config.yaml`:

```yaml
settings:
  ai_provider: "google"
  ai_model: "gemini-3.6-flash"
```

The API key is read from `.env` as `GEMINI_API_KEY`. The existing
`OPENAI_API_KEY` remains required only for `ai_provider: openai`; Google does
not reuse or overwrite it.

Google uses its official OpenAI-compatible endpoint:

```text
https://generativelanguage.googleapis.com/v1beta/openai/
```

## Architecture

Add a `GoogleProvider` alongside the existing provider classes. It will reuse
the existing `AsyncOpenAI` client because the Google endpoint implements the
Chat Completions interface. The provider will build only Google-compatible
request parameters:

- `model`, `messages`;
- `max_completion_tokens` when a budget is configured;
- `reasoning_effort` when configured, mapped to Gemini thinking levels by the
  compatibility layer;
- no DeepSeek-specific `thinking` body and no provider-specific fallback to
  `max_tokens`.

The provider will use the same response extraction and empty-response/error
handling contract as the other providers, but will log itself as Google.

The factory will accept `google_api_key`, validate it for the `google`
provider, and construct `GoogleProvider`. The existing `OpenAIProvider` will
continue to handle DeepSeek through `OPENAI_BASE_URL` exactly as it does now.

## Error handling

- Missing `GEMINI_API_KEY` produces a clear configuration error.
- Unknown providers list `google` among supported values.
- An API error is propagated with the provider's existing logging behavior.
- No automatic fallback between Google and DeepSeek is added; switching is
  explicit in `config.yaml`.

## Testing

Add tests covering:

1. configuration accepts `ai_provider: google` and resolves the default model;
2. configuration requires `GEMINI_API_KEY` only for Google;
3. the factory creates `GoogleProvider` and passes Google's endpoint;
4. a Google request contains Gemini-compatible parameters and excludes the
   DeepSeek-only `thinking` field;
5. existing OpenAI/DeepSeek provider tests remain unchanged and pass.

## Documentation

Update `config.yaml.example` and README provider/environment documentation with
the Google provider, model example, endpoint behavior, and `GEMINI_API_KEY`.
