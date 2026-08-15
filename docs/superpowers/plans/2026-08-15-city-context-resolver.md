# Berdyansk CityContextResolver and StoryContextEnricher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic Berdyansk entity resolution and story-level geographic aggregation so Telegram references such as a street name, neighborhood alias, route number, or provider name are normalized before LLM analysis without turning CityProfile into evidence of current events.

**Architecture:** Load the checked-in Berdyansk CityProfile once in `ArticleGenerator`, resolve each retained `SourceRecord` into typed local-context annotations during input preparation, and render those annotations into the Analyzer prompt. After Story Cards are selected, aggregate all referenced source records (`card.all_source_refs() & bundle.records.keys()`) into story-level area evidence, deduplicating multiple observations from the same canonical area. Writer and Fact Checker receive that deterministic context through the existing `PreparedBundle`; current-event truth always remains attached to today's `S######` source refs.

**Tech Stack:** Python dataclasses, PyYAML (already used by the project), pytest/pytest-asyncio, existing Story Card pipeline.

---

## Global Constraints

- Implement after the Scale Claims / Corpus Boundary / Stale Artifacts / Heading Safety hardening passes (already committed and merged).
- Do not modify `.agents/skills/news-style/SKILL.md` in this feature; local-context evidence rules belong in runtime Analyzer/Writer/Audit prompts so this change does not conflict with skill definitions.
- `CityProfile` is interpretive context only. It never establishes that a current outage, restoration, delay, closure, or other event happened today.
- Preserve every existing `S######` source ref unchanged through preparation, analysis, writer selection, audit, fallback, and repair.
- Normalize syntax aggressively; infer semantics conservatively. Ambiguous address matches return candidates and never guess.
- Municipal geography and colloquial geography are separate dimensions. Never count both as separate areas for scale.
- Until `editorial_scale_area_set.status == "approved"` and `exhaustive_for_scale == true`, `majority_supported` and `broad_prevalence_supported` must always be `false`.
- Current operational entries for routes/providers are `last_verified` context only and do not prove present-day operation.
- Do not add embeddings, vector storage, RAG, external geocoding, network calls, or another LLM call.
- If the CityProfile file is missing or invalid, log one warning and continue the article pipeline with city context disabled; publication must not fail because local context is unavailable.
- Do not stage local `config.yaml` or debug artifacts.

---

## File Map

- Create: `src/city_context_models.py` - provider-agnostic typed context models.
- Create: `src/city_context.py` - profile loader, indexes, deterministic resolver, story enricher, prompt rendering helpers.
- Added: `data/city_profiles/berdyansk.yaml` - reviewed `berdyansk_city_profile_v2.yaml` content.
- Added: `docs/city_profiles/berdyansk-sources.md` - reviewed source/provenance report.
- Modify: `src/editorial_models.py` - attach optional per-source city annotation and optional story contexts to `PreparedBundle`.
- Modify: `src/editorial_input.py` - run resolver for retained messages and centralize prompt rendering.
- Modify: `src/editorial_analysis.py` - reuse centralized source rendering in context batching and add local-context evidence contract.
- Modify: `src/editorial_writer.py` - add explicit use/boundary instructions for deterministic local context.
- Modify: `src/editorial_audit.py` - audit over-expansion from street evidence to whole-area claims and unsupported scale usage.
- Modify: `src/article_generator.py` - load resolver once, inject it into input preparation, enrich selected Story Cards before writing/audit.
- Modify: `src/editorial_fallback.py` - use resolved annotations rather than maintaining duplicate hard-coded Berdyansk area/provider dictionaries.
- Test: `tests/test_city_context.py`
- Test: `tests/test_editorial_input.py`
- Test: `tests/test_editorial_analysis.py`
- Test: `tests/test_editorial_writer.py`
- Test: `tests/test_editorial_audit.py`
- Test: `tests/test_article_generator.py`
- Test: `tests/test_editorial_fallback.py`

---

### Task 1: Check in CityProfile and add typed context models

**Files:**
- Existing: `data/city_profiles/berdyansk.yaml`
- Existing: `docs/city_profiles/berdyansk-sources.md`
- Create: `src/city_context_models.py`
- Create: `tests/test_city_context.py`

**Interfaces (`src/city_context_models.py`):**

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
    kind: str
    entity_id: str
    matched_text: str
    canonical_name: str
    object_type: str = ""
    confidence: str = "high"
    municipal_areas: tuple[AreaCandidate, ...] = ()
    colloquial_area_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class CityContextAnnotation:
    entities: tuple[ResolvedEntity, ...] = ()

@dataclass(frozen=True)
class AreaEvidence:
    area_set: str
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

- [ ] **Step 1: Write failing model/profile validation tests in `tests/test_city_context.py`**

```python
import yaml
from pathlib import Path
from src.city_context_models import (
    AreaCandidate,
    ResolvedEntity,
    CityContextAnnotation,
    AreaEvidence,
    ScaleEvidence,
    StoryContext,
)


def test_city_profile_yaml_structure():
    path = Path("data/city_profiles/berdyansk.yaml")
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    assert profile["schema_version"] == 2
    assert profile["profile_id"] == "berdyansk"
    assert profile["safety_contract"]["context_not_event_evidence"] is True
    assert profile["stable_context"]["geography"]["editorial_scale_area_set"]["exhaustive_for_scale"] is False
    assert len(profile["stable_context"]["geography"]["street_gazetteer"]["entries"]) >= 400


def test_city_context_models_instantiation():
    cand = AreaCandidate(
        area_set="municipal",
        area_id="center",
        area_name="Центр",
        confidence="high",
        coverage_kind="whole_object",
        source_ref="gazetteer",
    )
    entity = ResolvedEntity(
        kind="place",
        entity_id="street:Шевченка",
        matched_text="Шевченко",
        canonical_name="вулиця Шевченка",
        object_type="street",
        municipal_areas=(cand,),
    )
    annot = CityContextAnnotation(entities=(entity,))
    assert len(annot.entities) == 1
    assert annot.entities[0].entity_id == "street:Шевченка"

    evidence = AreaEvidence(
        area_set="municipal_neighborhood_committees_2021",
        area_id="center",
        source_refs=("S000001", "S000002"),
        direct_area_refs=("S000002",),
        inferred_from_place_refs=("S000001",),
    )
    assert evidence.area_set == "municipal_neighborhood_committees_2021"
    assert evidence.area_id == "center"
    assert len(evidence.source_refs) == 2

    scale = ScaleEvidence(
        observed_area_ids=("center", "liski"),
        observed_count=2,
        geographic_spread=True,
        broad_prevalence_supported=False,
        majority_supported=False,
    )
    context = StoryContext(
        card_id="SC001",
        municipal_areas=(evidence,),
        scale=scale,
    )
    assert context.card_id == "SC001"
    assert context.scale.geographic_spread is True
```

- [ ] **Step 2: Run the failing tests**

```bash
uv run pytest -q tests/test_city_context.py --no-cov
```

Expected: FAIL (`ModuleNotFoundError: No module named 'src.city_context_models'`).

- [ ] **Step 3: Implement pure dataclasses in `src/city_context_models.py`**

Create `src/city_context_models.py` defining `AreaCandidate`, `ResolvedEntity`, `CityContextAnnotation`, `AreaEvidence`, `ScaleEvidence`, and `StoryContext`.

- [ ] **Step 4: Run tests to verify GREEN**

```bash
uv run pytest -q tests/test_city_context.py --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/city_context_models.py tests/test_city_context.py
git commit -m "feat: add Berdyansk city context profile models"
```

---

### Task 2: Normalize profile coverage rules in YAML and implement deterministic resolver

**Files:**
- Modify: `data/city_profiles/berdyansk.yaml`
- Create: `src/city_context.py`
- Modify: `tests/test_city_context.py`

**Interfaces (`src/city_context.py`):**

```python
class CityProfileError(RuntimeError):
    pass

class CityContextResolver:
    @classmethod
    def from_yaml(cls, path: str | Path) -> "CityContextResolver": ...
    def resolve(self, text: str) -> CityContextAnnotation: ...
```

- [ ] **Step 1: Write RED tests for direct area resolution, exact place resolution, aliases, and address precision in `tests/test_city_context.py`**

```python
def test_city_context_resolver_exact_and_aliases():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # Direct area mentions (stable identity + canonical name)
    res_center = resolver.resolve("В центре снова нет света")
    area_center = next(e for e in res_center.entities if e.kind == "area" and e.entity_id == "center")
    assert area_center.entity_id == "center"
    assert area_center.canonical_name == "Центр міста"

    res_liski = resolver.resolve("На Лисках нет воды")
    area_liski = next(e for e in res_liski.entities if e.kind == "area" and e.entity_id == "liski")
    assert area_liski.entity_id == "liski"
    assert area_liski.canonical_name == "Ліски"

    # Streets and aliases
    res1 = resolver.resolve("На ул. Шевченко нет света")
    assert any(e.entity_id == "street:Шевченка" for e in res1.entities)

    res2 = resolver.resolve("На бульваре Шевченко нет света")
    assert any(e.entity_id == "boulevard:Шевченка" for e in res2.entities)

    res3 = resolver.resolve("На Морозова воды нет")
    assert any(e.entity_id == "street:Північна" for e in res3.entities)

    # Providers and routes
    res4 = resolver.resolve("Юпитер снова работает")
    assert any(e.entity_id == "jupiter" for e in res4.entities)

    res5 = resolver.resolve("Поинт лежит")
    assert any(e.entity_id == "point" for e in res5.entities)

    res6 = resolver.resolve("маршрут 15 сегодня не ходит")
    assert any(e.entity_id == "route:15" for e in res6.entities)

    # Bare number outside transport context does not become route 15
    res7 = resolver.resolve("В доме 15 отключили воду")
    assert not any(e.entity_id == "route:15" for e in res7.entities)


def test_city_context_resolver_multi_area_ambiguity():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    res = resolver.resolve("На Мелитопольском шоссе нет света")
    street_entity = next(e for e in res.entities if "Мелітопольське" in e.canonical_name or "шосе" in e.canonical_name)
    assert len(street_entity.municipal_areas) > 1
    assert street_entity.confidence == "ambiguous"


def test_city_context_resolver_loader_error_contract(tmp_path):
    import pytest
    from src.city_context import CityProfileError

    # Non-existent file propagates FileNotFoundError
    with pytest.raises(FileNotFoundError):
        CityContextResolver.from_yaml(tmp_path / "non_existent.yaml")

    # Corrupt YAML raises CityProfileError
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("schema_version: 999\nprofile_id: [invalid", encoding="utf-8")
    with pytest.raises(CityProfileError):
        CityContextResolver.from_yaml(bad_yaml)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest -q tests/test_city_context.py --no-cov
```

- [ ] **Step 3: Convert all 468 `rule:` strings to structured `coverage:` in `data/city_profiles/berdyansk.yaml`**

Mechanically convert:
- 432 `whole_object` mappings $\rightarrow$ `coverage: {kind: whole_object}`
- 36 non-trivial mappings $\rightarrow$ structured `coverage:`:
  - `kind: house_numbers` (with `houses: [...]` list and optional `private_sector: bool`)
  - `kind: side` (with `side: "even" | "odd"`)
  - `kind: segment` (with `from_landmark`, `to_landmark`, `from_house`, `to_house`, `side`)
  - `kind: any_of` (with `clauses: [...]`)

Verify with acceptance assertions:
`count_rule_keys(profile) == 0` and `count_coverage_keys(profile) == 468`.

- [ ] **Step 4: Implement `CityContextResolver` in `src/city_context.py`**

- Build loader validating schema and raising `CityProfileError` on malformed structures (`FileNotFoundError` propagates).
- Build normalization `_normalize(text)` (NFKC, casefold, replace ё $\rightarrow$ е, trim).
- Build deterministic surface-form prefix normalization:
  - `ул.` / `улица` / `улице` / `улицы` / `вул.` / `вулиця` / `вулиці` $\rightarrow$ `street`
  - `бульвар` / `бульваре` / `бул.` $\rightarrow$ `boulevard`
  - `проспект` / `проспекте` / `пр-т` $\rightarrow$ `prospect`
  - `пер.` / `переулок` / `переулке` / `провулок` / `провулку` $\rightarrow$ `lane`
  - `шоссе` / `шосе` $\rightarrow$ `highway`
- Build indices: `_typed_place_aliases`, `_untyped_place_aliases`, `_area_aliases` (including conversational case forms like `центре`, `в центре`, `Лисках`, `на Лисках`, `на горе`, `нагорной части`, `на Косе`, `Косе`), `_provider_aliases`, `_route_numbers`.
- Implement address parser for house numbers/segments when matching multi-candidate streets.
- If coverage details are insufficient, return all candidates with `confidence: "ambiguous"`.

- [ ] **Step 5: Run tests to verify PASS**

```bash
uv run pytest -q tests/test_city_context.py --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add data/city_profiles/berdyansk.yaml src/city_context.py tests/test_city_context.py
git commit -m "feat: resolve Berdyansk local entities deterministically"
```

---

### Task 3: Attach city context to SourceRecord and preserve in Analyzer batching

**Files:**
- Modify: `src/editorial_models.py`
- Modify: `src/editorial_input.py`
- Modify: `src/editorial_analysis.py`
- Modify: `tests/test_editorial_input.py`
- Modify: `tests/test_editorial_analysis.py`

**Interfaces:**

```python
# src/editorial_models.py
@dataclass
class SourceRecord:
    ref: str
    message: Message
    source_type: str
    parent_ref: str | None
    context_text: str
    city_context: CityContextAnnotation | None = None

@dataclass
class PreparedBundle:
    records: dict[str, SourceRecord]
    prompt_text: str
    total_messages: int
    candidate_count: int
    story_contexts: dict[str, StoryContext] = field(default_factory=dict)
```

- [ ] **Step 1: Write RED tests in `tests/test_editorial_input.py` and `tests/test_editorial_analysis.py`**

Test that `EditorialInputBuilder` annotates records when `city_context_resolver` is provided, and that `render_records` emits `local_context:` lines. Test that `EditorialAnalyzer._split_bundle()` preserves `local_context:` across split batches.

- [ ] **Step 2: Implement `city_context` in `EditorialInputBuilder` and centralize `render_records()`**

Update `EditorialInputBuilder.build()` to populate `SourceRecord.city_context`.
Expose `EditorialInputBuilder.render_records()` as the single canonical renderer for both input preparation and analyzer batch splitting.

- [ ] **Step 3: Add Analyzer evidence-boundary prompt text in `src/editorial_analysis.py`**

Add local context boundary instruction:
`LOCAL CONTEXT annotations are deterministic interpretation aids. They may establish that a source observation came from a broader known area or refers to a known provider/route. They do not establish that the reported phenomenon affected the whole broader area, and they never establish a current event without the attached source record.`

- [ ] **Step 4: Run tests to verify PASS**

```bash
uv run pytest tests/test_editorial_input.py tests/test_editorial_analysis.py tests/test_editorial_models.py --no-cov
```

- [ ] **Step 5: Commit**

```bash
git add src/editorial_models.py src/editorial_input.py src/editorial_analysis.py tests/test_editorial_input.py tests/test_editorial_analysis.py
git commit -m "feat: attach local context to editorial source records"
```

---

### Task 4: Aggregate Story Card geography without double counting and with ambiguity safety

**Files:**
- Modify: `src/city_context.py`
- Modify: `tests/test_city_context.py`

**Interfaces:**

```python
class StoryContextEnricher:
    def __init__(self, resolver: CityContextResolver): ...
    def enrich(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
    ) -> dict[str, StoryContext]: ...
```

- [ ] **Step 1: Write RED aggregation and ambiguity safety tests in `tests/test_city_context.py`**

```python
from datetime import datetime
from src.database import Message
from src.editorial_models import EditorialAnalysis, PreparedBundle, SourceRecord, StoryCard


def test_story_context_enricher_same_area_deduplication():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    # S000001 on Shevchenko St (inferred Center), S000002 directly in Center, S000003 in Liski
    rec1 = SourceRecord(
        ref="S000001",
        message=Message(id=1, text="На ул. Шевченко нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("На ул. Шевченко нет света"),
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=Message(id=2, text="В центре нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("В центре нет света"),
    )
    rec3 = SourceRecord(
        ref="S000003",
        message=Message(id=3, text="На Лисках нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("На Лисках нет света"),
    )
    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2, "S000003": rec3},
        prompt_text="",
        total_messages=3,
        candidate_count=3,
    )
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения в Центре и на Лисках",
        representative_source_refs=["S000001", "S000002", "S000003"],
    )
    analysis = EditorialAnalysis(cards=[card])
    contexts = enricher.enrich(analysis, bundle)

    assert "SC001" in contexts
    context = contexts["SC001"]
    assert context.scale.observed_count == 2
    assert context.scale.geographic_spread is True
    assert set(context.scale.observed_area_ids) == {"center", "liski"}

    center = next(area for area in context.municipal_areas if area.area_id == "center")
    assert center.source_refs == ("S000001", "S000002")
    assert center.inferred_from_place_refs == ("S000001",)
    assert center.direct_area_refs == ("S000002",)


def test_story_context_enricher_uses_all_source_refs():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    rec1 = SourceRecord(
        ref="S000001",
        message=Message(id=1, text="В центре нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("В центре нет света"),
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=Message(id=2, text="На Лисках нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("На Лисках нет света"),
    )
    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2},
        prompt_text="",
        total_messages=2,
        candidate_count=2,
    )
    # S000001 is representative, but S000002 is in community_observations
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения света",
        representative_source_refs=["S000001"],
        community_observations=[
            {"claim": "На Лисках тоже темно", "source_refs": ["S000002"]}
        ],
    )
    analysis = EditorialAnalysis(cards=[card])
    contexts = enricher.enrich(analysis, bundle)

    context = contexts["SC001"]
    assert context.scale.observed_count == 2
    assert set(context.scale.observed_area_ids) == {"center", "liski"}
    assert context.scale.geographic_spread is True


def test_ambiguous_place_does_not_inflate_scale():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    enricher = StoryContextEnricher(resolver)

    # Melitopolske Highway without house number maps to multiple municipal areas with confidence: ambiguous
    rec = SourceRecord(
        ref="S000001",
        message=Message(id=1, text="На Мелитопольском шоссе нет света", date=datetime.now()),
        source_type="channel",
        parent_ref=None,
        context_text="",
        city_context=resolver.resolve("На Мелитопольском шоссе нет света"),
    )
    assert rec.city_context is not None
    assert len(rec.city_context.entities) >= 1
    highway_entity = next(
        e for e in rec.city_context.entities
        if "Мелітопольське" in e.canonical_name or "шосе" in e.canonical_name
    )
    assert len(highway_entity.municipal_areas) > 1
    assert highway_entity.confidence == "ambiguous"

    bundle = PreparedBundle(
        records={"S000001": rec},
        prompt_text="",
        total_messages=1,
        candidate_count=1,
    )
    card = StoryCard(
        id="SC001",
        topic="Отключения света",
        importance="high",
        summary="Отключения на шоссе",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    contexts = enricher.enrich(analysis, bundle)

    context = contexts["SC001"]
    assert context.municipal_areas == ()
    assert context.scale.observed_count == 0
    assert context.scale.geographic_spread is False
    assert context.scale.majority_supported is False
```

- [ ] **Step 2: Implement `StoryContextEnricher.enrich()` in `src/city_context.py`**

- Collect all evidence refs: `evidence_refs = card.all_source_refs() & bundle.records.keys()`.
- Filter for **unambiguous municipal areas only** (direct area mentions or place matches with single unambiguous municipal area / `confidence != "ambiguous"`).
- Group unambiguous references by `(area_set, area_id)`.
- Compute `ScaleEvidence`:
  - `observed_area_ids = tuple(sorted(unique_unambiguous_municipal_area_ids))`
  - `observed_count = len(observed_area_ids)`
  - `geographic_spread = (observed_count >= 2)`
  - `broad_prevalence_supported = False`
  - `majority_supported = False`

- [ ] **Step 3: Run tests to verify PASS**

```bash
uv run pytest -q tests/test_city_context.py --no-cov
```

- [ ] **Step 4: Commit**

```bash
git add src/city_context.py tests/test_city_context.py
git commit -m "feat: aggregate story geography from source refs"
```

---

### Task 5: Integrate local context into ArticleGenerator, Writer, Fact Checker, and Fallback

**Files:**
- Modify: `src/article_generator.py`
- Modify: `src/editorial_writer.py`
- Modify: `src/editorial_audit.py`
- Modify: `src/editorial_fallback.py`
- Modify: `tests/test_article_generator.py`
- Modify: `tests/test_editorial_writer.py`
- Modify: `tests/test_editorial_audit.py`
- Modify: `tests/test_editorial_fallback.py`

- [ ] **Step 1: Write RED tests for ArticleGenerator, Writer, and Audit prompts**

- ArticleGenerator: missing or corrupt YAML catches `(CityProfileError, FileNotFoundError)`, logs warning, and continues generation with city context disabled.
- Writer prompt: includes rules that street-to-area mapping means observation came from that area (not that entire area was affected), same-area reports count as 1 area, and `majority_supported` is used only when deterministic scale evidence sets it to true.
- Audit prompt: flags un-scoped whole-area generalization from street-only evidence as FIX.
- Fallback: uses `SourceRecord.city_context` when present; when city context is unavailable, deterministic fallback continues normal text processing without performing CityProfile-dependent entity enrichment (removes duplicate `_KNOWN_AREAS` / `_KNOWN_PROVIDERS`).

- [ ] **Step 2: Implement `ArticleGenerator` profile loading and enrichment pipeline**

- In `ArticleGenerator.__init__`: load `CityContextResolver.from_yaml` (catch `(CityProfileError, FileNotFoundError)`, log `WARNING`, set `city_context_resolver = None`).
- In `generate_article()`: after Analyzer produces Story Cards and `selected_refs` are filtered for the writer bundle, run `StoryContextEnricher.enrich(analysis, writer_bundle)` and attach `story_contexts` to `writer_bundle`.

- [ ] **Step 3: Render deterministic story context into writer bundle prompt**

Render `[LOCAL STORY CONTEXT SC###]` blocks into `PreparedBundle.prompt_text` for Writer and Fact Checker.

- [ ] **Step 4: Update Writer, Audit, and Fallback components**

- Add deterministic local context instructions to `EditorialWriter.build_prompt()`.
- Add street-to-area audit check to `LightFactChecker._build_system_prompt()`.
- Update `EditorialFallback` to leverage `SourceRecord.city_context` and remove duplicate hardcoded dictionaries.

- [ ] **Step 5: Run tests to verify PASS**

```bash
uv run pytest tests/test_article_generator.py tests/test_editorial_writer.py tests/test_editorial_audit.py tests/test_editorial_fallback.py --no-cov
```

- [ ] **Step 6: Commit**

```bash
git add src/article_generator.py src/editorial_writer.py src/editorial_audit.py src/editorial_fallback.py tests/test_article_generator.py tests/test_editorial_writer.py tests/test_editorial_audit.py tests/test_editorial_fallback.py
git commit -m "feat: use Berdyansk context across editorial pipeline"
```

---

### Task 6: End-to-end regressions and live dry-run verification

**Files:**
- Modify: `tests/test_article_generator.py`
- Modify: `tests/test_city_context.py`

- [ ] **Step 1: Add end-to-end regressions in `tests/test_city_context.py` and `tests/test_article_generator.py`**

- Same-area regression: `S1` (Shevchenko), `S2` (Center), `S3` (Liski) $\rightarrow$ 2 unique municipal areas, Center has 2 refs.
- Ambiguity scale safety regression: ambiguous multi-area street does not inflate `observed_count`.
- Toponym alias regression: `Морозова`, `Северная`, `Північна` resolve to the same street entity.
- Collision regression: `улица Шевченко` vs `бульвар Шевченко` are distinct entities with distinct area memberships.
- Provider/Route recognition: `Юпитер`, `Поинт`, `+7Телеком`, `МирТелеком`, `маршрут 15` recognized as local entities without operational status leaking as current-day events.

- [ ] **Step 2: Run full verification suite**

```bash
uv run pytest -q --no-cov
uv run flake8 src tests
uv run mypy src
git diff --check
```

Expected: all checks PASS.

- [ ] **Step 3: Run live dry-run**

```bash
uv run python main.py --article --dry-run
```

Inspect generated `data/articles/preview_editorial.md` and debug artifacts.

- [ ] **Step 4: Commit**

```bash
git add tests/test_article_generator.py tests/test_city_context.py
git commit -m "test: cover Berdyansk city context integration"
```
