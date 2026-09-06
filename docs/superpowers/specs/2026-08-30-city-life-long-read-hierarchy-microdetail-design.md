# City-Life Long-Read Hierarchy & Microdetail Preservation Design

**Status:** Approved product direction from 2026-08-30 article comparison

## 1. Goal

Evolve the Event-First article product from a selective 3–5-theme narrative into a broad, evidence-bound city-life long read that can cover many meaningful developments from the reporting window while preserving concrete microdetails and still maintaining editorial hierarchy.

The target product is not a minimal newspaper analysis and not a message inventory. It is a readable daily article about how the city lived: major developments receive depth, secondary developments enrich those lines, and smaller useful city-life items remain visible in compact form.

## 2. Product Principle

> Do not publish less information merely to make the article look selective. Organize more useful information better.

A long read should preserve breadth when the day is rich. `PUBLISH` means the information is authorized for publication; it does not imply equal prominence. Prominence controls depth, placement, and compression, not whether a legitimate story may exist in the article.

The article must therefore optimize four things at once:

1. **Breadth** — meaningful city-life information should remain visible.
2. **Hierarchy** — major developments dominate the article; minor items do not receive equal weight.
3. **Microdetail** — concrete resident actions, locations, numbers, service names, timings, and short exact quotes should survive synthesis when supported.
4. **Evidence fidelity** — all existing Claim Atom, temporal, provenance, direct-quote, epistemic, and fail-closed rules remain authoritative.

## 3. Explicit Supersession

This design supersedes the following earlier product assumptions wherever they conflict:

- `docs/superpowers/specs/2026-08-30-reader-first-editorial-presentation-design.md` section 5.2, which says the article should choose a small number of central narrative lines and need not mention every selected Story;
- `docs/superpowers/plans/2026-08-30-reader-first-editorial-presentation.md` global constraint 9 (`Article coverage is selective`);
- the old rich-day soft target of roughly `800–1400 words / 3–5 sections` as the preferred upper shape for a rich city day.

The following earlier decisions are **not** superseded:

- Event-First article remains at most one generative writer call;
- no AI repair, reviewer, fact-check, or regeneration call is added;
- fail-closed Evidence Boundary remains in force;
- direct quotes remain exact-source-only;
- Claim Atoms remain source-close validation metadata, not prose templates;
- community/single-source reports remain publishable when faithfully attributed;
- resident questions remain context and do not create factual service state;
- digest behavior is unchanged by this design.

## 4. Target Reader Experience

A rich article should feel like a coherent walk through the day rather than a three-story column or a database dump.

Example shape:

```text
# Headline carrying the dominant city-wide frame

Lead: 2–4 major developments and the overall city condition.

## Major line: electricity / water / safety / transport / another high-impact theme
2–5 paragraphs with chronology, locations, resident actions, current status, and concrete details.

## Major or supporting line
2–4 paragraphs.

## How residents adapt / practical city life
Several related smaller observations woven into a coherent section.

## Services, transport, schools, sport, seasonal city life
Compact coverage of multiple lower-weight but meaningful items.

## Other city developments
Short paragraphs for remaining useful items when the day is rich enough.
```

The article may have 4–8 sections on a rich day. The section count follows the material rather than a fixed three-theme doctrine.

## 5. Editorial Prominence, Not Article-Worthiness

Introduce three deterministic presentation levels for every publishable Story that reaches the article context:

```python
ArticleProminence = Literal["DEVELOP", "WEAVE", "BRIEF"]
```

### DEVELOP

Use for major or evidence-rich lines.

Expected presentation:

- usually one dedicated section or a dominant subsection;
- multiple paragraphs when evidence supports them;
- chronology/current status where relevant;
- 2–3 concrete microdetail supports when available.

### WEAVE

Use for meaningful supporting developments.

Expected presentation:

- woven into an existing thematic section or given a shorter dedicated section;
- usually 1–2 paragraphs;
- at least one concrete microdetail support when available.

### BRIEF

Use for smaller but legitimate city-life information.

Expected presentation:

- 1–2 sentences, often grouped with other BRIEF stories in a natural city-life section;
- at least one supported factual proposition;
- no requirement for a dedicated heading.

`BRIEF` does **not** mean omit. It means compact coverage.

## 6. Deterministic ArticleCoveragePlan

No new planner LLM call is allowed.

Add a deterministic plan derived from the existing ordered `StoryCard` list plus `ArticleEditorialContext` supports:

```python
@dataclass(frozen=True)
class ArticleStoryCoverage:
    story_id: str
    topic: str
    rank: int
    prominence: ArticleProminence
    support_ids: tuple[str, ...]
    detail_support_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArticleCoveragePlan:
    stories: tuple[ArticleStoryCoverage, ...]

    @property
    def story_ids(self) -> tuple[str, ...]: ...

    def to_prompt_context(self) -> str: ...
```

### 6.1 Inclusion rule

The plan includes every Story that has at least one `PUBLISH` ArticleSupport, except items already excluded upstream by existing publication policy.

The planner must **not** invent a new hard article-worthiness gate.

`resident_question` / `CONTEXT` supports do not create standalone Story coverage requirements.

### 6.2 Prominence rule

Initial deterministic policy:

```text
DEVELOP when:
  StoryCard.importance == "high"
  OR PUBLISH support_count >= 4

WEAVE when:
  not DEVELOP
  AND PUBLISH support_count >= 2

BRIEF otherwise
```

The ordered StoryCard rank is preserved in the plan and is used to order peers within one prominence class.

The planner may later evolve, but this first version stays simple and deterministic.

## 7. Microdetail Preservation

The article must not replace evidence-rich reporting with generic abstractions such as:

```text
"Residents adapt to outages."
"Services continue to operate."
"The city is preparing for autumn."
```

when the authorized material contains useful specifics such as:

```text
- residents pooled a specific amount for a shared generator;
- one resident powered telecom equipment from a generator;
- a bus currently runs roughly once per hour;
- a neighborhood has had no water for a stated period;
- a resident charges a phone from a solar panel;
- a short exact quote captures lived experience;
- a named municipal service changed its operating status.
```

### 7.1 Detail-support selection

Each `ArticleStoryCoverage` contains `detail_support_ids` selected deterministically from its PUBLISH supports.

A support receives a higher specificity score when its `text` / `source_text` contains one or more of:

- numbers, dates, times, durations, money, percentages, or acronyms already recognized by `extract_concrete_claims()`;
- a concrete resident action or workaround (`generator`, charging, water delivery, route interval, queue, service access, etc.) as expressed in the canonical evidence text;
- a named place, service, organization, or neighborhood already present in the support;
- an exact short quote candidate in primary source text;
- `community_report`, `service_access`, or `operational_observation` evidence with concrete source text.

The first implementation does not attempt full NLP extraction. It scores supports, not free-form semantic concepts.

### 7.2 Detail budgets by prominence

```text
DEVELOP: up to 3 detail_support_ids
WEAVE:   up to 2 detail_support_ids
BRIEF:   up to 1 detail_support_id
```

When fewer qualifying supports exist, use what is available. Never fabricate a detail to satisfy the budget.

### 7.3 Non-blocking quality semantics

Microdetail retention is an editorial-quality metric, not a factual safety gate.

A draft that is evidence-valid must not be fail-closed solely because it omitted an optional microdetail.

Coverage diagnostics may emit warnings and benchmark failures, but they must not set `ArticleValidationResult.is_valid=False`.

This prevents a return to the earlier failure mode where stronger verification caused no articles to publish.

## 8. Reader-Facing Detail Policy

Microdetail does not mean directory payload.

### Preserve when supported

- neighborhood/street relevant to the event;
- exact route interval;
- amount residents pooled for a shared solution;
- service/operator name when materially relevant;
- operating time when it is itself useful context;
- practical resident action;
- brief exact quote;
- concrete before/after or current-state contrast.

### Suppress from writer input by default

- phone numbers;
- booking URLs;
- social handles used only for ordering/contact;
- raw call-to-action fragments such as "call now", "book here", "write in DM".

Suppression is only for the writer prompt. Raw `source_text` remains available in `ArticleSupport` for deterministic validation and audit.

Do **not** globally suppress prices, addresses, or organization names: these can be legitimate microdetails when they are actually the news (for example, a municipal fare change or a location-specific service outage).

## 9. Writer Prompt Contract

The writer receives two blocks before the support corpus:

```text
ARTICLE COVERAGE PLAN
- DEVELOP story:... detail supports: ...
- WEAVE story:... detail supports: ...
- BRIEF story:... detail supports: ...

EDITORIAL RULES
- prominence controls depth, not inclusion;
- cover all planned stories when evidence fits within hard output limits;
- DEVELOP gets depth;
- WEAVE gets compact narrative;
- BRIEF gets compact factual coverage, often grouped;
- preserve concrete detail from marked detail supports;
- do not turn the article into a directory or ad board;
- phones/URLs/contact CTAs are intentionally hidden from writer source text;
- use fewer abstractions when concrete evidence is available.
```

The writer still returns the existing `StructuredArticleDraft` schema. No new model-output schema is required.

## 10. Article Length Policy

The existing rich target is too small for the new product direction.

Update defaults and soft targets:

```text
thin:
  <= 4 publishable story IDs OR <= 8 PUBLISH supports
  target 350–800 words
  target 2–4 sections

standard:
  <= 10 publishable story IDs OR <= 24 PUBLISH supports
  target 700–1400 words
  target 3–6 sections

rich:
  everything above
  target 1200–2000 words
  target 4–8 sections

hard minimum:
  unchanged at min(180, configured article_min_words)

hard maximum default:
  2200 words

hard maximum sections default:
  8
```

The soft targets remain non-blocking. The hard maximum still protects runaway output.

## 11. Coverage Diagnostics

Add non-blocking deterministic diagnostics after a draft passes Evidence Boundary:

```python
@dataclass(frozen=True)
class ArticleCoverageDiagnostics:
    planned_story_count: int
    covered_story_count: int
    uncovered_story_ids: tuple[str, ...]
    develop_story_coverage: float
    weave_story_coverage: float
    brief_story_coverage: float
    planned_detail_support_count: int
    covered_detail_support_count: int
    uncovered_detail_support_ids: tuple[str, ...]
    detail_support_coverage: float
    leaked_contact_payloads: tuple[str, ...]
```

Coverage is derived from the support IDs actually cited by title/lead/headings/paragraphs.

These values are recorded in generation-attempt metadata and benchmark output.

They are not ArticleValidator blockers.

## 12. Contact / Directory Leakage Diagnostics

Even though phone numbers and URLs are removed from writer-facing source text, add a post-draft diagnostic scanner for:

- phone numbers;
- `http://` / `https://` URLs;
- `t.me/` links;
- obvious booking/contact CTAs.

The first version is warning-only and benchmark-visible.

Reason: supported contact data is not a hallucination, so Evidence Boundary should not reject it as false. The product contract instead reduces its probability before generation and measures any leakage.

## 13. Single-Call and Fail-Closed Invariants

This design must not add any extra generative call.

The flow remains:

```text
FrozenEditorialInput
  -> ArticleEditorialContext
  -> deterministic ArticleCoveragePlan
  -> deterministic writer-context rendering/sanitization
  -> exactly one writer LLM call
  -> existing ArticleValidator / Evidence Boundary
     FAIL -> ArticlePublicationRejected -> no publication
     PASS -> coverage diagnostics (non-blocking)
          -> publication
```

No repair call. No second planner call. No fallback article.

## 14. Comparison / Benchmark Acceptance

A rich-day clean A/B should report at least:

- writer call count;
- article published/rejected;
- word count;
- section count;
- planned stories by prominence;
- covered stories by prominence;
- story coverage ratio;
- detail-support coverage ratio;
- leaked phone/URL/contact payload count;
- Evidence Boundary blocking issue count.

The desired article should show broad coverage without becoming a raw inventory.

## 15. Concrete Regression Expectations

Using synthetic fixtures derived from the 2026-08-30 real article behavior:

1. A high-impact electricity Story with four supports becomes `DEVELOP` and receives up to three detail supports.
2. A two-support telecom workaround Story becomes `WEAVE` and preserves the concrete generator/Wi-Fi adaptation support.
3. A one-support sports-school enrollment Story becomes `BRIEF`, not omitted.
4. A service-access support containing a route plus phone/URL keeps the route fact but writer source text hides the phone/URL.
5. A municipal fare change containing an amount preserves the amount because it is the news, not contact payload.
6. A resident question remains `CONTEXT` and creates no coverage requirement.
7. A rich plan with 12+ stories uses rich length targets of 1200–2000 words and 4–8 sections.
8. A valid article with imperfect optional detail coverage remains publishable; the shortfall appears only in diagnostics.
9. Existing unsupported locations, numbers, direct-quote changes, temporal errors, and unsupported causal claims continue to hard-reject exactly as before.

## 16. Out of Scope

- changing Gate v4 retention semantics;
- changing resident-question semantics;
- changing geography/scope classification;
- changing event clustering or `join_similarity`;
- adding another LLM call;
- adding an AI fact checker or repair pass;
- weakening existing Evidence Boundary hard blockers;
- forcing every raw source fragment into the article;
- turning the article into a directory, classifieds page, or product catalog;
- changing digest presentation.
