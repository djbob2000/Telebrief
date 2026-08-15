# Editorial Story Cards Article Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the atomic claim-gated daily article with a Story Card editorial pipeline that writes readable long-form local journalism, performs non-blocking light fact checking with targeted repairs, and always degrades to a thematic digest when substantive material remains.

**Architecture:** `MessageCollector` produces messages with reply/topic metadata; `SourceRoleResolver` applies configured editorial source roles during input preparation. `EditorialInputBuilder` cleans and references the full 24-hour bundle, `EditorialAnalyzer` produces Story Cards (full bundle first, context batching only after explicit size rejection), `EditorialWriter` writes a free-form article, and `LightFactChecker` returns PASS/WARN/FIX with at most two batched repair passes. `DeterministicStoryCardBuilder` and `StoryCardRenderer` provide the final non-raw fallback. `ArticleGenerator` orchestrates these units while the morning digest remains independent.

**Tech Stack:** Python 3.14, dataclasses, asyncio, YAML configuration, existing `AIProvider`/provider cascade, Telethon `Message`, pytest/pytest-asyncio, existing Telegraph and Telegram delivery code.

## Global Constraints

- `source_type` is an editorial prior, never a trust score or permission to state a fact.
- Effective role precedence is `topic.source_type -> channel.source_type -> mixed`.
- Missing source roles remain backwards-compatible and resolve to `mixed`.
- Full-bundle editorial analysis is the primary path; batching is allowed only after all available provider slots explicitly reject the request for context/request size.
- A normal timeout does not trigger batching; provider cascade/retry handling runs first.
- Story Cards are reporting notes with element-level raw-message refs, not an atomic claim registry.
- Writer may synthesize supplied material but may not add a new independently verifiable fact.
- `WARN` never blocks publication; `FIX` is repaired locally, with at most two repair calls, one call per complete issue set.
- After unresolved local FIXes, remove or conservatively rewrite only those fragments; use one systemic regeneration or thematic fallback when the central story is unsafe.
- Fact-checker failure does not suppress a structurally valid writer article.
- No path may publish the latest raw Telegram messages as an article fallback.
- A substantive local result must use the main or degraded editorial path; only empty/unsafe/technically impossible input may skip publication.
- Preserve the existing morning digest, Telegraph delivery, Instant View delivery, provider keys and provider cascade behavior.
- Preserve existing uncommitted user changes; do not reset, stage unrelated files, or commit generated secrets.
- Before every `git add`, inspect the target file for pre-existing user changes; stage only intended files/hunks and never silently include unrelated work.

---

### Task 1: Add source roles and message context metadata

**Files:**

- Modify: `src/config_loader.py` (`ChannelConfig`, `ForumTopicConfig`, parsing/validation)
- Modify: `src/collector.py` (`Message`, Telegram conversion)
- Modify: `config.yaml` and `config.yaml.example` (role declarations/documentation)
- Test: `tests/test_config_loader.py`
- Test: `tests/test_collector.py`

**Interfaces:**

- `ChannelConfig.source_type: str = "mixed"`
- `ForumTopicConfig.source_type: str | None = None`
- `Message.message_id: int | None = None`
- `Message.reply_to_id: int | None = None`
- `Message.topic_id: int | None = None`
- Add `SOURCE_TYPES = ("news", "community", "official", "classifieds", "mixed")`.
- Add `effective_source_type(channel: ChannelConfig, topic: ForumTopicConfig | None = None) -> str`.
- Add `SourceRoleResolver(channels: list[ChannelConfig])` with `resolve(channel_name: str, topic_id: int | None = None) -> str`.

- [ ] **Step 1: Write failing configuration tests**

Add tests that load a channel with no role and assert `mixed`, reject `source_type: unknown`, parse channel role, parse topic override, and assert topic > channel > mixed precedence. Include the current configuration expectations: Berdyansk `community`, both selected forum topics `news`, numeric groups `mixed`.

```python
def test_source_type_precedence_and_mixed_default(tmp_path, mock_env_vars):
    config_file = write_config_with_channel_and_topic(
        tmp_path,
        channel_source_type="community",
        topic_source_type="news",
    )
    config = load_config(str(config_file))
    assert config.channels[0].source_type == "community"
    assert config.channels[0].topics[0].source_type == "news"
    assert effective_source_type(config.channels[0], config.channels[0].topics[0]) == "news"

def test_missing_source_type_resolves_to_mixed(...):
    assert load_config(...).channels[0].source_type == "mixed"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
uv run --with pyyaml --with pytest pytest -q tests/test_config_loader.py -k source_type --no-cov
```

Expected: FAIL because the dataclass fields and parser do not exist yet.

- [ ] **Step 3: Implement role parsing and compatible defaults**

Parse `source_type` only when present, normalize to lowercase, validate against `SOURCE_TYPES`, and leave absent channel fields as `mixed`. Keep topic role optional so it can fall through to the channel role. Do not infer roles from channel names.

- [ ] **Step 4: Write failing collector metadata tests**

Extend the Telegram-message fixture with `id`, `reply_to.reply_to_msg_id`, and a forum topic identifier. Assert that `_to_project_message` copies the IDs and that messages created directly in existing tests still work without arguments.

- [ ] **Step 5: Implement backward-compatible `Message` metadata**

Add optional dataclass fields with `None` defaults. Update `_to_project_message` to accept an optional `topic_id`, copy `message.id`, copy `reply_to.reply_to_msg_id`, and use the selected forum topic ID when collecting a topic. Preserve existing links, timestamps and media behavior.

- [ ] **Step 6: Update current YAML role declarations and run focused tests**

Set the exact current roles in `config.yaml`; mirror the documented fields in `config.yaml.example`. Run:

```bash
uv run --with pyyaml --with pytest pytest -q tests/test_config_loader.py tests/test_collector.py --no-cov
```

Expected: PASS, with old tests unchanged except for new assertions.

The same task must test `SourceRoleResolver.resolve()` for whole-channel keys, logical `channel — topic` keys and topic IDs. The resolver is the only component that converts configured channel/topic roles into the effective role used by editorial input preparation; roles are never inferred from a raw channel name.

- [ ] **Step 7: Commit the isolated metadata/config change**

```bash
git add src/config_loader.py src/collector.py config.yaml.example tests/test_config_loader.py tests/test_collector.py
git commit -m "feat: add editorial source roles and message context metadata"
```

### Task 2: Create typed editorial models and conservative source preparation

**Files:**

- Create: `src/editorial_models.py`
- Create: `src/editorial_input.py`
- Test: `tests/test_editorial_input.py`
- Test: `tests/test_editorial_models.py`

**Interfaces:**

- `SourceRecord(ref: str, message: Message, source_type: str, parent_ref: str | None, context_text: str)`
- `PreparedBundle(records: dict[str, SourceRecord], prompt_text: str, total_messages: int, candidate_count: int)`
- `StoryElement(text: str, source_refs: list[str], status: str = "attributed", attribution: str = "", areas: list[str] = field(default_factory=list))`
- `Uncertainty(text: str, basis: str, related_source_refs: list[str])`
- `StoryCard(id: str, topic: str, importance: str, summary: str, story_kind: str = "", timeframe: str = "", current_status: str = "", next_known_step: str = "", editorial_angle: dict | None = None, hard_facts: list[StoryElement] = ..., community_observations: list[StoryElement] = ..., useful_details: list[StoryElement] = ..., uncertainties: list[Uncertainty] = ...)`
- `EditorialAnalysis(cards: list[StoryCard], labels: dict[str, dict], excluded_refs: list[str] = ...)`
- `EditorialInputBuilder(role_resolver: SourceRoleResolver)`
- `EditorialInputBuilder.build(messages_by_channel: dict[str, list[Message]]) -> PreparedBundle`

Card validation must enforce `importance in {high, medium, low}` and item status in `{established, attributed, disputed}`. `story_kind` is free-form. Every card element ref must resolve to a `PreparedBundle.records` entry.

- [ ] **Step 1: Write model validation tests**

Test valid cards, invalid importance/status, unresolved refs, free-form `story_kind`, uncertainty `basis`, and stable serialization preserving element-level refs.

- [ ] **Step 2: Run model tests and verify failure**

```bash
uv run --with pytest pytest -q tests/test_editorial_models.py --no-cov
```

Expected: FAIL because the model module does not exist.

- [ ] **Step 3: Implement dataclasses and strict shape validation**

Keep models provider-agnostic and JSON-serializable. Do not add claim IDs or require sentence-level mappings. Implement `from_dict`/`to_dict` methods so analysis, audit, debug artifacts and tests use one representation.

- [ ] **Step 4: Write input-preparation tests**

Use fixtures containing news, official, community, mixed, ads, currency, emoji-only, short replies and repeated messages. Assert that:

```python
bundle.records["S000001"].message.message_id == 123
bundle.records["S000002"].parent_ref == "S000001"
assert "У кого сейчас есть вода?" in bundle.prompt_text
assert "На Колонии тоже нет" in bundle.prompt_text
```

Also assert that explicit spam is removed, useful short context survives, each ref is unique within the run, and no broad neighbor block is duplicated for every message.

- [ ] **Step 5: Implement conservative input preparation**

Assign refs before AI processing from deterministic input order and retain the composite channel/topic/message identity in each record. Build a channel/topic-aware lookup for reply parents. Include parent text for short/reply-dependent messages and only a bounded nearby context. Use a conservative filter for empty service records, emoji-only items, explicit ads, currency and commercial spam; do not filter solely by character count.

For the article path, do not apply the old fixed character bundle cap by default. Build the complete prepared bundle (`max_chars=None`) and let the selected provider/model accept or explicitly reject its context. If an optional article input budget is configured, it must be token-based and reserve space for the analysis prompt and response; it must not silently discard the oldest messages by character count.

- [ ] **Step 6: Run focused input tests and commit**

```bash
uv run --with pytest pytest -q tests/test_editorial_models.py tests/test_editorial_input.py --no-cov
git add src/editorial_models.py src/editorial_input.py tests/test_editorial_models.py tests/test_editorial_input.py
git commit -m "feat: add editorial source bundle and story card models"
```

### Task 3: Implement editorial analysis, context fallback and provider error classification

**Files:**

- Create: `src/editorial_analysis.py`
- Modify: `src/ai_providers.py` (`ProviderCascadeError` safe failure metadata)
- Test: `tests/test_editorial_analysis.py`
- Test: `tests/test_ai_providers.py`

**Interfaces:**

- `class EditorialAnalysisError(RuntimeError)`
- `class ContextSizeRejectedError(EditorialAnalysisError)`
- `EditorialAnalyzer(provider: AIProvider, model: str, logger: logging.Logger)`
- `async EditorialAnalyzer.analyze(bundle: PreparedBundle) -> EditorialAnalysis`
- `async EditorialAnalyzer.analyze_batched(bundle: PreparedBundle) -> EditorialAnalysis`
- `EditorialAnalyzer.build_prompt(bundle: PreparedBundle) -> tuple[str, str]`

Extend `ProviderCascadeError` with safe structured failure kinds (`context_size`, `quota`, `timeout`, `auth`, `server`, `other`) and a `context_only` property. Do not expose exception text or API keys. `context_only` is true only when every attempted provider slot rejected the request as context/request-size too large.

- [ ] **Step 1: Write provider classification tests**

Mock quota, timeout and context-length exceptions across cascade slots. Assert that labels and safe kinds are retained, secret-bearing exception text is absent, and `context_only` is true only when all failures are context-size failures.

- [ ] **Step 2: Implement safe provider failure metadata**

Classify exception status/code/message internally, store only labels, exception class and safe kind, and preserve existing failover order. Keep the existing Google-key/OpenRouter cascade behavior intact.

- [ ] **Step 3: Write editorial-analysis tests first**

Mock a provider response containing two Story Cards with hard facts, community observations, editorial angle, uncertainty and labels. Assert parsing, element refs, status normalization and rejection of unsupported card shapes. Add a test that the prompt contains all prepared records, source roles and the instruction that source text is untrusted data.

- [ ] **Step 4: Implement the full-bundle analysis call**

Create a dedicated analysis prompt that asks for Story Cards, not claims. Preserve all refs and distinguish source role from epistemic status. Do not impose a fixed 4–12 limit; use it as prompt guidance only. Parse and validate `EditorialAnalysis`.

- [ ] **Step 5: Implement explicit context-size batching**

Call `analyze` once with the full bundle through the provider cascade. Catch only `ProviderCascadeError.context_only` and convert it to `ContextSizeRejectedError`. Split by source/channel/time while preserving refs, analyze batches, validate each batch’s Story Cards against that batch’s refs, then perform one merge call that validates the merged cards against the original full bundle. The merge must group related aspects without inventing causal links. Do not batch after a normal timeout, quota failure or one failed provider slot.

- [ ] **Step 6: Test and commit the analysis stage**

```bash
uv run --with pytest --with pytest-asyncio pytest -q tests/test_ai_providers.py tests/test_editorial_analysis.py --no-cov
git add src/ai_providers.py src/editorial_analysis.py tests/test_ai_providers.py tests/test_editorial_analysis.py
git commit -m "feat: add editorial story analysis and context fallback"
```

### Task 4: Implement the free-form Story Card writer

**Files:**

- Create: `src/editorial_writer.py`
- Test: `tests/test_editorial_writer.py`

**Interfaces:**

- `ArticleDraft(headline: str, lead: str, paragraphs: list[str], sections: list[ArticleSection])`
- `ArticleSection(heading: str, paragraphs: list[str])`
- `AuditUnitLocator(unit_id: str, path: tuple[str, ...], text: str)`
- `EditorialWriter(provider: AIProvider, model: str, skill_instructions: str, logger: logging.Logger)`
- `async EditorialWriter.write(analysis: EditorialAnalysis, bundle: PreparedBundle) -> ArticleDraft`
- `ArticleDraft.to_markdown() -> str`
- `ArticleDraft.audit_units() -> dict[str, AuditUnitLocator]`
- `ArticleDraft.apply_replacements(replacements: dict[str, str]) -> ArticleDraft`

The writer response is strict JSON, never Markdown. The required shape is:

```json
{
  "headline": "...",
  "lead": "...",
  "paragraphs": ["...", "..."],
  "sections": [
    {"heading": "...", "paragraphs": ["...", "..."]}
  ]
}
```

The local `ArticleDraft` parser validates this JSON, assigns audit units (`TITLE`, `LEAD`, `P001`, `H001`, `P002`, …), and creates Markdown only in `to_markdown()`. A quiet day may use top-level `paragraphs` with `sections: []`; a developed article may use both. An empty `heading` is never used to fake a section.

- [ ] **Step 1: Write writer prompt tests**

Assert the prompt explicitly says Story Cards are reporting notes, permits natural synthesis and transitions, preserves community attribution, forbids new independently verifiable facts, and does not request claim IDs. Assert source excerpts and refs are present.

- [ ] **Step 2: Write draft parsing/preflight tests**

Test a normal article with lead and H2 sections, a thin article with top-level paragraphs and no sections, malformed JSON, missing headline and deterministic audit-unit locators. Assert that `TITLE`, `LEAD`, all top-level/body paragraphs and every H2 have unique structural paths, including two equal-text paragraphs. Ensure `to_markdown()` produces one headline and valid paragraphs.

- [ ] **Step 3: Implement writer and draft model**

Use the loaded `SKILL.md`, Story Cards and only relevant original source excerpts. Ask for the strict JSON shape above; never ask the writer to emit Markdown. Let the writer choose section names from real material. Do not force 900–1800 words or 4–7 sections. Preserve contradictions and modality. Implement structural locators so a repair can target `TITLE`, `LEAD`, any `H###` or an exact section/paragraph path without searching by text.

- [ ] **Step 4: Run writer tests and commit**

```bash
uv run --with pytest --with pytest-asyncio pytest -q tests/test_editorial_writer.py --no-cov
git add src/editorial_writer.py tests/test_editorial_writer.py
git commit -m "feat: add free-form Story Card article writer"
```

### Task 5: Implement light fact check, targeted repair and deterministic preflight

**Files:**

- Create: `src/editorial_audit.py`
- Test: `tests/test_editorial_audit.py`

**Interfaces:**

- `AuditIssue(unit_id: str, code: str, original_excerpt: str, reason: str, suggested_direction: str, source_refs: list[str], severity: str = "fix")`
- `FactCheckResult(status: str, systemic_problem: bool, issues: list[AuditIssue])`
- `LightFactChecker(provider: AIProvider, model: str, logger: logging.Logger)`
- `async LightFactChecker.check(draft: ArticleDraft, analysis: EditorialAnalysis, bundle: PreparedBundle, audit_units: dict[str, AuditUnitLocator] | None = None) -> FactCheckResult`
- `async LightFactChecker.repair(draft: ArticleDraft, result: FactCheckResult, analysis: EditorialAnalysis, bundle: PreparedBundle) -> ArticleDraft`
- `deterministic_preflight(markdown: str) -> None`

- [ ] **Step 1: Write status and issue tests**

Assert deterministic status normalization: any FIX wins over WARN, WARN wins over PASS, and PASS with issues is normalized to WARN/FIX according to issue severity. Test `unit_id` lookup for `TITLE`, `LEAD`, every `H###` and body units, plus missing/duplicate IDs.

- [ ] **Step 2: Write audit policy tests**

Use a fixture containing community synthesis, unsupported sales, unsupported technical mechanism, unsupported cause, and a high-risk claim in the headline or lead. Assert synthesis becomes PASS/WARN while concrete unsupported claims become FIX. Assert source refs are inspection references, not assumed proof.

- [ ] **Step 3: Implement audit prompt and parser**

Compare the whole draft with cards and raw source records. Check new numbers, prices, dates, names, official actions, causes, mechanisms, damage, sales, medical/legal/military claims, casualties and precise scale. Permit ordinary contextual synthesis. Assign stable audit units before the audit: `TITLE`, `LEAD`, `P001`, `H001`, `P002`, and so on. Every H2 receives a locator; neutral headings simply receive no issue, while factual headings can receive `FIX`.

- [ ] **Step 4: Implement one-call batched repair**

Send all current FIX issues in one repair request. Return a replacement map keyed by audit unit ID. Include surrounding paragraphs only as context. Apply replacements through the structural locator (`TITLE`, `LEAD`, `H###` or exact section/paragraph path); preserve unaffected units byte-for-byte where practical.

- [ ] **Step 5: Implement second-pass and unresolved-FIX policy**

Recheck repaired fragments only. Run a second repair call only for remaining/new FIX issues. After two passes, do not call a third AI repair: remove only the unsupported sentence or the whole affected unit when safe separation is impossible; always remove an unsafe high-risk fragment. For a systemic issue, regenerate once from the same Story Cards, run preflight and one new full light fact check. If the central systemic problem is gone but isolated local FIXes remain, remove or conservatively neutralize those units deterministically without starting another repair cycle. If the central unsafe problem remains, use the thematic fallback.

- [ ] **Step 6: Implement preflight and audit-failure behavior**

Check non-empty article, headline, valid Markdown, no raw JSON, and no internal `TITLE`/`LEAD`/`P###`/`H###`/`S######` markers. If the AI fact checker fails, log the failure and publish a preflight-passing writer draft.

- [ ] **Step 7: Test and commit**

```bash
uv run --with pytest --with pytest-asyncio pytest -q tests/test_editorial_audit.py --no-cov
git add src/editorial_audit.py tests/test_editorial_audit.py
git commit -m "feat: add non-blocking fact check and targeted repair"
```

### Task 6: Implement deterministic thematic fallback

**Files:**

- Create: `src/editorial_fallback.py`
- Test: `tests/test_editorial_fallback.py`

**Interfaces:**

- `DeterministicStoryCardBuilder.build(bundle: PreparedBundle, priority_hints: list[str] | None = None) -> EditorialAnalysis`
- `StoryCardRenderer.render(analysis: EditorialAnalysis) -> ArticleDraft`
- `NoSubstantiveMaterialError(RuntimeError)`

- [ ] **Step 1: Write fallback tests**

Cover electricity/water/connectivity/incidents/transport/social/urban-life categories, districts, repeated observations, explicit ads/currency/emoji spam, conflicting official/community reports, and separate “explosion” plus “power outage” observations. Assert no causal bridge is created and no raw latest-message text is emitted one item per paragraph.

- [ ] **Step 2: Implement conservative cleaning and classification**

Use explicit category vocabularies, normalized text, district extraction, reply/topic context and time proximity. Keep uncertain messages as separate elements inside a broad topic rather than forcing a merge. Rank by significance, specificity, source mix, practical impact, repetition and novelty; never by frequency alone.

- [ ] **Step 3: Implement card construction**

Build the same `StoryCard` type used by AI analysis: put news/official material in `hard_facts`, resident material in `community_observations`, practical items in `useful_details`, and evidence boundaries in `uncertainties`; retain element-level `source_refs`. Preserve contradictions side by side. Attribute community elements. Remove empty topics. Produce normally 3–8 blocks, with no artificial quota.

- [ ] **Step 4: Implement thematic renderer**

Render a headline, short lead and H2 blocks containing grouped sentences, not a raw Telegram dump. Use only card elements and source refs. Raise `NoSubstantiveMaterialError` when cleaning leaves no substantive local content.

- [ ] **Step 5: Run fallback tests and commit**

```bash
uv run --with pytest pytest -q tests/test_editorial_fallback.py --no-cov
git add src/editorial_fallback.py tests/test_editorial_fallback.py
git commit -m "feat: add deterministic thematic article fallback"
```

### Task 7: Replace the ArticleGenerator orchestration and remove raw fallback

**Files:**

- Modify: `src/article_generator.py`
- Modify: `src/core.py` (`generate_and_publish_article`, remove raw latest-message fallback)
- Modify: `src/config_loader.py` (`ArticleConfig.save_debug_artifacts`)
- Modify: `config.yaml.example` (debug artifact option/documentation)
- Local-only: `config.yaml` (enable the option for this installation if desired; do not stage or commit this ignored user configuration)
- Conditional: `.gitignore` (add only the debug-artifact directory if it is not already ignored)
- Test: `tests/test_article_generator.py`
- Test: `tests/test_core.py`

**Interfaces:**

- `ArticleGenerator.generate_article(messages_by_channel: dict[str, list[Message]]) -> tuple[str, str, str]`
- Internal orchestration order:
  `prepare -> editorial analysis -> (context batches only when explicitly required) -> writer -> preflight -> audit -> repair -> publish`
- On analysis failure: `DeterministicStoryCardBuilder -> StoryCardRenderer`.
- On writer failure after successful analysis: `StoryCardRenderer` over AI Story Cards.
- On unresolved central unsafe issue: one systemic regeneration, preflight and one new full audit; then deterministic local cleanup or thematic fallback.

`ArticleGenerator` constructs `SourceRoleResolver(config.channels)` once and passes it to `EditorialInputBuilder`; the public `generate_article(messages_by_channel)` signature remains unchanged.

- [ ] **Step 1: Replace obsolete claim-registry tests with orchestration tests**

Remove tests that require verbatim claim quotes, claim IDs on every draft unit, or fail-closed audit rejection. Keep tests for empty input, article parsing, provider retry and source metadata. Add tests for:

```python
assert "## Электроснабжение" in body
assert "последние сообщения" not in body.lower()
assert "продажи генераторов" not in body.lower()
```

- [ ] **Step 2: Implement ArticleGenerator orchestration**

Load the skill once, instantiate the focused services, pass the same prepared refs through every stage, and keep all AI errors recoverable. Log counts and stage transitions without dumping hundreds of per-message validation lines.

- [ ] **Step 3: Remove raw latest-message fallback from core**

Delete or replace `_build_fallback_article`. Core should publish the tuple returned by `ArticleGenerator`, return `False` only for `NoSubstantiveMaterialError` or an unsafe impossible result, and never synthesize an article from the five newest messages.

- [ ] **Step 4: Add debug artifact persistence**

Add `article.save_debug_artifacts: bool = false`. In dry-run, allow an explicit debug flag to enable artifacts. Write prepared input, Story Cards, writer draft, fact-check JSON, repair result and final article with a run timestamp; do not include secrets. Verify the artifact directory is git-ignored; if it is not, add only that directory to `.gitignore`. Never upload or publish artifacts automatically: prepared input contains real Telegram text, sender metadata and links. Artifact failures log warnings and do not block publication. Add parser tests for the new option and a preflight regression asserting `TITLE`, `LEAD`, `P###`, `H###` and `S######` markers never reach output.

- [ ] **Step 5: Run article/core tests and commit**

```bash
uv run --with pyyaml --with pytest --with pytest-asyncio pytest -q tests/test_article_generator.py tests/test_core.py --no-cov
git add src/article_generator.py src/core.py src/config_loader.py config.yaml.example tests/test_article_generator.py tests/test_core.py
git commit -m "feat: orchestrate Story Card article generation"
```

### Task 8: Rewrite the news-style skill and add the realistic quality regression

**Files:**

- Modify: `.agents/skills/news-style/SKILL.md`
- Modify: `.agents/skills/news-style/agents/openai.yaml` only if trigger/default prompt needs alignment
- Test: `tests/test_article_generator.py` or new `tests/test_editorial_integration.py`
- Test fixture: `tests/fixtures/editorial_day_messages.json`

**Interfaces:**

- `ArticleGenerator` loads the revised skill once and passes `skill_instructions` to `EditorialWriter`; `EditorialWriter` composes the writer prompt.
- The skill must describe the four layers: product, allowed synthesis, concrete-fact boundary, and high-risk caution.

- [ ] **Step 1: Replace the compliance-heavy article contract**

Before editing `.agents/skills/news-style/SKILL.md`, follow the installed `skill-creator` workflow/guidance. Keep source boundary, attribution, high-risk caution, privacy and no-invention rules. Add explicit permission to synthesize multiple community messages into attributed observations, use editorial angles and natural transitions, and write a readable long-form article. Remove claim-ID publication requirements, universal four-to-six paragraph forcing and unconditional fail-closed language. Preserve the target local-news voice.

- [ ] **Step 2: Build the realistic integration fixture**

Create a deterministic fixture with 5–10 news/official items, 30–50 community messages, reply pairs, multiple districts, ads/currency spam, contradictions, and one high-risk rumor. Include the generator’s expected provider responses so the test does not require a network call.

- [ ] **Step 3: Add quality assertions**

Assert that the generated result has a coherent headline/lead, groups related messages, uses attributed community synthesis, includes useful district detail, omits ads, does not become a Story Card list, does not invent sales/technical causes, and preserves cautious high-risk wording. Assert that a normal community paragraph remains unchanged when an unrelated paragraph is repaired.

- [ ] **Step 4: Run the quality test and commit**

```bash
uv run --with pyyaml --with pytest --with pytest-asyncio pytest -q tests/test_editorial_integration.py --no-cov
git add .agents/skills/news-style/SKILL.md .agents/skills/news-style/agents/openai.yaml tests/fixtures/editorial_day_messages.json tests/test_editorial_integration.py
git commit -m "test: add editorial article quality regression"
```

### Task 9: Full verification and handoff

**Files:**

- Modify only files required by failing verification.

- [ ] **Step 1: Run focused editorial tests**

```bash
uv run --with pyyaml --with pytest --with pytest-asyncio pytest -q \
  tests/test_editorial_models.py \
  tests/test_editorial_input.py \
  tests/test_editorial_analysis.py \
  tests/test_editorial_writer.py \
  tests/test_editorial_audit.py \
  tests/test_editorial_fallback.py \
  tests/test_editorial_integration.py \
  tests/test_article_generator.py \
  tests/test_core.py --no-cov
```

- [ ] **Step 2: Run the complete suite**

```bash
uv run --with pyyaml --with pytest --with pytest-asyncio pytest -q --no-cov
```

Expected: all existing and new tests pass; no test should contact Telegram, Telegraph or a model API.

- [ ] **Step 3: Run formatting, lint and type checks**

```bash
uv run black --check src tests
uv run flake8 src tests
uv run mypy src
git diff --check
```

Fix only implementation-related findings; do not reformat unrelated pre-existing files.

- [ ] **Step 4: Perform a dry-run smoke test with debug artifacts enabled**

Run `uv run python main.py --article --dry-run` with configured credentials and debug artifacts enabled. Verify logs show source preparation, Story Cards, writer, audit/repair or fallback, and that the output directory contains no API keys and no raw latest-message dump.

- [ ] **Step 5: Commit the verified implementation and report behavior**

```bash
git status --short
git diff --check
```

Stage only files changed while fixing verification findings; if verification made no changes, create no additional commit. Never stage the ignored local `config.yaml` or unrelated pre-existing work. Report the final test counts, fallback behavior, source-role configuration and any intentionally skipped external smoke tests.
