# Reader-First Editorial Presentation Implementation Plan

> **Superseded decision:** Any task text that treats deterministic Event-First article fallback as a publishable/degraded product is superseded by `2026-08-30-article-fail-closed-publication.md`. Digest fallback remains valid.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change the already-implemented Event-First Narrative Editorial Layer so the digest is scan-first (`bold mini-summary + compact narrative`) and the article is a genuine evidence-bound long-read rather than an event inventory, without increasing the existing one-call budgets.

**Architecture:** Keep the current Event-First knowledge, Gate, geography, temporal resolution, Claim Atoms, and deterministic Evidence Boundary. Evolve the existing digest narrative draft from block paragraphs into validated editorial items that may group Stories only inside deterministic rubric blocks. For articles, relax presentation-only constraints that currently trigger unnecessary fallback: add an adaptive soft length profile, allow support-bound thematic headings without mandatory Claim Atoms, and strengthen the writer contract around selective narrative composition and safe connective prose.

**Tech Stack:** Python 3.12, dataclasses, pytest, existing AI provider abstraction, PostgreSQL publication integration tests, Event-First `ArticleEditorialContext`, `StructuredArticleDraft`, `ArticleValidator`, `DigestNarrativeWriter`, `PublicationDigestRenderer`.

**Spec:** `docs/superpowers/specs/2026-08-30-reader-first-editorial-presentation-design.md`

## Global Constraints

1. This is a **follow-up** to the already-started `2026-08-30-event-first-narrative-editorial-layer` work. Do not rewrite that historical plan or undo commits already produced from it.
2. Baseline assumes local code already contains `src/publication/digest_narrative.py`, Event-First digest narrative wiring, Event-First article single-call generation, Claim Atoms, and deterministic fallback.
3. Do not change ingestion, Gate V2, Edition Geography, temporal resolution, Story persistence, Story clustering, or `join_similarity`.
4. Digest deterministic mode remains **0 generative LLM calls**.
5. Digest `single_call` narrative mode remains **<= 1 generative LLM call total**. No repair call.
6. Event-First article remains **<= 1 generative LLM call total**. No AI fact-check, repair, or regeneration.
7. Claim Atoms remain validation metadata, not sentence templates.
8. The digest LLM may group Stories only inside an already-determined block. It may not add, drop, reorder across blocks, or move a Story to a different rubric.
9. Article coverage is selective: the writer is not required to mention every authorized Story.
10. Evidence Boundary, reporting-window rules, exact support provenance, commercial publication policy, and current-state temporal semantics remain authoritative.
11. A safer fallback is allowed to be dry. Fallback readability is not the target-quality benchmark.
12. No city-specific names, streets, districts, prices, or local examples in production prompt code.
13. No exact target article/digest prose in golden fixtures.
14. Preserve legacy/custom routing unless an existing Event-First test explicitly exercises the same shared function.
15. Every task follows RED -> GREEN -> focused regression -> commit.

---

## File Structure

### New production file

- `src/publication/article_length.py`
  - computes deterministic article richness and soft target length/section ranges;
  - contains no provider, DB, or prompt logic.

### Existing production files to modify

- `src/publication/digest_narrative.py`
  - change narrative output from paragraph-only blocks to scan-first editorial items;
  - validate exact Story partition inside deterministic blocks;
  - keep single-call writer and deterministic fallback semantics.

- `src/publication/narrative_contract.py`
  - update digest contract for `headline + body` items;
  - update article contract for selective long-read composition and safe narrative glue.

- `src/publication/renderers.py`
  - render narrative digest items as `• **headline**: body`;
  - keep City Situation first and deterministic rendering unchanged when no valid narrative draft exists.

- `src/publication/article_validator.py`
  - accept an optional `ArticleLengthProfile` for Event-First validation;
  - make soft target length/section ranges diagnostic instead of fallback-triggering;
  - make Claim Atoms optional for support-bound non-concrete section headings.

- `src/article_generator.py`
  - derive article length profile before the one writer call;
  - inject target range and reader-first article contract;
  - preserve one-call/fallback semantics and attempt recording.

- `src/publication/narrative_quality.py`
  - add diagnostic reader-experience metrics; never use them as factual publication gates.

- `scripts/benchmark_publication_quality.py`
  - separate `writer_success` from fallback;
  - compare digest scan-first structure and successful articles honestly.

### Existing tests / fixtures to modify

- `tests/fixtures/berdyansk_narrative_editorial_golden.json`
- `tests/publication/test_digest_narrative.py`
- `tests/publication/test_renderers.py`
- `tests/publication/test_article_validator.py`
- `tests/integration/test_article_evidence_bound_generation.py`
- `tests/integration/test_event_first_narrative_publication.py`
- `tests/publication/test_generation.py`
- `tests/publication/test_narrative_contract.py`

### New focused tests

- `tests/publication/test_article_length.py`
- `tests/publication/test_reader_first_quality.py`

No database migration is required.

---

# Phase A - Freeze the Reader Contract Before Changing Code

### Task 1: Extend the golden fixture with scan-first digest and successful-article expectations

**Files:**
- Modify: `tests/fixtures/berdyansk_narrative_editorial_golden.json`
- Modify: `tests/publication/test_digest_narrative.py`
- Modify: `tests/integration/test_article_evidence_bound_generation.py`

**Interfaces:**
- Consumes: existing golden evidence/support IDs from the Narrative Editorial Layer.
- Produces: fixture cases used by Tasks 2-9; no exact prose strings.

- [ ] **Step 1: Add a digest case where three related electricity Stories belong to one deterministic block**

Add fixture metadata equivalent to:

```json
{
  "reader_first_expectations": {
    "digest": {
      "electricity_story_ids": ["story:101", "story:102", "story:103"],
      "expected_block_id": "rubric:utilities:0",
      "allow_single_editorial_item_for_related_stories": true,
      "require_each_story_exactly_once": true,
      "require_bold_scan_headline": true
    },
    "article": {
      "claim_atoms_are_not_sentence_templates": true,
      "allow_thematic_heading_without_claim_atoms": true,
      "require_writer_success_for_quality_gate": true,
      "fallback_is_safety_only": true
    }
  }
}
```

Use real fixture IDs already present in the file instead of inventing production IDs; keep this metadata next to the existing cases it describes.

- [ ] **Step 2: Add failing digest tests for item-level grouping**

Add a test that constructs one existing `DigestNarrativeBlock` containing three Story IDs and asserts the parser/validator accepts one item covering all three:

```python
raw = {
    "blocks": [
        {
            "block_id": block.block_id,
            "items": [
                {
                    "headline": "Подтвержденных сроков восстановления света пока нет",
                    "body": (
                        "Сообщения об отключениях поступали в разные дни. "
                        "Жители называли несколько неофициальных сроков, "
                        "но подтвержденной даты в поддержках нет."
                    ),
                    "covered_story_ids": list(block.story_ids),
                    "cited_support_ids": list(block.support_ids),
                }
            ],
        }
    ]
}

draft = DigestNarrativeDraft.from_dict(raw)
result = validate_digest_narrative(draft, plan, support_index)
assert result.is_valid
assert draft.blocks[0].items[0].covered_story_ids == block.story_ids
```

Expected RED: current draft schema uses block paragraphs rather than `items`.

- [ ] **Step 3: Add a failing article validator test for a thematic heading**

Use an existing valid paragraph/support fixture and construct:

```python
section = ArticleSection(
    heading="Свет по цепочке",
    heading_support_ids=(support_id,),
    heading_claims=(),
    paragraphs=(supported_paragraph,),
)
```

Assert the only reason for failure is no longer `MISSING_CLAIM_ATOMS:H001` once Task 6 is implemented.

- [ ] **Step 4: Add a failing integration assertion that thin safe prose is not rejected solely for being under 800 words**

The test model response must be at least 180 words, remain within evidence, and be intentionally below the current `article_min_words=800`.

Assert the final attempt kind is `writer`, not `story_renderer_fallback`.

Expected RED: current Event-First validator rejects it with `WORD_COUNT_OUT_OF_BOUNDS`.

- [ ] **Step 5: Run RED tests**

```bash
pytest tests/publication/test_digest_narrative.py \
  tests/publication/test_article_validator.py \
  tests/integration/test_article_evidence_bound_generation.py \
  -v --no-cov
```

Expected: failures on the new item schema, heading Claim Atom rule, and thin-article length rule.

- [ ] **Step 6: Commit tests/fixture only**

```bash
git add tests/fixtures/berdyansk_narrative_editorial_golden.json \
  tests/publication/test_digest_narrative.py \
  tests/publication/test_article_validator.py \
  tests/integration/test_article_evidence_bound_generation.py
git commit -m "test(editorial): freeze reader-first digest and article behavior"
```

---

# Phase B - Make the Digest Scan First

### Task 2: Replace paragraph-only digest blocks with editorial items

**Files:**
- Modify: `src/publication/digest_narrative.py`
- Modify: `tests/publication/test_digest_narrative.py`

**Interfaces:**
- Consumes: existing deterministic `DigestNarrativeBlock` plan objects.
- Produces:

```python
@dataclass(frozen=True)
class DigestEditorialItemDraft:
    headline: str
    body: str
    covered_story_ids: tuple[str, ...]
    cited_support_ids: tuple[str, ...]


@dataclass(frozen=True)
class DigestNarrativeBlockDraft:
    block_id: str
    items: tuple[DigestEditorialItemDraft, ...]


@dataclass(frozen=True)
class DigestNarrativeDraft:
    blocks: tuple[DigestNarrativeBlockDraft, ...]
```

- [ ] **Step 1: Add parser tests for the new schema**

Cover:

```python
assert draft.blocks[0].items[0].headline
assert draft.blocks[0].items[0].body
assert draft.blocks[0].items[0].covered_story_ids == ("story:101", "story:102")
assert draft.blocks[0].items[0].cited_support_ids == ("support:1", "support:2")
```

Also assert empty headline, empty body, empty Story IDs, and empty support IDs are rejected during parsing or validation.

- [ ] **Step 2: Run parser tests and verify RED**

```bash
pytest tests/publication/test_digest_narrative.py -k "item or schema" -v --no-cov
```

- [ ] **Step 3: Implement immutable item models and `from_dict()` parsing**

Use this normalization pattern:

```python
@classmethod
def from_dict(cls, raw: Mapping[str, Any]) -> "DigestEditorialItemDraft":
    headline = str(raw.get("headline", "")).strip()
    body = str(raw.get("body", "")).strip()
    story_ids = tuple(dict.fromkeys(str(x).strip() for x in raw.get("covered_story_ids", []) if str(x).strip()))
    support_ids = tuple(dict.fromkeys(str(x).strip() for x in raw.get("cited_support_ids", []) if str(x).strip()))
    if not headline or not body or not story_ids or not support_ids:
        raise ValueError("digest editorial item requires headline, body, stories and supports")
    return cls(
        headline=headline,
        body=body,
        covered_story_ids=story_ids,
        cited_support_ids=support_ids,
    )
```

Do not keep a second paragraph-only output model.

- [ ] **Step 4: Run focused tests and verify GREEN**

```bash
pytest tests/publication/test_digest_narrative.py -k "item or schema" -v --no-cov
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/digest_narrative.py tests/publication/test_digest_narrative.py
git commit -m "refactor(digest): model narrative output as scan-first items"
```

---

### Task 3: Validate editorial grouping as an exact Story partition inside each block

**Files:**
- Modify: `src/publication/digest_narrative.py`
- Modify: `tests/publication/test_digest_narrative.py`

**Interfaces:**
- Consumes:

```python
def validate_digest_narrative(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
    support_index: Mapping[str, str],
) -> DigestNarrativeValidationResult: ...
```

- Produces validation issues with stable codes:
  - `BLOCK_SET_MISMATCH`
  - `STORY_PARTITION_MISMATCH`
  - `DUPLICATE_STORY_COVERAGE`
  - `UNKNOWN_STORY_ID`
  - `UNKNOWN_SUPPORT_ID`
  - `SUPPORT_OUTSIDE_BLOCK`
  - `UNSUPPORTED_CONCRETE_CLAIM`
  - `INTERNAL_ID_LEAK`
  - `HEADLINE_TOO_LONG`
  - `BODY_TOO_LONG`

- [ ] **Step 1: Add failing partition tests**

Cases:

```python
# valid: two related stories in one item + one independent story in another
expected = set(block.story_ids)
covered = {sid for item in block_draft.items for sid in item.covered_story_ids}
assert covered == expected
```

And invalid cases:

```text
story omitted
story duplicated in two items
story from another block inserted
unknown story inserted
support from another block cited
```

- [ ] **Step 2: Add failing evidence tests for headline and body separately**

The headline and body must both be checked against the item's cited support texts:

```python
headline_bad = "Свет восстановят через три дня"
body_good = "Жители обсуждают несколько неподтвержденных сроков."
```

With no three-day support, expect `UNSUPPORTED_CONCRETE_CLAIM`.

- [ ] **Step 3: Run RED**

```bash
pytest tests/publication/test_digest_narrative.py -k "partition or support or unsupported" -v --no-cov
```

- [ ] **Step 4: Implement exact partition validation**

Core logic must be equivalent to:

```python
planned_by_id = {b.block_id: b for b in plan.blocks}
for out_block in draft.blocks:
    planned = planned_by_id[out_block.block_id]
    expected_story_ids = tuple(planned.story_ids)
    flat_story_ids = tuple(
        sid for item in out_block.items for sid in item.covered_story_ids
    )
    if len(flat_story_ids) != len(set(flat_story_ids)):
        add_issue("DUPLICATE_STORY_COVERAGE", out_block.block_id)
    if set(flat_story_ids) != set(expected_story_ids):
        add_issue("STORY_PARTITION_MISMATCH", out_block.block_id)
```

Then verify every item support ID belongs to `planned.support_ids` and run existing conservative claim support checks on both `headline` and `body`.

- [ ] **Step 5: Add bounded presentation limits**

Use module constants:

```python
DIGEST_ITEM_HEADLINE_MAX_CHARS = 140
DIGEST_ITEM_BODY_MAX_CHARS = 900
DIGEST_ITEM_MAX_STORIES = 6
```

`DIGEST_ITEM_MAX_STORIES` is a presentation bound only. If a deterministic block has more Stories, the writer must create multiple items so every Story is still covered exactly once.

- [ ] **Step 6: Run GREEN**

```bash
pytest tests/publication/test_digest_narrative.py -v --no-cov
```

- [ ] **Step 7: Commit**

```bash
git add src/publication/digest_narrative.py tests/publication/test_digest_narrative.py
git commit -m "feat(digest): enforce evidence-bound story partitioning"
```

---

### Task 4: Rewrite the digest narrative contract for mini-summary plus compact body

**Files:**
- Modify: `src/publication/narrative_contract.py`
- Modify: `src/publication/digest_narrative.py`
- Modify: `tests/publication/test_narrative_contract.py`
- Modify: `tests/publication/test_digest_narrative.py`

**Interfaces:**
- Consumes: existing `build_digest_narrative_contract(output_language=...)` and `DigestNarrativeWriter.generate_narrative_draft(...)`.
- Produces: one JSON response using the Task 2 item schema.

- [ ] **Step 1: Add contract tests for scan-first behavior**

Assert the production contract contains requirements equivalent to:

```text
Each digest item has one short scan headline and one compact body.
The headline must stand on its own as the answer to "what happened?".
The body adds context, chronology, current status, attribution or practical detail.
Prefer 2-4 sentences in the body.
Do not output one giant paragraph for an entire rubric.
Related stories inside the same deterministic block may be grouped into one item.
Independent stories must remain separate items.
Every story must be covered exactly once.
Do not repeat the headline verbatim in the body.
```

- [ ] **Step 2: Run RED**

```bash
pytest tests/publication/test_narrative_contract.py tests/publication/test_digest_narrative.py -k "scan or headline or contract" -v --no-cov
```

- [ ] **Step 3: Update `build_digest_narrative_contract()`**

Keep evidence/scope rules unchanged. Replace paragraph-flow instructions with the scan-first item contract.

- [ ] **Step 4: Update writer JSON schema/examples**

The system prompt must require:

```json
{
  "blocks": [
    {
      "block_id": "rubric:utilities:0",
      "items": [
        {
          "headline": "...",
          "body": "...",
          "covered_story_ids": ["story:101", "story:102"],
          "cited_support_ids": ["support:1", "support:2"]
        }
      ]
    }
  ]
}
```

Do not let the model generate rubric names or block ordering.

- [ ] **Step 5: Run GREEN**

```bash
pytest tests/publication/test_narrative_contract.py tests/publication/test_digest_narrative.py -v --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add src/publication/narrative_contract.py src/publication/digest_narrative.py \
  tests/publication/test_narrative_contract.py tests/publication/test_digest_narrative.py
git commit -m "feat(digest): make narrative output scan-first"
```

---

### Task 5: Render narrative digest items with the old scan shell and new synthesis

**Files:**
- Modify: `src/publication/renderers.py`
- Modify: `tests/publication/test_renderers.py`
- Modify: `tests/integration/test_event_first_narrative_publication.py`

**Interfaces:**
- Consumes: validated `DigestNarrativeDraft` from Tasks 2-4.
- Produces rendered body:

```text
*Rubric title*
• **Headline**: Body
• **Headline**: Body
```

- [ ] **Step 1: Add failing renderer tests**

For a block with two items, assert:

```python
assert "• **Подтвержденных сроков восстановления света пока нет**:" in body
assert "• **На Горе банкоматы без связи, но карты в магазинах принимают**:" in body
```

Also assert:

```python
assert body.index("Городская обстановка") < body.index("Коммунальная обстановка")
assert "story:" not in body
assert "support:" not in body
```

- [ ] **Step 2: Run RED**

```bash
pytest tests/publication/test_renderers.py -k "narrative or city_situation" -v --no-cov
```

- [ ] **Step 3: Implement narrative item rendering**

Inside the existing narrative-draft branch, render deterministic rubric headings and model-authored items only:

```python
for item in block.items:
    lines.append(f"• **{item.headline.strip()}**: {item.body.strip()}")
```

Do not render canonical StoryCard bullets again for Stories already represented by the validated narrative draft.

- [ ] **Step 4: Preserve the deterministic path byte-for-byte at the behavioral level**

When `narrative_draft is None`, existing StoryCard rendering and zero-call behavior must remain unchanged.

- [ ] **Step 5: Run focused and integration tests**

```bash
pytest tests/publication/test_renderers.py \
  tests/integration/test_event_first_narrative_publication.py \
  -v --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add src/publication/renderers.py tests/publication/test_renderers.py \
  tests/integration/test_event_first_narrative_publication.py
git commit -m "feat(digest): render scan-first narrative items"
```

---

# Phase C - Stop Safe Articles from Falling Back for Editorial Reasons

### Task 6: Add deterministic article length profiles and make target length soft

**Files:**
- Create: `src/publication/article_length.py`
- Create: `tests/publication/test_article_length.py`
- Modify: `src/publication/article_validator.py`
- Modify: `tests/publication/test_article_validator.py`

**Interfaces:**

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ArticleLengthProfile:
    richness: Literal["thin", "standard", "rich"]
    target_min_words: int
    target_max_words: int
    target_min_sections: int
    target_max_sections: int
    hard_min_words: int
    hard_max_words: int


def derive_article_length_profile(
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig,
) -> ArticleLengthProfile: ...
```

Event-First validator signature becomes:

```python
def validate_article_draft(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig | None = None,
    *,
    length_profile: ArticleLengthProfile | None = None,
) -> ArticleValidationResult: ...
```

When `length_profile is None`, preserve current validation semantics for compatibility.

- [ ] **Step 1: Write policy tests**

Test three exact buckets:

```python
assert derive_article_length_profile(thin_ctx, cfg).richness == "thin"
assert derive_article_length_profile(thin_ctx, cfg).target_min_words == 300
assert derive_article_length_profile(thin_ctx, cfg).target_max_words == 700

assert derive_article_length_profile(standard_ctx, cfg).richness == "standard"
assert derive_article_length_profile(standard_ctx, cfg).target_min_words == 500
assert derive_article_length_profile(standard_ctx, cfg).target_max_words == 1100

assert derive_article_length_profile(rich_ctx, cfg).richness == "rich"
assert derive_article_length_profile(rich_ctx, cfg).target_min_words == 800
assert derive_article_length_profile(rich_ctx, cfg).target_max_words == min(1400, cfg.article_max_words)
```

Bucket rules:

```python
publish_story_count = len({s.story_id for s in context.supports if s.publication_use == "PUBLISH"})
publish_support_count = sum(1 for s in context.supports if s.publication_use == "PUBLISH")

thin = publish_story_count <= 4 or publish_support_count <= 8
standard = not thin and (publish_story_count <= 10 or publish_support_count <= 24)
rich = otherwise
```

- [ ] **Step 2: Add validator tests for soft target misses**

For an Event-First thin profile:

```python
assert draft.word_count >= 180
assert draft.word_count < profile.target_min_words
result = validate_article_draft(draft, ctx, cfg, length_profile=profile)
assert "WORD_COUNT_OUT_OF_BOUNDS:DRAFT" not in result.violations
```

For a 150-word draft, expect the hard floor violation. For a draft over `cfg.article_max_words`, expect the hard ceiling violation.

- [ ] **Step 3: Run RED**

```bash
pytest tests/publication/test_article_length.py tests/publication/test_article_validator.py -k "length or word_count" -v --no-cov
```

- [ ] **Step 4: Implement the pure length policy**

`article_length.py` must import only article context/config types and standard library. It must not call providers or DB.

- [ ] **Step 5: Update validator hard bounds only when a profile is provided**

Equivalent logic:

```python
if length_profile is None:
    min_words = config.article_min_words
    max_words = config.article_max_words
    min_sections = config.article_min_sections
    max_sections = config.article_max_sections
else:
    min_words = length_profile.hard_min_words
    max_words = length_profile.hard_max_words
    min_sections = 1
    max_sections = config.article_max_sections
```

Do not reject a valid Event-First article merely for missing the profile's soft target.

- [ ] **Step 6: Run GREEN**

```bash
pytest tests/publication/test_article_length.py tests/publication/test_article_validator.py -v --no-cov
```

- [ ] **Step 7: Commit**

```bash
git add src/publication/article_length.py src/publication/article_validator.py \
  tests/publication/test_article_length.py tests/publication/test_article_validator.py
git commit -m "feat(article): use adaptive soft length targets"
```

---

### Task 7: Let thematic section headings be editorial without weakening factual validation

**Files:**
- Modify: `src/publication/article_validator.py`
- Modify: `tests/publication/test_article_validator.py`

**Interfaces:**
- Consumes: existing `ArticleSection.heading`, `heading_support_ids`, `heading_claims`, paragraph support IDs.
- Produces: heading-specific validation policy.

- [ ] **Step 1: Add failing tests for safe and unsafe headings**

Safe thematic heading:

```python
ArticleSection(
    heading="Свет по цепочке",
    heading_support_ids=(support_id,),
    heading_claims=(),
    paragraphs=(supported_paragraph,),
)
```

Expected: no `MISSING_CLAIM_ATOMS:H001`.

Unsafe concrete heading:

```text
"Свет восстановят через три дня"
```

with no such duration support must still fail through concrete-claim validation.

Also add:

```python
heading_support_ids = (foreign_support_id,)
paragraph_support_ids = (section_support_id,)
```

Expected: `HEADING_SUPPORT_OUTSIDE_SECTION:H001`.

- [ ] **Step 2: Run RED**

```bash
pytest tests/publication/test_article_validator.py -k "heading" -v --no-cov
```

- [ ] **Step 3: Change Claim Atom requirement by unit type**

Replace unconditional requirement with:

```python
requires_claim_atoms = unit_type in {"title", "lead", "paragraph"}
if requires_claim_atoms and not claim_atoms:
    add_missing_claim_atom_issue(...)
```

If `heading_claims` are present, validate them normally. Regardless of Claim Atoms, continue running `find_unsupported_claims()` on heading text.

- [ ] **Step 4: Validate heading supports against section paragraph supports**

Before flattening units, compute per-section paragraph support union:

```python
section_support_ids = {
    sid for para in sec.paragraphs for sid in para.cited_support_ids
}
if not set(sec.heading_support_ids).issubset(section_support_ids):
    issues.append(
        ArticleValidationIssue(
            code="HEADING_SUPPORT_OUTSIDE_SECTION",
            unit_id=h_id,
            message="Heading must be supported by evidence used inside its section",
            support_ids=sec.heading_support_ids,
        )
    )
```

- [ ] **Step 5: Run GREEN plus full article validator tests**

```bash
pytest tests/publication/test_article_validator.py -v --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add src/publication/article_validator.py tests/publication/test_article_validator.py
git commit -m "feat(article): allow support-bound thematic headings"
```

---

# Phase D - Make the One Article Call Write a Long-Read, Not a Digest

### Task 8: Update the article narrative contract for selective composition and safe narrative glue

**Files:**
- Modify: `src/publication/narrative_contract.py`
- Modify: `tests/publication/test_narrative_contract.py`
- Modify: `src/article_generator.py`
- Modify: `tests/test_article_generator.py`

**Interfaces:**
- Consumes:

```python
build_article_narrative_contract(
    *,
    output_language: str,
    length_profile: ArticleLengthProfile,
) -> str
```

- Produces a single writer prompt; no extra provider calls.

- [ ] **Step 1: Add failing contract assertions**

Assert the article contract explicitly states:

```text
- Do not cover every authorized Story merely because it exists.
- Choose a small number of central narrative lines for the day.
- Use the target section/word range as a soft editorial target; never pad.
- Build paragraphs around state, chronology, resident consequence/adaptation, uncertainty, or current status.
- Claim Atoms are validation metadata, not sentence boundaries.
- Safe connective language may organize supported facts without creating causality.
- Avoid category-shaped chapters such as generic "Utilities", "Water", "Other" when a more specific supported narrative heading exists.
- Do not use bullet lists in the main article body.
- Do not write an inventory paragraph that simply enumerates every Story.
- No unsupported interpretation, symbolism, psychological conclusions, or broad social claims.
```

- [ ] **Step 2: Add a writer prompt test using a thin profile**

Assert the generated system prompt contains `300-700` and `2-3` as editorial targets and still contains the strict support/Claim Atom JSON contract.

- [ ] **Step 3: Run RED**

```bash
pytest tests/publication/test_narrative_contract.py tests/test_article_generator.py -k "narrative or profile or prompt" -v --no-cov
```

- [ ] **Step 4: Update `build_article_narrative_contract()`**

Keep evidence prohibitions separate from editorial guidance. Include neutral connective examples only as prose guidance, not as a string allowlist gate.

- [ ] **Step 5: Derive `ArticleLengthProfile` before the provider call**

In `generate_from_event_article_context()`:

```python
editorial_cfg = getattr(
    self.config.settings,
    "publication_editorial",
    PublicationEditorialConfig(),
)
length_profile = derive_article_length_profile(article_ctx, editorial_cfg)
contract = build_article_narrative_contract(
    output_language=self.output_language,
    length_profile=length_profile,
)
```

Then use the same one `chat_completion()` call as today.

- [ ] **Step 6: Pass the same profile into deterministic validation**

```python
val_res = validate_article_draft(
    draft,
    article_ctx,
    editorial_cfg,
    length_profile=length_profile,
)
```

No retry after failure.

- [ ] **Step 7: Run GREEN**

```bash
pytest tests/publication/test_narrative_contract.py \
  tests/test_article_generator.py \
  tests/integration/test_article_evidence_bound_generation.py \
  -v --no-cov
```

- [ ] **Step 8: Commit**

```bash
git add src/publication/narrative_contract.py src/article_generator.py \
  tests/publication/test_narrative_contract.py tests/test_article_generator.py \
  tests/integration/test_article_evidence_bound_generation.py
git commit -m "feat(article): make single-call writer compose selective long-read"
```

---

### Task 9: Record successful-writer vs degraded-fallback outcomes explicitly

**Files:**
- Modify: `src/article_generator.py`
- Modify: `tests/test_article_generator.py`
- Modify: `tests/publication/test_generation.py`

**Interfaces:**
- Consumes: existing generation attempt observer.
- Produces writer metadata:

```python
{
    "article_result": "writer_success",
    "length_profile": "thin|standard|rich",
    "target_word_range": [300, 700],
    "target_section_range": [2, 3],
    "validation": {...}
}
```

Fallback metadata:

```python
{
    "article_result": "validation_fallback" | "writer_error_fallback",
    "reason": "...",
    "violations": [...]
}
```

- [ ] **Step 1: Add failing attempt-metadata tests**

Successful writer must record `article_result=writer_success`.

Validation fallback must record `article_result=validation_fallback` on the successful fallback attempt and keep the failed writer attempt's violation metadata.

Provider/parse exception fallback must record `article_result=writer_error_fallback`.

- [ ] **Step 2: Run RED**

```bash
pytest tests/test_article_generator.py tests/publication/test_generation.py -k "article_result or fallback" -v --no-cov
```

- [ ] **Step 3: Add metadata without changing routing**

Do not add a new attempt kind or DB migration. Use existing attempt metadata JSON.

- [ ] **Step 4: Run GREEN**

```bash
pytest tests/test_article_generator.py tests/publication/test_generation.py -v --no-cov
```

- [ ] **Step 5: Commit**

```bash
git add src/article_generator.py tests/test_article_generator.py tests/publication/test_generation.py
git commit -m "feat(article): distinguish writer success from safety fallback"
```

---

# Phase E - Add Reader Diagnostics Without Turning Style into a Hard Factual Gate

### Task 10: Add scanability and long-read diagnostics

**Files:**
- Modify: `src/publication/narrative_quality.py`
- Create: `tests/publication/test_reader_first_quality.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class DigestReaderMetrics:
    item_count: int
    average_headline_chars: float
    average_body_sentences: float
    giant_paragraph_count: int
    repeated_attribution_openings: int


@dataclass(frozen=True)
class ArticleReaderMetrics:
    section_count: int
    paragraph_count: int
    bullet_line_count: int
    generic_heading_count: int
    repeated_attribution_openings: int
    average_paragraph_sentences: float


def measure_digest_reader_metrics(text: str) -> DigestReaderMetrics: ...

def measure_article_reader_metrics(text: str) -> ArticleReaderMetrics: ...
```

These functions are diagnostics only.

- [ ] **Step 1: Write metric tests from representative old/new shapes**

Examples:

```python
assert measure_digest_reader_metrics(scan_first).item_count == 3
assert measure_digest_reader_metrics(one_giant_paragraph).giant_paragraph_count == 1
assert measure_article_reader_metrics(bullet_fallback).bullet_line_count > 0
assert measure_article_reader_metrics(longread).bullet_line_count == 0
```

Generic article heading set for diagnostics only:

```python
{"события и факты", "другое", "прочее", "городские события"}
```

Do not fail publication merely because a generic heading appears.

- [ ] **Step 2: Run RED**

```bash
pytest tests/publication/test_reader_first_quality.py -v --no-cov
```

- [ ] **Step 3: Implement pure text diagnostics**

No DB, provider, config, or publication routing imports.

- [ ] **Step 4: Run GREEN**

```bash
pytest tests/publication/test_reader_first_quality.py -v --no-cov
```

- [ ] **Step 5: Commit**

```bash
git add src/publication/narrative_quality.py tests/publication/test_reader_first_quality.py
git commit -m "feat(editorial): add reader-first quality diagnostics"
```

---

# Phase F - Honest A/B on the Same Real Corpus

### Task 11: Make the quality benchmark distinguish target prose from fallback

**Files:**
- Modify: `scripts/benchmark_publication_quality.py`
- Modify: `tests/integration/test_event_first_narrative_publication.py`

**Interfaces:**
- Consumes current publication attempts and rendered output.
- Produces benchmark JSON with separate product outcomes.

Required structure:

```json
{
  "digest": {
    "mode": "single_call",
    "generative_attempts": 1,
    "reader_metrics": {},
    "hard_gate_pass": true
  },
  "article": {
    "article_result": "writer_success",
    "generative_attempts": 1,
    "fallback_used": false,
    "reader_metrics": {},
    "hard_gate_pass": true
  }
}
```

- [ ] **Step 1: Add a failing benchmark test for fallback classification**

If the winning attempt is deterministic fallback, benchmark output must be:

```json
{
  "article_result": "validation_fallback",
  "fallback_used": true,
  "target_editorial_quality_pass": false
}
```

Do not score fallback prose against `custom` as a successful long-read.

- [ ] **Step 2: Add digest structural metrics**

Report:

```text
item_count
story_coverage_count
story_duplicate_count
average_headline_chars
average_body_sentences
giant_paragraph_count
```

- [ ] **Step 3: Add article structural metrics**

Report:

```text
writer_success/fallback
section_count
paragraph_count
bullet_line_count
generic_heading_count
word_count
length_profile
target range
```

- [ ] **Step 4: Keep AI budgets separated**

Report at least:

```text
processing/enrichment generative calls
publication digest generative calls
publication article generative calls
rubric embedding batches
```

Do not count embedding classifier attempts as generative writer calls merely because the attempt kind is `writer`; inspect `metadata.subkind`.

- [ ] **Step 5: Run benchmark tests/integration**

```bash
pytest tests/integration/test_event_first_narrative_publication.py -v --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add scripts/benchmark_publication_quality.py \
  tests/integration/test_event_first_narrative_publication.py
git commit -m "feat(benchmark): compare successful reader-first outputs honestly"
```

---

### Task 12: Run the real 24-hour three-way digest and two-way successful-article A/B

**Files:**
- Modify only if benchmark output reveals a test harness bug: `scripts/benchmark_publication_quality.py`
- Save output under the existing debug/benchmark output convention; do not add generated benchmark artifacts to source control unless the repo already tracks that directory.

**Interfaces:**
- Digest comparison:
  1. legacy/custom reference;
  2. Event-First deterministic 0-call;
  3. Event-First scan-first `single_call`.
- Article comparison:
  1. legacy/custom successful article;
  2. Event-First `writer_success` article.
  - If Event-First uses fallback, article editorial A/B is **not valid** and the run fails the target-quality gate.

- [ ] **Step 1: Run the same 24-hour corpus**

```bash
PYTHONPATH=. DATABASE_URL="$DATABASE_URL" .venv/bin/python \
  scripts/benchmark_publication_quality.py --hours 24 --edition berdyansk
```

- [ ] **Step 2: Verify digest hard gates**

All must be true:

```text
unsupported concrete claims = 0
unknown support IDs = 0
unknown Story IDs = 0
Story omissions = 0
Story duplicate coverage = 0
cross-block Story moves = 0
internal ID leaks = 0
generative attempts <= 1
City Situation appears before editorial items when present
```

- [ ] **Step 3: Verify digest reader gates manually**

For the same corpus, answer yes/no and save with benchmark notes:

```text
Can the main stories be understood from bold headlines alone?
Does each item add context instead of repeating its headline?
Are related outage/utility updates synthesized rather than repeated?
Are independent stories still visibly separate?
Are there any rubric-sized giant paragraphs?
```

Required rollout result: scan-first Event-First must tie or beat the custom reference on at least 4/5 questions, with no hard-gate regression.

- [ ] **Step 4: Verify article hard gates**

All must be true:

```text
article_result = writer_success
fallback_used = false
unsupported Claim Atoms = 0
unsupported concrete claims = 0
unsupported causal/mechanism claims = 0
reporting-window violations = 0
internal ID leaks = 0
generative attempts <= 1
```

- [ ] **Step 5: Verify article reader gates manually**

Rate each 1-5 against the custom reference:

```text
central narrative line
chapter/headline quality
chronology
human-scale local detail
transitions
absence of list/database feel
restraint from unsupported interpretation
```

Required rollout result:

```text
Event-First score >= custom on at least 5 of 7 dimensions
no dimension below 3/5
all hard gates pass
```

This is a human editorial acceptance gate, not an automated "literary score".

- [ ] **Step 6: If Event-First falls back, diagnose but do not weaken Evidence Boundary**

Allowed fixes after a failed A/B are limited to:

```text
prompt/schema clarity
soft length target
heading policy
parser robustness
support packet completeness
```

Do not lower claim-support coverage thresholds, disable reporting-window checks, or add a second LLM repair call merely to make the prose pass.

- [ ] **Step 7: Commit only harness corrections, if any**

```bash
git add scripts/benchmark_publication_quality.py
git commit -m "fix(benchmark): correct reader-first corpus evaluation"
```

Skip this commit if no harness code changed.

---

# Phase G - Full Verification and Rollout

### Task 13: Run full publication verification and lock budgets

**Files:**
- Modify only if a regression exposed by these commands is directly caused by Tasks 1-12.

**Interfaces:**
- Produces final evidence for rollout; no new runtime interface.

- [ ] **Step 1: Run focused reader-first suites**

```bash
pytest tests/publication/test_digest_narrative.py \
  tests/publication/test_renderers.py \
  tests/publication/test_article_length.py \
  tests/publication/test_article_validator.py \
  tests/publication/test_narrative_contract.py \
  tests/publication/test_reader_first_quality.py \
  tests/test_article_generator.py \
  -v --no-cov
```

Expected: all PASS.

- [ ] **Step 2: Run Event-First integration suites**

```bash
pytest tests/integration/test_article_evidence_bound_generation.py \
  tests/integration/test_event_first_narrative_publication.py \
  tests/publication/test_generation.py \
  -v --no-cov
```

Expected: all PASS.

- [ ] **Step 3: Run all publication tests**

```bash
pytest tests/publication -v --no-cov
```

Expected: all PASS.

- [ ] **Step 4: Run static checks**

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
```

Expected: zero errors.

- [ ] **Step 5: Run full regression suite**

```bash
pytest --no-cov
```

Expected: all PASS.

- [ ] **Step 6: Verify AI budget invariants from attempt history**

For one successful Event-First digest run:

```text
publication generative writer/editorial calls <= 1
```

For one successful Event-First article run:

```text
publication generative writer calls = 1
AI fact-check calls = 0
AI repair calls = 0
AI regeneration calls = 0
```

Rubric embedding batches must be reported separately from generative calls.

- [ ] **Step 7: Verify fallback remains deterministic**

Force one invalid digest narrative response and one invalid article response in tests. Assert neither path invokes a second provider call and both produce a successful deterministic publication attempt.

- [ ] **Step 8: Final commit**

```bash
git add src tests scripts
git commit -m "feat(editorial): ship reader-first Event-First presentation"
```

Only create this commit when there are uncommitted final changes; do not create an empty commit.

---

## Acceptance Criteria

### Digest structure

1. City Situation remains first when present.
2. Narrative digest uses deterministic rubric/block order.
3. Each narrative item has one non-empty mini-summary headline.
4. Each narrative item has one compact body.
5. Renderer outputs `• **headline**: body`.
6. A Story may be grouped with related Stories only inside its deterministic block.
7. Every planned Story appears exactly once across items in its block.
8. No Story moves across rubric blocks.
9. No Story is added or omitted by the writer.
10. Related Stories can be synthesized into one item.
11. Independent Stories can remain separate items.
12. No rubric-sized giant paragraph is required.
13. Headline does not simply duplicate the body opening.
14. Deterministic digest mode remains unchanged when no narrative draft exists.

### Digest safety and budget

15. Unknown support IDs fail validation.
16. Supports outside the assigned block fail validation.
17. Unsupported concrete claims in headline or body fail validation.
18. Internal Story/support IDs never render.
19. Invalid digest narrative output falls back without a second LLM call.
20. Deterministic mode uses 0 generative calls.
21. `single_call` mode uses <= 1 generative call.

### Article presentation

22. Event-First article remains structured as title + lead + narrative sections.
23. Article writer is explicitly told not to cover every Story.
24. Claim Atoms remain metadata and may be combined into natural paragraphs.
25. Rich material targets 3-5 chapters rather than one section per category/Event.
26. Thin material may produce a shorter article without padding.
27. Main article body contains no bullet inventory in the target writer path.
28. Narrative transitions may organize chronology/contrast without implying unsupported causality.
29. Unsupported interpretation remains prohibited.
30. Micro-locations and resident actions may appear naturally when supported.
31. Repeated attribution phrases are reduced by narrative synthesis, not by changing epistemic status.

### Article heading and length policy

32. Title still requires support and Claim Atoms.
33. Lead still requires support and Claim Atoms.
34. Paragraphs still require support and Claim Atoms.
35. A non-concrete thematic section heading may omit Claim Atoms.
36. Section heading still requires support IDs.
37. Heading support IDs must be used by paragraphs in the same section.
38. Concrete unsupported claims in a heading still fail validation.
39. Event-First target word ranges are adaptive to evidence richness.
40. Missing a soft target minimum does not trigger fallback by itself.
41. Articles below the hard 180-word floor still fail.
42. Articles above the configured hard maximum still fail.
43. Existing validator behavior remains unchanged for callers that do not pass `length_profile`.

### Article safety and failure semantics

44. Event-First article uses <= 1 generative call.
45. No AI fact-check call is added.
46. No AI repair call is added.
47. No AI regeneration call is added.
48. Evidence Boundary remains authoritative.
49. Reporting-window rules remain authoritative.
50. Invalid writer output falls back deterministically.
51. Fallback is marked as degraded safety output, not target-quality writer success.
52. Benchmark never compares fallback prose against a successful custom article as an equal target-quality output.

### Reader-quality evaluation

53. Digest scanability is evaluated separately from narrative cohesion.
54. Bold digest mini-summaries are readable without the body.
55. Digest bodies add context rather than restating the headline.
56. Article reader metrics are diagnostic, not factual publication gates.
57. Real-corpus article A/B requires `article_result=writer_success`.
58. Event-First article must tie/beat custom on at least 5 of 7 human editorial dimensions before rollout.
59. No human-quality gate may override a factual hard-gate failure.

### Scope

60. No ingestion changes.
61. No Gate V2 changes.
62. No Edition Geography changes.
63. No temporal resolver changes.
64. No Story clustering changes.
65. No `join_similarity` changes.
66. No city-specific production prompt examples.
67. No exact-string golden article/digest prose tests.
68. No database migration.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-30-reader-first-editorial-presentation.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - use `superpowers:subagent-driven-development`; one fresh subagent per task with review between tasks.
2. **Inline Execution** - use `superpowers:executing-plans`; execute sequentially with checkpoints.

Recommended order: Tasks 1-5 first (digest UX), Tasks 6-9 next (article fallback/readability), Tasks 10-12 for honest A/B, then Task 13 for full verification.
