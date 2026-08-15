# Berdyansk CityContextResolver and StoryContextEnricher Design Document

## 1. Overview and Goal

Telebrief generates daily local journalistic articles from raw Telegram messages. Resident messages frequently reference local entities using diverse names:
- Street names in Russian and Ukrainian, historical Soviet names (2016 renames), and recent occupation-era alias variants (e.g., *улица Шевченко*, *Северная / Північна / Морозова*);
- Colloquial neighborhood names vs. formal municipal committee boundaries (*на горе*, *на Лисках*, *в центре*);
- Local service providers (*Поинт*, *+7Телеком*, *МирТелеком*, *Бердянскводоканал*, *Запорожгаз*);
- Public transport routes (*маршрут 15*).

**Goal:** Add deterministic entity resolution and story-level geographic evidence aggregation so local entities and locations are normalized and deduplicated before LLM analysis, without converting static profile context into evidence of current-day events.

---

## 2. Hard Invariants & Editorial Boundaries

1. **Context $\neq$ Current-Day Event Evidence:**
   Static knowledge in `CityProfile` (e.g., that an entity exists or was active at last verification) *never* proves that an outage, repair, schedule change, or event happened today. All event facts must originate from today's `SourceRecord` messages.
2. **Street Evidence $\neq$ Whole-Area Evidence:**
   Evidence from a street in a given area (e.g., *«На улице Шевченко нет света»*) means the report originated from that area; it does *not* prove that the entire area was affected. Generalizing to the entire area requires explicit area-wide evidence or must be worded with source scoping (*«На улице Шевченко в центре...»*).
3. **Scale Denominator & Majority Gating:**
   Until an editor-approved exhaustive area set is explicitly configured (`editorial_scale_area_set.exhaustive_for_scale: true` and `status: "approved"`), `majority_supported` remains strictly `false`, and no denominator is assumed.
4. **Separation of Resolution and Aggregation:**
   - `CityContextResolver`: Answers *"What entity is mentioned and what geography does it map to?"*
   - `StoryContextEnricher`: Answers *"How many independent source refs of this selected Story Card map to the same canonical areas?"*
5. **Deduplication of Same-Area Reports:**
   Multiple reports referencing the same canonical area (e.g., S001 on Shevchenko St. + S027 in Center) strengthen evidence within that single area and count as **1 unique observed area**, not 2.
6. **Scoped Fail-Open:**
   If `CityProfile` is missing or invalid, the pipeline logs a `WARNING`, sets `city_context_resolver = None`, and continues regular article generation without crashing. Errors in Analyzer, Writer, or Audit are never masked.
7. **No Out-of-Scope Dependencies:**
   No SQLite, vector databases, embeddings, RAG, external geocoding network calls, or additional LLM calls.

---

## 3. Architecture & Data Flow

```text
CityProfile YAML (data/city_profiles/berdyansk.yaml)
        ↓
CityContextResolver (src/city_context.py)
        ↓
SourceRecord.city_context (src/editorial_models.py)
        ↓ (rendered via EditorialInputBuilder.render_records())
EditorialAnalyzer (src/editorial_analysis.py)
        ↓
Story Cards (EditorialAnalysis)
        ↓
StoryContextEnricher (src/city_context.py)
        ↓
PreparedBundle.story_contexts (dict[str, StoryContext])
        ↓
EditorialWriter + LightFactChecker (src/editorial_writer.py, src/editorial_audit.py)
```

### Stage Details

1. **Initialization (`ArticleGenerator`):**
   - Loads `data/city_profiles/berdyansk.yaml` once into `CityContextResolver`.
   - Initializes `StoryContextEnricher(resolver)`.
   - Injects `resolver` into `EditorialInputBuilder`.

2. **Input Preparation (`EditorialInputBuilder.build()`):**
   - Resolves retained messages into `CityContextAnnotation`.
   - Attaches `city_context` to each `SourceRecord`.
   - Renders compact deterministic annotations (e.g. `local_context: street:Шевченка -> municipal:center; colloquial:center`) into `PreparedBundle.prompt_text`.

3. **Analysis (`EditorialAnalyzer.analyze()`):**
   - Receives annotated messages in context.
   - Leverages normalized street/area linkages to group geographically related messages into coherent `StoryCard`s.

4. **Story Card Selection & Enrichment (`StoryContextEnricher.enrich()`):**
   - Runs strictly over selected `StoryCard.representative_source_refs`.
   - Aggregates canonical municipal areas and colloquial areas.
   - Computes `ScaleEvidence` (observed area count, deduplicated).
   - Generates `StoryContext` mapped to each Story Card ID.

5. **Drafting & Audit (`EditorialWriter`, `LightFactChecker`):**
   - Writer receives selected source excerpts and deterministic `[LOCAL STORY CONTEXT SC###]` blocks.
   - Writer crafts articles with accurate geographical scope without over-generalizing street observations to whole districts.
   - Fact Checker validates that draft statements match source evidence and that scale language respects deterministic `ScaleEvidence`.

6. **Fallback Pipeline (`EditorialFallback`):**
   - Uses `SourceRecord.city_context` when available, falling back gracefully to text-based matching if city context is disabled.

---

## 4. Component & Model Specifications

### 4.1 Data Models (`src/city_context_models.py`)

Pure, immutable dataclasses independent of AI providers:

```python
@dataclass(frozen=True)
class AreaCandidate:
    area_set: str
    area_id: str
    area_name: str
    confidence: str
    coverage_kind: str
    source_ref: str

@dataclass(frozen=True)
class ResolvedEntity:
    kind: str  # "place", "area", "provider", "route"
    entity_id: str  # e.g., "street:Шевченка", "point", "route:15"
    matched_text: str
    canonical_name: str
    object_type: str = ""  # "street", "boulevard", "lane", "prospect", etc.
    confidence: str = "high"
    municipal_areas: tuple[AreaCandidate, ...] = ()
    colloquial_area_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class CityContextAnnotation:
    entities: tuple[ResolvedEntity, ...] = ()

@dataclass(frozen=True)
class AreaEvidence:
    area_id: str
    source_refs: tuple[str, ...]
    direct_area_refs: tuple[str, ...] = ()
    inferred_from_place_refs: tuple[str, ...] = ()

@dataclass(frozen=True)
class ScaleEvidence:
    observed_area_ids: tuple[str, ...] = ()
    observed_count: int = 0
    total_comparable_areas: int | None = None
    coverage_ratio: float | None = None
    geographic_spread: bool = False
    broad_prevalence_supported: bool = False
    majority_supported: bool = False

@dataclass(frozen=True)
class StoryContext:
    card_id: str
    municipal_areas: tuple[AreaEvidence, ...] = ()
    colloquial_area_ids: tuple[str, ...] = ()
    scale: ScaleEvidence = ScaleEvidence()
```

### 4.2 YAML Coverage Schema (`data/city_profiles/berdyansk.yaml`)

All 36 string `rule:` entries are mechanically normalized into typed `coverage:` mappings:

- `kind: whole_object`
- `kind: house_numbers` (with `houses: [...]` list and optional `private_sector: bool`)
- `kind: side` (with `side: "even" | "odd"`)
- `kind: segment` (with `from_landmark`, `to_landmark`, `from_house`, `to_house`, `side`)
- `kind: any_of` (with `clauses: [...]`)

The runtime resolver operates strictly on structured `coverage:`.

### 4.3 Resolver & Normalizer (`src/city_context.py`)

- **Text Normalization:**
  `unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")`, whitespace normalization, and punctuation trimming.
- **Typed Identity:**
  `(object_type, canonical_name)` distinction ensures `улица Шевченко` and `бульвар Шевченко` are distinct entities with distinct area memberships.
- **Alias Indexing:**
  Precomputed in-memory lookups for place aliases, area aliases, provider aliases, and transport routes.
- **Ambiguity Preservation:**
  When a street traverses multiple municipal areas and the input text lacks house number or segment detail, all candidate areas are returned with `confidence: "ambiguous"` (no arbitrary guessing).

### 4.4 Story Context Enricher (`src/city_context.py`)

- Maps `StoryCard.representative_source_refs` $\rightarrow$ `SourceRecord.city_context`.
- Groups references by canonical municipal area ID.
- Computes `observed_count = len(unique_municipal_areas)`.
- Enforces `majority_supported = False` while `editorial_scale_area_set.exhaustive_for_scale: false`.

---

## 5. Testing & Verification Plan

1. **Unit Tests (`tests/test_city_context.py`):**
   - Profile YAML schema validation (schema version 2, safety contracts, 400+ street entries).
   - Exact entity resolution for streets, historical aliases, 2024 aliases, providers, routes.
   - Distinction between object types (`улица` vs `бульвар`).
   - Ambiguous multi-area street matching vs. narrowed house-number matching.
   - Same-area aggregation and deduplication (`S001` + `S027` in Center $\rightarrow$ 1 area, 2 refs).
   - Majority gating safety tests.
2. **Pipeline Integration Tests (`tests/test_editorial_input.py`, `tests/test_editorial_analysis.py`, `tests/test_editorial_writer.py`, `tests/test_editorial_audit.py`, `tests/test_article_generator.py`, `tests/test_editorial_fallback.py`):**
   - Preservation of `city_context` across batching splits.
   - Graceful fallback when YAML is missing/corrupted.
   - Writer and Audit prompt evidence boundaries.
3. **Full Suite & Static Quality Verification:**
   - `uv run pytest -q --no-cov`
   - `uv run flake8 src tests`
   - `uv run mypy src`
   - `git diff --check`
   - Live dry-run `uv run python main.py --article --dry-run`
