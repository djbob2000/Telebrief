# Reader-First Editorial Presentation Design

**Status:** Approved direction from 2026-08-30 reader comparison

## 1. Goal

Keep Event-First correctness, provenance, temporal resolution, geography, and bounded AI cost while changing the final reader experience so that:

- the **digest scans like a professional digest**: operational dashboard first, then short bullet items with a bold one-line summary and a compact explanatory body;
- the **article reads like an article**: a selective long-form narrative with a central line, chronology, contrast, human-scale details, and chapter-level composition rather than a list of Events.

This is a follow-up to the already-started Narrative Editorial Layer work. It does not replace or reopen ingestion, Gate V2, clustering, Edition Geography, temporal resolution, Article Claim Atoms, or Evidence Boundary.

## 2. Baseline

The current local implementation already contains:

- `src/publication/digest_narrative.py` with `DigestNarrativeWriter`, `plan_digest_narrative_blocks()`, `validate_digest_narrative()`, and `build_digest_support_text_index()`;
- Event-First narrative digest wiring in `src/publication/generation.py`;
- Event-First single-call article generation in `src/article_generator.py`;
- structured article supports, Claim Atoms, and deterministic validation;
- fail-closed rejection for invalid article output.

The follow-up must modify those paths incrementally rather than restore the legacy cascade.

## 3. Product Principle

> The digest is optimized for scanning. The article is optimized for reading. Both are optimized for evidence safety.

The same Event-First knowledge may therefore produce different presentation structures.

## 4. Digest UX

### 4.1 Target shape

```text
Digest: <edition> · <date>

City Situation dashboard

Rubric
• **Short scan headline**: 2-4 sentences of compact narrative synthesis.
• **Short scan headline**: 2-4 sentences of compact narrative synthesis.

Rubric
• **Short scan headline**: 2-4 sentences of compact narrative synthesis.

Statistics
```

The bold sentence fragment is a mini-summary. A reader who reads only bold text should still understand the main developments.

### 4.2 Story grouping

Deterministic planning still decides rubric/block membership. The LLM may **partition Stories inside one deterministic block into editorial items** because grouping is presentation, not selection.

For each block:

- every input Story ID must appear in exactly one editorial item;
- no Story may move to another block;
- no Story may disappear;
- no unknown Story may appear;
- independent stories should remain separate items;
- closely related observations may be synthesized into one item.

This allows three separate electricity Stories to become one readable digest item without letting the model choose publication coverage.

### 4.3 Digest draft contract

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
```

Rubric headings remain deterministic. The LLM does not author or rename them.

## 5. Article UX

### 5.1 Target shape

A successful Event-First article should look like:

```text
# Headline with one central promise

Lead with 2-3 themes, not an inventory.

## Narrative chapter

Paragraph: state -> chronology -> lived detail -> uncertainty/current status.

Paragraph: connected supported detail, contrast, resident adaptation.

## Narrative chapter

...
```

It must not look like:

```text
## Utilities
fact
fact

## Water
fact
fact
```

or a bullet list of all selected Stories.

### 5.2 Selective coverage

The article is not required to mention every selected Story. It should use the authorized material to choose a small number of central narrative lines. Omission from the article is not deletion from knowledge and does not affect the digest.

Default editorial targets:

- thin material: 2-3 chapters, roughly 300-700 words;
- standard material: 2-4 chapters, roughly 500-1100 words;
- rich material: 3-5 chapters, roughly 800-1400 words.

These are **soft targets**, not evidence validity rules.

## 6. Article Length Policy

Introduce a deterministic `ArticleLengthProfile` derived from the amount of current publishable evidence.

```python
@dataclass(frozen=True)
class ArticleLengthProfile:
    richness: Literal["thin", "standard", "rich"]
    target_min_words: int
    target_max_words: int
    target_min_sections: int
    target_max_sections: int
    hard_min_words: int
    hard_max_words: int
```

Initial policy:

```text
thin:
  <= 4 publishable story IDs OR <= 8 PUBLISH supports
  target 300-700 words, 2-3 sections

standard:
  <= 10 publishable story IDs OR <= 24 PUBLISH supports
  target 500-1100 words, 2-4 sections

rich:
  everything above
  target 800-1400 words, 3-5 sections

hard validation:
  minimum 180 words
  maximum = PublicationEditorialConfig.article_max_words
  maximum sections = PublicationEditorialConfig.article_max_sections
```

Falling below a soft target is diagnostic. It must not by itself trigger deterministic fallback.

## 7. Heading Policy

Current Claim Atom enforcement is too strict for chapter headings and can turn expressive but safe headings into validation failures.

New rule:

- title, lead, and paragraphs still require Claim Atoms;
- section headings require support IDs but **do not require Claim Atoms** when they contain no independently verifiable concrete claim;
- if a heading contains a number, date, named concrete entity, causal relation, mechanism, or another concrete claim already recognized by deterministic claim extraction, that claim must be supported;
- heading support IDs must be a subset of the supports cited by paragraphs in that section.

This allows thematic headings such as "Свет по цепочке" while preventing unsupported headings such as "Свет восстановят через три дня".

## 8. Narrative Glue

Allowed editorial glue organizes supported facts without adding a new factual relationship:

- "тем временем";
- "при этом";
- "к вечеру" when timestamps support it;
- "несколькими днями ранее" when timestamps support it;
- "одновременно";
- "ситуация различалась от района к району" when multiple supported locations establish that contrast.

Causal or interpretive bridges still require evidence:

- "поэтому";
- "вследствие этого";
- "это привело к";
- "стало причиной";
- broad psychological/social conclusions;
- symbolic or political interpretation not present in support.

Claim Atoms remain validation metadata. They do not define sentence boundaries.

## 9. Failure Semantics

### Digest

`single_call` mode:

- one generative attempt;
- validate block identity, exact Story partition, supports, concrete claims, and text hygiene;
- on any invalid result, discard the complete overlay and render the existing deterministic digest;
- never repair with a second LLM call.

### Article

```text
ARTICLE
  ArticleEditorialContext
        |
        v
  ONE writer LLM
        |
        v
  deterministic validator
      /       \
   PASS       FAIL
    |          |
    v          v
 publish     reject run
              |
              +-- no Publication
              +-- no delivery
              +-- no retry
```

Event-First article publication is fail-closed.
A writer draft that fails deterministic validation is rejected and produces no publication.
Writer/provider/response failure also produces no publication.
There is no public deterministic article fallback and no second generative attempt.
Digest deterministic fallback remains valid and unchanged.

## 10. Quality Gates

### Digest hard gates

- City Situation remains first when present;
- every deterministic block appears exactly once;
- every Story appears in exactly one narrative item within its block;
- no unknown Story or support IDs;
- unsupported concrete claims = 0;
- internal IDs in rendered text = 0;
- generative attempts <= 1;
- deterministic mode remains 0 generative calls.

### Digest reader gates

Human A/B review should prefer or tie `custom` for:

- scanability;
- clarity of bold mini-summaries;
- absence of giant paragraphs;
- useful synthesis of repeated observations.

### Article hard gates

- writer path succeeds; publication is binary (fail-closed);
- unsupported Claim Atoms = 0;
- unsupported concrete/causal/mechanism claims = 0;
- reporting-window violations = 0;
- internal IDs = 0;
- generative attempts <= 1.

### Article reader gates

Human A/B review should prefer or tie `custom` for:

- narrative flow;
- chapter quality;
- chronology;
- human-scale local detail;
- lack of database/list feeling;
- readable transitions;
- restraint from unsupported interpretation.

## 11. Non-Goals

- no change to `join_similarity`;
- no new LLM selector;
- no return to Analyzer -> Writer -> FactChecker -> Repair cascade;
- no second article generation call;
- no second digest generation call;
- no city-specific production prompt;
- no requirement that article cover every Story;
- no exact-string golden prose tests.
