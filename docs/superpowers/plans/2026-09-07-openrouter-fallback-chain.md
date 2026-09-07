# OpenRouter Fallback Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable OpenRouter multi-model fallback chains configured via a single comma-separated `OPENROUTER_MODEL` variable so that rate-limited or unavailable models seamlessly fail over to backup models.

**Architecture:** Parse comma-separated models in `config/loader.py`, include all configured models in the `ai_providers` allowlist, and dynamically instantiate a `ProviderCascade` with slots `openrouter-1` .. `openrouter-N` in `create_provider()`.

**Tech Stack:** Python 3.12+, Pydantic v2, Pytest, OpenAI SDK / HTTPX async.

---

### Task 1: Allowlist and ProviderCascade for Multiple OpenRouter Models

**Files:**
- Modify: `src/ai_providers.py`
- Test: `tests/test_openrouter_fallback_chain.py`

- [ ] **Step 1.1**: Write failing unit tests in `tests/test_openrouter_fallback_chain.py` testing:
  - `get_allowed_ai_models()` splits `OPENROUTER_MODEL` by comma.
  - `validate_model_allowed()` permits any model present in the comma-separated list.
  - `create_provider("openrouter", ..., openrouter_models=["m1", "m2", "m3"])` creates `ProviderCascade` with 3 slots.
- [ ] **Step 1.2**: Run the test to verify it fails (`pytest tests/test_openrouter_fallback_chain.py`).
- [ ] **Step 1.3**: Implement in `src/ai_providers.py`:
  - In `get_allowed_ai_models()`: parse `OPENROUTER_MODEL` by splitting on `,` and stripping items.
  - In `create_provider()`: accept `openrouter_models: Sequence[str] | None = None`. If `len(openrouter_models) > 1`, instantiate `ProviderCascade` with slots `openrouter-1`, `openrouter-2`, ..., `openrouter-N`.
- [ ] **Step 1.4**: Run unit tests to verify they pass.

---

### Task 2: Config Loader & Schema Parsing

**Files:**
- Modify: `src/config/schemas/root.py`
- Modify: `src/config/loader.py`
- Test: `tests/config/test_config_schemas.py`

- [ ] **Step 2.1**: Write failing unit test for comma-separated `OPENROUTER_MODEL` parsing in `tests/config/test_config_schemas.py`.
- [ ] **Step 2.2**: Implement in `src/config/schemas/root.py`: add `openrouter_models: list[str] = Field(default_factory=list)`.
- [ ] **Step 2.3**: Implement in `src/config/loader.py`: parse `OPENROUTER_MODEL` by comma, populating `openrouter_models` and setting `openrouter_model = openrouter_models[0]`.
- [ ] **Step 2.4**: Run config unit tests to verify they pass.

---

### Task 3: Wire Callers Across Pipeline

**Files:**
- Modify: `src/jobs/event_processing.py`
- Modify: `src/publication/editorializer.py`
- Modify: `src/publication/selection_ai.py`
- Modify: `src/article_generator.py`
- Modify: `src/summarizer.py`
- Modify: `src/grouper.py`

- [ ] **Step 3.1**: Pass `openrouter_models=getattr(config, "openrouter_models", None)` to `create_provider` in all callers.
- [ ] **Step 3.2**: Run pipeline tests (`pytest tests/jobs/ tests/publication/`) to confirm no regressions.

---

### Task 4: Configure .env.example, .env, and Deploy to Server

**Files:**
- Modify: `.env.example`
- Modify: `.env`

- [ ] **Step 4.1**: Set `OPENROUTER_MODEL=` in `.env.example` (clean without examples).
- [ ] **Step 4.2**: Set `OPENROUTER_MODEL=minimax/minimax-m3:free,minimax/minimax-m2.7:free,thinkingmachines/inkling:free,google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free,thinkingmachines/inkling-small:free,nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3.5-lightning:free` in local `.env`.
- [ ] **Step 4.3**: Commit changes, push to `dev`, pull on server, update server `.env`, and recreate containers.
- [ ] **Step 4.4**: Verify publication/digest on server.
