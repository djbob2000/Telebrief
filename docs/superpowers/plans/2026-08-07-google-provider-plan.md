# Google Gemini Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Subagents are prohibited by this repository's `AGENTS.md`.

**Goal:** Add an explicit `google` AI provider for Gemini 3.6 Flash without changing the existing DeepSeek/OpenAI, Ollama, or Anthropic behavior.

**Architecture:** Extend the existing provider abstraction with a dedicated `GoogleProvider` backed by `AsyncOpenAI` and Google's OpenAI-compatible endpoint. Thread a separate `GEMINI_API_KEY` through configuration and factory creation, and keep Google request construction separate from DeepSeek-specific request logic.

**Tech Stack:** Python 3, asyncio, `openai` Python SDK, PyYAML, pytest, existing Telebrief provider abstraction.

## Global Constraints

- `ai_provider: "google"` selects Google explicitly; there is no automatic fallback.
- Google credentials come from `GEMINI_API_KEY`; `OPENAI_API_KEY` remains required only for `openai`.
- The Google default model is `gemini-3.6-flash`.
- The DeepSeek path and its `OPENAI_BASE_URL` configuration must remain unchanged.
- Do not commit credentials or modify unrelated dirty-worktree files.

### Task 1: Add Google configuration and factory contract

**Files:**
- Modify: `src/config_loader.py`
- Modify: `src/ai_providers.py`
- Test: `tests/test_config_loader.py`
- Test: `tests/test_ai_providers.py`

**Interfaces:**
- `Config.google_api_key: str` stores the environment value.
- `create_provider(..., google_api_key="...")` creates `GoogleProvider` for `provider_name="google"`.
- `_resolve_ai_settings()` accepts `google` and defaults its model to `gemini-3.6-flash`.

- [ ] **Step 1: Write failing configuration tests**

Add tests that load a Google config and assert `ai_provider == "google"`,
`ai_model == "gemini-3.6-flash"`, and `config.google_api_key` comes from
`GEMINI_API_KEY`. Add a test that removes `GEMINI_API_KEY` and expects the
missing-variable error to name it. Update the test environment fixture only in
the individual Google test so existing OpenAI tests retain their current setup.

- [ ] **Step 2: Run the configuration tests and verify the expected failure**

Run:

```bash
pytest -q tests/test_config_loader.py -k google
```

Expected: failures because `google` is not a supported provider and
`Config` has no Google key field.

- [ ] **Step 3: Write failing factory tests**

Import `GoogleProvider` and add tests that create it with a Google key, assert
the returned type, and assert its client receives:

```python
base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
```

Add a missing-key test expecting `GEMINI_API_KEY is required`.

- [ ] **Step 4: Run the factory tests and verify the expected failure**

Run:

```bash
pytest -q tests/test_ai_providers.py -k google
```

Expected: collection or assertion failures because the new provider and
factory branch do not exist yet.

- [ ] **Step 5: Implement configuration plumbing**

Add `google_api_key` to `Config`, load `GEMINI_API_KEY` in
`_load_and_validate_env_vars()`, require it only when `ai_provider ==
"google"`, return it from that function, and pass it into `Config` in
`load_config()`. Add `google` to `_SUPPORTED_PROVIDERS` and
`_PROVIDER_DEFAULT_MODELS`.

- [ ] **Step 6: Implement the factory branch**

Add the `google` branch to `create_provider()`, validate the key, and
construct `GoogleProvider` with the fixed official endpoint and configured
timeout. Update factory docstrings and unknown-provider messages to include
Google.

- [ ] **Step 7: Run the focused tests and verify they pass**

Run:

```bash
pytest -q tests/test_config_loader.py -k google tests/test_ai_providers.py -k google
```

Expected: all selected tests pass.

### Task 2: Implement Google request behavior

**Files:**
- Modify: `src/ai_providers.py`
- Test: `tests/test_ai_providers.py`

**Interfaces:**
- `GoogleProvider` implements `AIProvider.chat_completion()` with the same
  return/error contract as `OpenAIProvider`.
- Google requests use `model`, `messages`, `max_completion_tokens` when
  configured, and `reasoning_effort` when supplied.
- Google requests never include DeepSeek's `extra_body.thinking` payload.

- [ ] **Step 1: Write the failing request-shape test**

Patch `AsyncOpenAI`, return a minimal completion response, call
`GoogleProvider.chat_completion()` with `reasoning_effort="high"` and
`thinking=True`, then assert the request contains `reasoning_effort` and
`max_completion_tokens`, has the Google model/messages, and does not contain
`extra_body`.

- [ ] **Step 2: Run the request test and verify it fails**

Run:

```bash
pytest -q tests/test_ai_providers.py -k google_request
```

Expected: failure because `GoogleProvider` has not implemented the request.

- [ ] **Step 3: Implement the minimal Google provider**

Create `GoogleProvider` next to `OpenAIProvider`. Initialize `AsyncOpenAI`
with the Google endpoint, build the Google-compatible request without the
DeepSeek-only thinking payload, call `chat.completions.create()`, and reuse a
small shared response-extraction helper so OpenAI and Google keep identical
empty-content and truncation handling without duplicating the full method.

- [ ] **Step 4: Run the request test and the full provider tests**

Run:

```bash
pytest -q tests/test_ai_providers.py
```

Expected: all provider tests pass with no change to existing OpenAI/DeepSeek
request assertions.

### Task 3: Update user-facing configuration and documentation

**Files:**
- Modify: `config.yaml.example`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add Google to the configuration example**

Document `google` as a supported provider, `GEMINI_API_KEY` as its required
credential, and `gemini-3.6-flash` as the example model.

- [ ] **Step 2: Add the separate environment variable example**

Add a commented `GEMINI_API_KEY` entry to `.env.example` without adding a real
key.

- [ ] **Step 3: Update README provider setup**

Mention Google Gemini in the supported-provider overview, setup instructions,
and provider list. Explain that selecting Google uses the Gemini key and that
switching remains explicit through `ai_provider`.

- [ ] **Step 4: Verify documentation contains no secrets or stale provider list**

Run:

```bash
rg -n "OpenAI.*Ollama.*Anthropic|ai_provider|GEMINI_API_KEY|gemini-3.6-flash" README.md config.yaml.example .env.example
```

Expected: Google appears in the supported-provider documentation and no
literal API key appears.

### Task 4: Full verification

**Files:**
- Test: existing test suite

- [ ] **Step 1: Run all unit tests**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run static checks**

Run:

```bash
ruff check src tests
```

Expected: no new lint errors.

- [ ] **Step 3: Inspect the final diff**

Run:

```bash
git diff -- src/ai_providers.py src/config_loader.py tests/test_ai_providers.py tests/test_config_loader.py config.yaml.example .env.example README.md
git status --short
```

Confirm that only the planned files changed in the feature diff and unrelated
pre-existing worktree modifications remain untouched.
