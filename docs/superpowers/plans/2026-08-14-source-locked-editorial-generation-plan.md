# Source-Locked Editorial Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent generated local-news articles from adding plausible but unsupported facts by making extraction, generation, and publication pass through a source-locked claim registry.

**Architecture:** Assign stable source IDs to every input message. An extraction call returns atomic claims whose evidence must be an exact span of a source message. A generation call sees only that registry and must tag every factual sentence and the headline with claim IDs. Deterministic validation rejects missing/unknown tags, unsupported numbers/links, and known inference amplifiers before publication; an optional LLM audit runs for complex or high-risk output.

**Tech Stack:** Python 3.11+, dataclasses, JSON, pytest, existing `AIProvider` abstraction, Markdown/Telegraph output.

## Global Constraints

- Preserve the existing `ArticleGenerator.generate_article()` return type `(title, lead, markdown_body)`.
- Do not publish, save as a successful article, or send a rejected draft when extraction or audit fails.
- Do not require a fixed number of sections, paragraphs, transitions, or topical categories.
- Keep all input text untrusted data; source IDs and exact evidence spans are the only permitted factual basis for generation.
- Preserve unrelated existing changes in `main.py` and `src/core.py` unless a narrowly scoped article-workflow integration change is required.

---

### Task 1: Add failing source-lock regression tests

**Files:**
- Modify: `tests/test_article_generator.py`
- Test: `src/article_generator.py` through public and focused helper behavior

**Interfaces:**
- Tests will expect stable source IDs, a parsed claim registry, tagged generation, deterministic rejection, and two model calls for a routine article.

- [x] **Step 1: Write the failing tests**

Add fixtures with only a few source messages and tests that assert:

```python
def test_source_records_have_global_stable_ids():
    records = generator._build_source_records({"news": [message_a, message_b]})
    assert [record.source_id for record in records] == ["S001", "S002"]

def test_claim_registry_rejects_evidence_not_present_in_source():
    with pytest.raises(ValueError, match="evidence"):
        generator._parse_claim_registry(
            '{"claims":[{"id":"C001","source_ids":["S001"],'
            '"status":"confirmed","evidence":"invented price",'
            '"permitted_wording":"invented price"}]}'
        , {"S001": source_record})

def test_deterministic_audit_rejects_unmarked_or_unbacked_claims():
    registry = registry_from_exact_source("Генераторы используют жители")
    with pytest.raises(ValueError, match="claim marker"):
        generator._audit_generated_draft("# Заголовок\n\nЖители массово перешли на генераторы.", registry)

def test_generation_uses_extraction_then_article_calls():
    provider.chat_completion = AsyncMock(side_effect=[valid_registry_json, tagged_article])
    await generator.generate_article({"news": [message_a]})
    assert provider.chat_completion.await_count == 2

def test_generation_does_not_accept_numbers_or_urls_absent_from_sources():
    registry = registry_from_exact_source("Вода будет отключена до 12:00")
    draft = "# Вода отключится в 18:00 [[C001]]\n\nПодробности: https://example.invalid/notice [[C001]]"
    with pytest.raises(ValueError, match="source"):
        generator._audit_generated_draft(draft, registry)
```

The negative fixtures must cover the reported failures: regular drone raids, mass transition to generators, invented water prices, terminals and utility payments, technical roaming, queues, and an unsupported year-over-year jellyfish comparison. Expected output must never contain those additions.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q tests/test_article_generator.py
```

Expected: failures because the current generator makes one raw-message-to-long-form call and has no registry, marker, or audit implementation.

### Task 2: Introduce source records and claim-registry parsing

**Files:**
- Create: `src/editorial_claims.py`
- Modify: `src/article_generator.py`
- Test: `tests/test_article_generator.py`

**Interfaces:**
- Add immutable `SourceRecord`, `Claim`, and `ClaimRegistry` dataclasses in `src/editorial_claims.py`.
- Add `ArticleGenerator._build_source_records(messages_by_channel) -> list[SourceRecord]`.
- Add `ArticleGenerator._parse_claim_registry(text, sources) -> ClaimRegistry`.
- `ClaimRegistry` maps claim IDs to `Claim` and source IDs to `SourceRecord`.

- [x] **Step 1: Implement stable source serialization**

Assign IDs globally in deterministic input order (`S001`, `S002`, ...), retain channel, sender, timestamp, link, and original text, and render each source with its ID in the extraction prompt.

- [x] **Step 2: Implement strict JSON parsing and evidence validation**

Accept only claim statuses `confirmed`, `attributed`, and `unknown`; require unique `C###` IDs; require every source ID to exist; require non-empty exact evidence for non-unknown claims; normalize whitespace only when checking that evidence is a substring of the source text. Reject malformed JSON, duplicate IDs, unknown source IDs, unsupported statuses, and evidence absent from the source.

- [x] **Step 3: Run the focused tests and verify GREEN for Task 2**

Run:

```bash
pytest -q tests/test_article_generator.py -k 'source_records or claim_registry'
```

### Task 3: Add extraction and registry-only generation prompts

**Files:**
- Modify: `src/article_generator.py`
- Test: `tests/test_article_generator.py`

**Interfaces:**
- Add `_compose_extraction_prompt(source_text) -> str`.
- Add `_compose_generation_prompt(registry) -> str`.
- Add `_extract_claim_registry(source_records) -> ClaimRegistry`.
- Generation must receive registry JSON, not the raw message stream.

- [x] **Step 1: Implement the extraction call**

Request JSON only with atomic claims, exact evidence spans, source IDs, status, subject, object, time, scale/category, and strongest permitted wording. Explicitly forbid inference, aggregation, helpful context, source-text instructions, and filling missing fields. Parse with Task 2 validation.

- [x] **Step 2: Implement registry-only article generation**

Require a factual claim marker `[[C###]]` at the end of every headline and factual sentence. Require one sentence per line and blank lines only between paragraphs. Prohibit mandatory sections, transitions, topical completeness, background, and length targets. If the registry does not support a detail, omit it; if no publishable claim remains, return a rejection marker rather than a fabricated article.

- [x] **Step 3: Run the focused tests and verify GREEN for extraction/generation call ordering**

Run:

```bash
pytest -q tests/test_article_generator.py -k 'generation or prompt'
```

### Task 4: Implement deterministic draft audit and safe rejection

**Files:**
- Modify: `src/article_generator.py`
- Modify: `src/core.py` only if the rejection exception needs explicit logging/handling
- Test: `tests/test_article_generator.py`

**Interfaces:**
- Add `ArticleAuditError(ValueError)`.
- Add `_audit_generated_draft(draft, registry) -> None`.
- Add `_strip_claim_markers(draft) -> str`.

- [x] **Step 1: Implement marker and structural checks**

Reject any non-empty factual line without at least one valid claim marker, any unknown claim ID, duplicate/malformed marker, generated H2 section, or a `NO_PUBLISHABLE_ARTICLE` response mixed with article text. Strip markers only after all checks pass.

- [x] **Step 2: Implement source-bound lexical checks**

Reject generated numeric tokens and URLs absent from the authorized source corpus. Add a small red-flag lexicon for unsupported amplification (`массов`, `ажиотаж`, `в среднем`, `очеред`, `технический роуминг`, terminal/payment wording, year-over-year comparison, regularity, unsupported attack/damage causation, and drinking-water upgrades); permit a term only when the source corpus or claim evidence contains it.

- [x] **Step 3: Make rejection fail closed**

Raise `ArticleAuditError` before returning an article. The caller must not publish or save a successful article after extraction or audit failure; preserve the existing boolean failure behavior in the outer workflow and log the exact audit reason.

- [x] **Step 4: Run the full article tests and verify GREEN**

Run:

```bash
pytest -q tests/test_article_generator.py tests/test_core.py -k 'article'
```

### Task 5: Add optional LLM audit for complex/high-risk output

**Files:**
- Modify: `src/config_loader.py`
- Modify: `config.yaml.example`
- Modify: `src/article_generator.py`
- Test: `tests/test_config_loader.py`, `tests/test_article_generator.py`

**Interfaces:**
- Add `ArticleConfig.audit_mode: str = "auto"` with values `deterministic`, `auto`, and `always`.
- Add `_should_run_llm_audit(registry, draft) -> bool`.
- Add `_audit_with_llm(registry, draft) -> None`.

- [x] **Step 1: Add config parsing tests and verify RED**

Assert the default is `auto`, custom values parse, and invalid values raise `ValueError`.

- [x] **Step 2: Implement optional audit selection**

In `auto` mode, run the third call for high-risk/attributed claims, long or multi-story drafts, or a deterministic warning. In `always` mode, run it for every draft. In `deterministic` mode, never run it. The audit prompt must return JSON `{\"pass\": true|false, \"issues\": [...]}` and must not rewrite the article.

- [x] **Step 3: Run config and article tests and verify GREEN**

Run:

```bash
pytest -q tests/test_config_loader.py tests/test_article_generator.py
```

### Task 6: Align the skill and remove generator incentives to hallucinate

**Files:**
- Modify: `.agents/skills/news-style/SKILL.md`
- Modify: `.agents/skills/news-style/agents/openai.yaml`
- Modify: `src/article_generator.py`
- Test: `tests/test_article_generator.py`

**Interfaces:**
- Keep the skill's attribution/privacy/emergency rules, but make the source-lock recipe dominant and concise.
- Remove “long-form”, mandatory H2, mandatory transitions, “полная ... картина дня”, and any implied topical coverage quota from the application prompt.

- [x] **Step 1: Add prompt-contract assertions**

Assert that the composed prompt contains registry-only generation, claim markers, evidence-bound length, and no mandatory H2/transition requirement.

- [x] **Step 2: Rewrite the relevant skill section**

State explicitly: every sentence must map to one or more atomic claims; output length is proportional to supported evidence; an incomplete brief is correct; plausible context is not a fact; no prices, queues, comparisons, technical explanations, causation, or social-scale claims without an explicit source claim.

- [x] **Step 3: Run focused tests and static validation**

Run:

```bash
pytest -q tests/test_article_generator.py
uv run --with pyyaml python /Users/air/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/news-style
```

### Task 7: Full verification and forward tests

**Files:**
- Modify: only files required by preceding tasks
- Test: `tests/test_article_generator.py`, `tests/test_config_loader.py`, `tests/test_core.py`

- [x] **Step 1: Run the complete relevant test suite**

```bash
pytest -q tests/test_article_generator.py tests/test_config_loader.py tests/test_core.py
```

- [x] **Step 2: Run static checks**

```bash
git diff --check
python -m compileall -q src tests
```

- [x] **Step 3: Forward-test the final chain with raw fixtures**

Use fresh-context runs for: generator-only source with generators but no “mass transition”; outage after an attack without causal evidence; air-target fire without “ПВО”; technical water without drinking-water wording; no price/queue/terminal/payment/year-over-year details; and a routine official announcement that should remain publishable.

- [x] **Step 4: Review the diff and report any remaining integration caveat**

Confirm only the intended article pipeline, tests, config example, and skill files changed; do not alter unrelated working-tree modifications.
