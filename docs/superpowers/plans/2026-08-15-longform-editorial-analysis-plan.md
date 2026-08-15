# Compact Editorial Analysis and Long-Form Article Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep full-day editorial analysis as the primary path while bounding the analyzer’s response to a small set of important Story Cards so the free-form writer can reliably produce a cohesive long-form daily article.

**Architecture:** The complete prepared 24-hour bundle remains the analyzer’s input. `EditorialAnalyzer` compresses the day, not the evidence: it returns roughly 4–8 significant Story Cards with representative evidence refs instead of labels or claims for every message. `ArticleGenerator` selects the referenced source records for the writer and light fact-checker, while the deterministic thematic renderer remains the final emergency fallback only.

**Tech Stack:** Python 3.14, dataclasses, asyncio, existing `AIProvider`/provider cascade, Telethon `Message`, pytest/pytest-asyncio, existing Markdown/Telegraph delivery.

## Global Constraints

- `EditorialAnalyzer` sees the complete prepared 24-hour bundle on the primary path; it must not emit output proportional to the number of input messages.
- The normal analysis target is approximately 4–8 significant Story Cards; this is editorial guidance, not a per-message claim ledger.
- Each Story Card keeps a limited representative evidence set, normally about 3–8 refs per story; more refs are allowed when needed for several districts, source-role differences or contradictions.
- `source_type` remains an editorial prior (`news`, `community`, `official`, `classifieds`, `mixed`), never a trust score or permission to state a fact.
- `TokenBudgetExhaustedError` means “retry the same full input in compact-analysis mode”; it never activates context batching.
- Explicit context/request-size rejection activates the existing batching path; timeout, quota and ordinary provider failures use provider cascade/retry and never activate batching.
- Batching does not change editorial semantics. Batch refs are validated against their batch; the merged result is validated against the original complete bundle.
- Writer receives selected Story Cards plus selected original source excerpts, not the full daily corpus. It may synthesize and connect supplied material but may not add a new independently verifiable fact.
- Target article shape is soft: usually 8–12 substantive paragraphs; roughly 900–1500 words on a sufficiently busy day, up to about 1800 for genuinely strong material, and 600–900 words when the source day is thin. Never pad for length and never reject a good article for missing a word count.
- `WARN` never blocks a structurally valid article; targeted repair remains bounded and does not trigger an infinite audit loop.
- Deterministic fallback remains a compact thematic digest, never the normal long-form writer and never a raw-message dump.
- Preserve the existing morning digest, Telegram/Telegraph delivery, provider-key cascade, source-role configuration and all unrelated user changes.

---

### Task 1: Bound Story Card analysis without changing context semantics

**Files:**

- Modify: `src/ai_providers.py` (`_classify_provider_failure`)
- Modify: `src/editorial_analysis.py` (`build_prompt`, `analyze`, `analyze_batched`, provider-call parsing)
- Test: `tests/test_ai_providers.py`
- Test: `tests/test_editorial_analysis.py`

**Interfaces:**

- `EditorialAnalyzer.build_prompt(bundle: PreparedBundle, *, compact: bool = False) -> tuple[str, str]`
- `async EditorialAnalyzer.analyze(bundle: PreparedBundle, *, compact: bool = False) -> EditorialAnalysis`
- `async EditorialAnalyzer.analyze_batched(bundle: PreparedBundle, *, compact: bool = False) -> EditorialAnalysis`
- `EditorialAnalysisError.reason == "token_budget"` for direct or cascaded token-budget exhaustion.
- `ProviderCascadeError.failure_kinds` may include `token_budget`; `context_only` remains true only when every slot is `context_size`.

- [ ] **Step 1: Add failing provider-classification tests**

Extend the provider tests with a `TokenBudgetExhaustedError` slot failure and assert that the cascade exposes `failure_kinds == ("token_budget",)` without copying provider exception text into the public error. Keep the existing assertions that quota, timeout and context-size failures retain their current kinds.

```python
def test_provider_classifies_token_budget_exhaustion(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=TokenBudgetExhaustedError("secret provider details")
    )
    cascade = ProviderCascade([("primary", provider)], mock_logger)

    with pytest.raises(ProviderCascadeError) as error:
        asyncio.run(cascade.chat_completion([], "model", 0.2, 100))

    assert error.value.failure_kinds == ("token_budget",)
    assert "secret provider details" not in str(error.value)
```

- [ ] **Step 2: Run the focused provider test and verify it fails**

Run:

```bash
uv run pytest -q tests/test_ai_providers.py -k token_budget --no-cov
```

Expected: FAIL because `_classify_provider_failure` currently treats token-budget exhaustion as `other`.

- [ ] **Step 3: Implement the distinct token-budget classification**

Add token-budget markers (`token budget`, `finish_reason=length`, `max_tokens`, and the `TokenBudgetExhaustedError` type) before the generic `other` branch. Preserve safe logging: store only failure kinds, slot labels and exception classes.

- [ ] **Step 4: Add failing bounded-analysis tests**

Update the analysis prompt tests to assert that the full input is still present, while the instructions explicitly say:

```text
Select approximately 4–8 significant stories for the day.
Do not classify or label every supplied message.
Do not repeat source text in the JSON.
Keep only representative source refs needed to support each story, including extra refs only for meaningful geographic spread or contradictions.
The response size must depend on the number of stories, not the number of source messages.
```

Add a compact-mode assertion that the prompt requests fewer cards, omits exhaustive labels, and still contains every supplied source record. Add a parser test for a valid 4–8-card response with element-level refs and no `labels` key.

- [ ] **Step 5: Run the focused analysis tests and verify the new expectations fail**

Run:

```bash
uv run pytest -q tests/test_editorial_analysis.py -k "prompt or compact or card" --no-cov
```

Expected: FAIL because the current prompt requests a complete classification and has no compact mode.

- [ ] **Step 6: Implement normal and compact analysis prompts**

Add the `compact` keyword to the three analyzer entry points. The normal prompt must request roughly 4–8 cards and no per-message labels; compact mode must request approximately 3–6 cards, omit optional labels/excluded refs, keep only representative refs, and return the smallest complete JSON. Keep the full `bundle.prompt_text` unchanged in both modes.

Use `max_tokens=8000` for compact analysis, which is enough for the bounded JSON while preventing an unnecessarily large response budget. Do not alter the input context or invoke batching from this task. Parse both the new Story Card shape and the existing legacy `claims` shape for backward compatibility.

- [ ] **Step 7: Map token-budget errors without conflating context errors**

Catch `TokenBudgetExhaustedError` explicitly in `_call_messages` and emit `EditorialAnalysisError(stage="provider_call", reason="token_budget")`. Map cascaded `failure_kinds` in the existing provider wrapper. Leave `ContextSizeRejectedError` as the only signal that can activate batching.

- [ ] **Step 8: Verify analysis behavior and commit**

Run:

```bash
uv run pytest -q tests/test_ai_providers.py tests/test_editorial_analysis.py --no-cov
uv run flake8 src/ai_providers.py src/editorial_analysis.py tests/test_ai_providers.py tests/test_editorial_analysis.py
```

Commit only the task files:

```bash
git add src/ai_providers.py src/editorial_analysis.py tests/test_ai_providers.py tests/test_editorial_analysis.py
git commit -m "feat: bound editorial analysis to significant stories"
```

### Task 2: Build a selected reporting bundle for writer and audit

**Files:**

- Modify: `src/editorial_input.py` (`select_records`)
- Modify: `src/editorial_models.py` (`PreparedBundle` helper if needed)
- Test: `tests/test_editorial_input.py`
- Test: `tests/test_editorial_models.py`

**Interfaces:**

- `EditorialInputBuilder.select_records(bundle: PreparedBundle, refs: Iterable[str], *, max_refs: int = 96) -> PreparedBundle`
- The returned bundle preserves original `SourceRecord.ref`, `message_id`, `parent_ref`, `context_text`, `source_type`, and source order.

- [ ] **Step 1: Add failing selected-bundle tests**

Create a fixture with two Story Cards referencing messages from different channels, a short reply whose parent is not itself a card ref, and unrelated records. Assert that selection includes requested refs and their available reply parents, preserves the composite source identity, and excludes unrelated messages.

```python
selected = builder.select_records(bundle, ["S000014", "S000087"])
assert set(selected.records) == {"S000014", "S000087", "S000013"}
assert selected.records["S000014"].source_type == "community"
assert 'reply_to: "У кого сейчас есть вода?"' in selected.prompt_text
```

Add a test that more than `max_refs` candidates are reduced deterministically by story-selection order, without rewriting or renumbering refs. The limit is a context safety guard, not a sentence-level evidence ledger.

- [ ] **Step 2: Run the selected-bundle tests and verify failure**

```bash
uv run pytest -q tests/test_editorial_input.py -k selected --no-cov
```

Expected: FAIL because no selected-bundle method exists.

- [ ] **Step 3: Implement `select_records`**

Resolve refs in caller order, preserve each selected card’s representative evidence, add one available `parent_ref` for reply context, and render the selected records with the existing source-role/time/reply format. Do not create new refs, rewrite text, or re-run the noise filter: the full `PreparedBundle` is already cleaned.

If the selected set is empty, return a valid empty-record bundle only for internal handling; `ArticleGenerator` must fall back to the complete bundle’s safe path rather than publish an unsupported article.

- [ ] **Step 4: Verify the helper and commit**

Run:

```bash
uv run pytest -q tests/test_editorial_input.py tests/test_editorial_models.py --no-cov
uv run flake8 src/editorial_input.py src/editorial_models.py tests/test_editorial_input.py tests/test_editorial_models.py
```

```bash
git add src/editorial_input.py src/editorial_models.py tests/test_editorial_input.py tests/test_editorial_models.py
git commit -m "feat: select representative reporting excerpts"
```

### Task 3: Align writer and skill with the long-form editorial contract

**Files:**

- Modify: `src/editorial_writer.py` (`build_prompt`)
- Modify: `.agents/skills/news-style/SKILL.md`
- Test: `tests/test_editorial_writer.py`
- Test: `tests/test_article_generator.py`

**Interfaces:**

- `EditorialWriter.write(analysis: EditorialAnalysis, bundle: PreparedBundle) -> ArticleDraft` keeps its public signature; the caller supplies the selected bundle.
- The writer returns strict JSON and remains free to choose sections only when supported by material.

- [ ] **Step 1: Add failing writer-contract tests**

Assert that the writer prompt says Story Cards are reporting notes, the source excerpts are selected evidence, natural synthesis and transitions are allowed, and the article should target the following soft shape without padding:

```text
Usually write 8–12 substantive paragraphs. Aim for about 900–1500 words on a busy day, allow up to about 1800 when the material genuinely supports it, and accept 600–900 words on a thin day. These are targets, not validation limits.
```

Assert that a selected bundle’s source text is present and an unrelated full-bundle message is absent from the writer user prompt.

- [ ] **Step 2: Run the writer tests and verify the new expectation fails**

```bash
uv run pytest -q tests/test_editorial_writer.py tests/test_article_generator.py -k "prompt or selected" --no-cov
```

- [ ] **Step 3: Update the writer prompt**

Keep the evidence boundary and attribution rules, but explicitly permit combining, reordering, compressing and connecting Story Card material. Add these exact soft targets: usually 8–12 substantive paragraphs; about 900–1500 words on a busy day; up to about 1800 words only when the material genuinely supports it; and 600–900 words on a thin day. Never require exact word/paragraph counts, never request internal refs in output, and do not require a section for every card.

- [ ] **Step 4: Update the skill’s product contract**

In `.agents/skills/news-style/SKILL.md`, preserve source locking, attribution, high-risk and privacy rules, but replace any implication that a short brief is the default for a sufficiently rich day with the approved soft long-form target. State that a daily article should become a coherent narrative from several supported stories, while thin source material may remain shorter. Do not reintroduce claim-ID or sentence-level proof requirements.

- [ ] **Step 5: Run writer/skill tests and commit**

```bash
uv run pytest -q tests/test_editorial_writer.py tests/test_article_generator.py --no-cov
uv run flake8 src/editorial_writer.py tests/test_editorial_writer.py tests/test_article_generator.py
git diff --check
```

```bash
git add src/editorial_writer.py .agents/skills/news-style/SKILL.md tests/test_editorial_writer.py tests/test_article_generator.py
git commit -m "feat: target cohesive long-form daily articles"
```

### Task 4: Orchestrate compact retry, context batching and selected writer input

**Files:**

- Modify: `src/article_generator.py` (`_analyze`, `generate_article`, debug artifacts)
- Test: `tests/test_article_generator.py`
- Test: `tests/test_article_pipeline.py`

**Interfaces:**

- `ArticleGenerator._analyze(bundle: PreparedBundle) -> EditorialAnalysis`
- Internal state machine:

```text
full bundle + normal analysis
  ├─ PASS → select representative refs → writer → audit/repair
  ├─ token_budget → full bundle + compact analysis (one mode switch)
  ├─ context_size → existing explicit batching (normal/compact mode preserved)
  └─ timeout/quota/provider → existing provider retry/cascade; never batch
```

- Writer, fact-check and repair consume the selected writer bundle. Deterministic fallback consumes the complete cleaned bundle.

- [ ] **Step 1: Add failing orchestration tests**

Add a provider sequence where the first analysis call raises `EditorialAnalysisError(reason="token_budget")`, the compact analysis returns valid Story Cards, the writer returns an 8–12-paragraph draft, and the audit passes. Assert that the writer is called and the deterministic fallback is not.

Add a context-size sequence and assert that `analyze_batched()` is called without compact retry. Add timeout/quota sequences and assert that the existing retry path is used without batching. Add an assertion that the writer prompt contains only the selected source refs.

```python
assert analyzer.analyze.call_args_list[0].kwargs == {"compact": False}
assert analyzer.analyze.call_args_list[1].kwargs == {"compact": True}
assert not fallback_builder.build.called
```

- [ ] **Step 2: Run the orchestration tests and verify failure**

```bash
uv run pytest -q tests/test_article_generator.py tests/test_article_pipeline.py -k "compact or batching or selected" --no-cov
```

- [ ] **Step 3: Implement the compact-analysis state machine**

On the first token-budget failure, switch once to `analyzer.analyze(bundle, compact=True)` using the same full bundle. Do not sleep or repeat the same normal prompt. If the compact call explicitly rejects context size, call `analyze_batched(bundle, compact=True)`. If compact analysis fails for timeout/quota/provider reasons, apply the configured provider retry policy to the compact mode and then use the existing fallback. Do not classify token-budget failure as context-size failure.

Keep existing normal retries for timeout/quota/provider failures. Log mode and branch explicitly, for example:

```text
Editorial analysis token budget exhausted; retrying full bundle in compact-analysis mode
Editorial analysis exceeded model context; using explicit context batching
```

- [ ] **Step 4: Select source excerpts after analysis**

Collect `StoryCard.all_source_refs()` in card order, preserve meaningful refs for high-importance cards and contradictions, call `input_builder.select_records`, and save an opt-in `writer_input.txt` artifact. Do not pass the complete 2000-message prompt to the writer when selected refs exist. Use the full cleaned bundle only for deterministic fallback and for the analyzer.

- [ ] **Step 5: Route writer and audit through the selected bundle**

Call `writer.write(analysis, writer_bundle)` and `_repair_and_check(draft, analysis, writer_bundle)`. Keep fallback calls as `_fallback(bundle, reason)`. If selection yields no records, retain Story Cards as the writer’s notes but use a bounded source subset chosen from the highest-priority `news`/`official` records; never revert to a raw latest-message slice.

- [ ] **Step 6: Run orchestration tests and commit**

```bash
uv run pytest -q tests/test_article_generator.py tests/test_article_pipeline.py --no-cov
uv run flake8 src/article_generator.py tests/test_article_generator.py tests/test_article_pipeline.py
uv run mypy src/article_generator.py
```

```bash
git add src/article_generator.py tests/test_article_generator.py tests/test_article_pipeline.py
git commit -m "feat: retry compact analysis before long-form writing"
```

### Task 5: Add a realistic full-day long-form quality regression

**Files:**

- Create: `tests/fixtures/editorial_longform_day.json`
- Create: `tests/test_editorial_integration.py`
- Modify: `tests/conftest.py` only if a reusable fixture factory is required

**Interfaces:**

- The fixture contains 5–10 news/official messages, 30–50 community observations, reply pairs, multiple districts, repeated electricity/water/connectivity themes, one incident, advertisements/currency spam, contradictory official/community updates and one high-risk rumor.
- The test stubs provider responses and never contacts Telegram, Telegraph or a model API.

- [ ] **Step 1: Create the fixture and expected compact Story Cards**

Include stable message IDs, channel/topic metadata, timestamps, source roles and reply parents. Ensure the fixture contains enough supported details for a developed article but no unsupported prices, technical causes or comparative statistics.

- [ ] **Step 2: Add the failing integration assertions**

Assert that the result has a specific headline and lead, at least several substantive paragraphs, related observations synthesized into narrative prose, visible community attribution, district detail supported by the fixture, and no raw Story Card labels or internal refs. Assert that ads, exchange rates, unsupported generator-sales claims, unsupported technical causation and the high-risk rumor as an established fact are absent.

Do not assert an exact word count. Check only that a sufficiently rich fixture does not produce the generic deterministic fallback title and that the article remains readable after a targeted repair of one unrelated paragraph.

- [ ] **Step 3: Run the quality regression and verify failure against the current pipeline**

```bash
uv run pytest -q tests/test_editorial_integration.py --no-cov
```

Expected before Tasks 1–4: FAIL because the current full analysis can exhaust the provider budget and the writer receives the full raw bundle.

- [ ] **Step 4: Stabilize the fixture-driven test and commit**

```bash
uv run pytest -q tests/test_editorial_integration.py tests/test_article_generator.py tests/test_article_pipeline.py --no-cov
git add tests/fixtures/editorial_longform_day.json tests/test_editorial_integration.py tests/conftest.py
git commit -m "test: protect long-form article quality"
```

### Task 6: Full verification and implementation handoff

**Files:**

- Modify only files required by failing verification.

- [ ] **Step 1: Run focused editorial tests**

```bash
uv run pytest -q \
  tests/test_ai_providers.py \
  tests/test_editorial_models.py \
  tests/test_editorial_input.py \
  tests/test_editorial_analysis.py \
  tests/test_editorial_writer.py \
  tests/test_editorial_audit.py \
  tests/test_editorial_fallback.py \
  tests/test_editorial_integration.py \
  tests/test_article_generator.py \
  tests/test_article_pipeline.py \
  tests/test_core.py --no-cov
```

- [ ] **Step 2: Run the complete suite**

```bash
uv run pytest -q --no-cov
```

Expected: all existing and new tests pass; no test contacts Telegram, Telegraph or a model API.

- [ ] **Step 3: Run formatting, lint, type and diff checks**

```bash
uv run black --check src tests
uv run flake8 src tests
uv run mypy src
git diff --check
```

- [ ] **Step 4: Run one configured dry-run smoke test**

Run:

```bash
uv run python main.py --article --dry-run
```

Verify that a token-budget failure logs a compact-analysis retry before any deterministic fallback, that a successful run logs writer/audit stages, and that the preview is a developed article rather than `Что происходило в городе за сутки`. Do not treat a network/provider outage as a test failure if the fallback is compact and the log explains the branch.

- [ ] **Step 5: Commit only verified implementation changes**

```bash
git status --short
git diff --check
```

Stage only implementation and test files from this plan. Do not stage local credentials, generated debug artifacts or unrelated user changes. Report the final test counts, compact-analysis behavior, selected source excerpt behavior and the observed dry-run branch.
