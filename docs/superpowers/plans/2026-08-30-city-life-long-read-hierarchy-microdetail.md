# City-Life Long-Read Hierarchy & Microdetail Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Event-First article into a broad city-life long read that preserves meaningful smaller stories and concrete microdetails while using deterministic editorial prominence to control depth, without adding LLM calls or weakening Evidence Boundary.

**Architecture:** Add a deterministic `ArticleCoveragePlan` between `ArticleEditorialContext` and the existing single-call writer. The plan keeps every publishable Story visible at `DEVELOP`, `WEAVE`, or `BRIEF` depth, marks high-specificity support packets as microdetail anchors, renders a sanitized writer context that hides directory contact payload while preserving raw evidence for validation, and records non-blocking coverage/detail diagnostics after the existing fail-closed validator passes.

**Tech Stack:** Python 3.12, dataclasses, pytest, existing Event-First `StoryCard` / `ArticleEditorialContext` / `ArticleSupport` / `StructuredArticleDraft` / `ArticleValidator`, existing OpenRouter/provider abstraction, PostgreSQL integration tests, no new NLP or LLM dependency.

**Spec:** `docs/superpowers/specs/2026-08-30-city-life-long-read-hierarchy-microdetail-design.md`

## Global Constraints

1. Event-First article remains **<= 1 generative LLM call total** per publication run.
2. Do not add an LLM planner, repair call, reviewer, fact-check call, or regeneration call.
3. Existing fail-closed semantics remain authoritative: a real Evidence Boundary violation still produces `ArticlePublicationRejected` and no publication.
4. Coverage/detail diagnostics introduced by this plan are **non-blocking** and must never set `ArticleValidationResult.is_valid=False`.
5. `PUBLISH` is not an equal-prominence signal. Prominence controls depth (`DEVELOP / WEAVE / BRIEF`), not factual authorization.
6. Do not introduce a new hard `ARTICLE_WORTHY` gate. Every Story with at least one non-question `PUBLISH` support enters the coverage plan.
7. `resident_question` / `CONTEXT` evidence does not create a standalone article coverage requirement.
8. Do not require official corroboration or multiple sources for a legitimate community report.
9. Preserve source-close Claim Atoms, direct-quote exactness, reporting-window rules, edition-anchor rules, multilingual support logic, unsupported-location checks, unsupported proper-name checks, concrete claim checks, and question-context overclaim checks.
10. Do not change Gate v4, Analysis v3, Edition Geography, clustering, `join_similarity`, digest generation, or City Situation.
11. Production prompt code remains generic: no hard-coded Berdyansk streets, neighborhoods, prices, operators, or local businesses.
12. Keep raw `ArticleSupport.source_text` unchanged for audit/validation. Writer-facing sanitization must use a separate rendering path.
13. Never globally strip prices, addresses, times, organization names, or numbers; they may be legitimate microdetails. Only contact/directory payload is hidden by default.
14. Preserve legacy/custom article routing; changes apply only to Event-First generation when `FrozenEditorialInput.analysis.article_context` exists.
15. Every code task follows RED -> verify failure -> minimal GREEN -> focused regression -> commit.

---

## Baseline

Baseline branch: `multisource-roadmap` at or after commit `a996b093ef2099771c0967c318fb03fa3b6b2f32` (`test(article): add attempt 74 golden draft regression test`).

Current behavior to preserve:

- `src/publication/narrative_contract.py` is at `ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v4"` and already defines source-close Claim Atoms and exact direct-quote rules.
- `src/publication/article_length.py` currently uses `300–700 / 2–3`, `500–1100 / 2–4`, and `800–1400 / 3–5` soft targets.
- `src/article_generator.py` performs exactly one Event-First writer call, validates via `validate_article_draft()`, and rejects fail-closed on blocking issues.
- `PublicationEditorialConfig` currently defaults to `article_max_words=1400` and `article_max_sections=6`.
- Existing Evidence Boundary fixes for Attempt 74 must remain green.

---

## File Structure

### New production files

- `src/publication/article_coverage.py`
  - pure deterministic coverage/prominence planning;
  - detail-support specificity scoring;
  - no DB/provider/prompt side effects.

- `src/publication/article_writer_context.py`
  - hides contact/directory payload from writer-facing `source=` text;
  - renders the coverage plan plus sanitized support corpus;
  - never mutates raw `ArticleSupport`.

- `src/publication/article_coverage_diagnostics.py`
  - measures planned-vs-cited story coverage, detail-support coverage, and contact leakage;
  - warning/metadata only; never publication blocking.

### Existing production files to modify

- `src/publication/article_context.py`
  - add `story_id` to `ArticleSupport`;
  - preserve story identity for evidence and operational supports.

- `src/publication/article_length.py`
  - widen rich-day word/section targets.

- `src/config_loader.py`
  - raise default hard article maximum to 2200 words and 8 sections.

- `src/publication/narrative_contract.py`
  - move from selective long-read wording to broad city-life coverage + hierarchy + microdetail preservation;
  - bump article narrative prompt version.

- `src/article_generator.py`
  - build the deterministic coverage plan;
  - render sanitized writer context;
  - keep exactly one provider call;
  - record coverage diagnostics in success metadata.

- `scripts/compare_article_approaches.py`
  - print coverage/detail/contact-leak metrics for the new approach.

- `scripts/benchmark_publication_quality.py`
  - aggregate the same metrics over benchmark runs.

### Existing docs to modify

- `docs/superpowers/specs/2026-08-30-reader-first-editorial-presentation-design.md`
  - add supersession note to section 5.2 selective coverage and old rich-day targets.

- `docs/superpowers/plans/2026-08-30-reader-first-editorial-presentation.md`
  - add supersession note to global constraint 9 and old article length assumptions.

### New / modified tests and fixtures

- Create: `tests/fixtures/article_city_life_coverage_cases.json`
- Create: `tests/publication/test_article_coverage.py`
- Create: `tests/publication/test_article_writer_context.py`
- Create: `tests/publication/test_article_coverage_diagnostics.py`
- Modify: `tests/publication/test_article_context.py`
- Modify: `tests/publication/test_article_length.py`
- Modify: `tests/publication/test_narrative_contract.py`
- Modify: `tests/publication/test_article_fail_closed.py`
- Modify: `tests/integration/test_event_first_narrative_publication.py`
- Modify: `tests/scripts/test_benchmark_publication_quality.py`

No database migration is required.

---

## Task 1: Freeze the City-Life Coverage Contract in Golden Fixtures

**Files:**
- Create: `tests/fixtures/article_city_life_coverage_cases.json`
- Create: `tests/publication/test_article_coverage.py`

**Interfaces:**
- Consumes: existing `StoryCard`, `ArticleEditorialContext`, and `ArticleSupport` concepts.
- Produces: fixture cases used by Tasks 2–10; no exact final article prose.

- [ ] **Step 1: Add a fixture with broad city-life cases**

Create `tests/fixtures/article_city_life_coverage_cases.json` with this structure:

```json
{
  "stories": [
    {
      "story_id": "story:power",
      "topic": "Электроснабжение и вода",
      "importance": "high",
      "supports": [
        {"id": "story:power:evidence:0:frag:1", "text": "Жители сообщают, что света нет около месяца", "kind": "community_report"},
        {"id": "story:power:evidence:1:frag:2", "text": "Жильцы дома скидываются по 300 рублей на общий генератор для подачи воды", "kind": "community_report"},
        {"id": "story:power:evidence:2:frag:3", "text": "Один житель запитал оборудование провайдера от своего генератора", "kind": "service_access"},
        {"id": "story:power:evidence:3:frag:4", "text": "Жители заряжают телефоны от солнечных панелей", "kind": "community_report"}
      ]
    },
    {
      "story_id": "story:safety",
      "topic": "Ночные события над морем",
      "importance": "high",
      "supports": [
        {"id": "story:safety:evidence:0:frag:5", "text": "Жители видели две вспышки над морем", "kind": "community_report"},
        {"id": "story:safety:evidence:1:frag:6", "text": "Примерно через 30 секунд были слышны громкие звуки", "kind": "community_report"}
      ]
    },
    {
      "story_id": "story:telecom",
      "topic": "Связь при отключениях",
      "importance": "medium",
      "supports": [
        {"id": "story:telecom:evidence:0:frag:7", "text": "Оборудование провайдера работает от генератора", "kind": "service_access"},
        {"id": "story:telecom:evidence:1:frag:8", "text": "Жители сравнивают качество мобильной связи", "kind": "community_report"}
      ]
    },
    {
      "story_id": "story:sport",
      "topic": "Набор в спортивную школу",
      "importance": "medium",
      "supports": [
        {"id": "story:sport:evidence:0:frag:9", "text": "Спортивная школа объявила бесплатный набор детей на футбол", "kind": "service_access"}
      ]
    },
    {
      "story_id": "story:route",
      "topic": "Междугородний транспорт",
      "importance": "medium",
      "supports": [
        {"id": "story:route:evidence:0:frag:10", "text": "Ежедневно есть рейсы в Краснодар и Ростов", "source_text": "Ежедневные рейсы в Краснодар и Ростов. Бронирование +79900292947, https://bus.example", "kind": "service_access"}
      ]
    },
    {
      "story_id": "story:question",
      "topic": "Вопрос о пенсионном фонде",
      "importance": "medium",
      "supports": [
        {"id": "story:question:evidence:0:frag:11", "text": "Житель спрашивает, работает ли пенсионный фонд", "kind": "resident_question", "publication_use": "CONTEXT"}
      ]
    }
  ]
}
```

- [ ] **Step 2: Write fixture schema tests before planner implementation**

Create `tests/publication/test_article_coverage.py` with a loader and these tests:

```python
def test_city_life_fixture_contains_major_supporting_brief_and_question_cases():
    data = _load_cases()
    ids = {story["story_id"] for story in data["stories"]}
    assert ids >= {
        "story:power",
        "story:safety",
        "story:telecom",
        "story:sport",
        "story:route",
        "story:question",
    }
```

- [ ] **Step 3: Run the fixture-only test**

Run:

```bash
pytest tests/publication/test_article_coverage.py::test_city_life_fixture_contains_major_supporting_brief_and_question_cases -v
```

Expected: PASS. This task freezes examples only; production planner does not exist yet.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/article_city_life_coverage_cases.json tests/publication/test_article_coverage.py
git commit -m "test(article): freeze broad city-life coverage cases"
```

---

## Task 2: Preserve Story Identity on Every ArticleSupport

**Files:**
- Modify: `src/publication/article_context.py`
- Modify: `tests/publication/test_article_context.py`
- Modify: `tests/publication/test_article_length.py`
- Modify: `tests/publication/test_article_claim_support.py`

**Interfaces:**
- Consumes: `PublicationEvidence.story_id`, `StoryCard.id`, operational `source_refs`.
- Produces: `ArticleSupport.story_id: str` used by `ArticleCoveragePlan`.

- [ ] **Step 1: Write a failing support identity test**

Add to `tests/publication/test_article_context.py`:

```python
def test_article_support_preserves_story_id_for_evidence():
    # Build one StoryCard(id="story:42") and one PublicationEvidence(story_id=42).
    # Build context through build_article_editorial_context().
    support = context.support_index[0]
    assert support.story_id == "story:42"
```

Use the existing test helpers in this file for `StoryCard` and `PublicationEvidence`; do not construct a second parallel fixture framework.

- [ ] **Step 2: Write a failing operational support identity test**

Add a case with `StoryCard(id="story:77", representative_source_refs=["telegram:...:frag:700"])` plus one resolved operational observation using that same source ref:

```python
op_support = next(s for s in context.support_index if s.support_kind == "operational")
assert op_support.story_id == "story:77"
```

- [ ] **Step 3: Run the two focused tests and verify failure**

```bash
pytest tests/publication/test_article_context.py -k "preserves_story_id or operational_support_identity" -v
```

Expected: FAIL because `ArticleSupport` has no `story_id` field.

- [ ] **Step 4: Add `story_id` to `ArticleSupport`**

Modify the dataclass:

```python
@dataclass(frozen=True)
class ArticleSupport:
    support_id: str
    text: str
    source_text: str
    support_kind: Literal["evidence", "operational"]
    publication_use: Literal["PUBLISH", "CONTEXT"]
    source_refs: tuple[str, ...]
    fragment_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    observed_at: dt.datetime | None
    effective_from: dt.datetime | None = None
    effective_until: dt.datetime | None = None
    temporal_role: TemporalRole = "CURRENT_WINDOW"
    evidence_kind: str = "established_fact"
    source_roles: tuple[str, ...] = ()
    story_id: str = ""
```

- [ ] **Step 5: Populate evidence story IDs**

When building support from `PublicationEvidence`, use:

```python
story_id=f"story:{evi.story_id}",
```

- [ ] **Step 6: Populate operational story IDs from source refs**

Before the operational observation loop, create a deterministic first-owner mapping:

```python
story_id_by_source_ref: dict[str, str] = {}
for card in cards:
    for ref in card.representative_source_refs:
        story_id_by_source_ref.setdefault(ref, card.id)
```

For each operational support:

```python
op_story_ids = [
    story_id_by_source_ref[ref]
    for ref in matching_refs
    if ref in story_id_by_source_ref
]
op_story_id = op_story_ids[0] if op_story_ids else ""
```

Pass `story_id=op_story_id` into `ArticleSupport`.

- [ ] **Step 7: Preserve compatibility for manually constructed test contexts**

Keep `story_id` as a trailing defaulted field (`story_id: str = ""`) so existing focused validator/trace tests that construct `ArticleSupport` directly do not need unrelated edits. Production builders must populate it. The coverage planner in Task 3 must fall back to parsing `story:<id>` from `support_id` when `story_id` is empty.

- [ ] **Step 8: Run focused tests**

```bash
pytest tests/publication/test_article_context.py tests/publication/test_article_length.py tests/publication/test_article_claim_support.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/publication/article_context.py tests/publication/test_article_context.py tests/publication/test_article_length.py tests/publication/test_article_claim_support.py
git commit -m "feat(article): preserve story identity in support packets"
```

---

## Task 3: Implement Deterministic DEVELOP / WEAVE / BRIEF Coverage Planning

**Files:**
- Create: `src/publication/article_coverage.py`
- Modify: `tests/publication/test_article_coverage.py`

**Interfaces:**
- Consumes: `Sequence[StoryCard]`, `ArticleEditorialContext`.
- Produces:
  - `ArticleProminence`
  - `ArticleStoryCoverage`
  - `ArticleCoveragePlan`
  - `build_article_coverage_plan(cards, context)`

- [ ] **Step 1: Add failing planner tests**

In `tests/publication/test_article_coverage.py`, build cards/context from the fixture and assert:

```python
plan = build_article_coverage_plan(cards, context)
by_id = {item.story_id: item for item in plan.stories}

assert by_id["story:power"].prominence == "DEVELOP"   # high / 4 supports
assert by_id["story:safety"].prominence == "DEVELOP"  # high
assert by_id["story:telecom"].prominence == "WEAVE"   # 2 supports
assert by_id["story:sport"].prominence == "BRIEF"     # 1 support
assert by_id["story:route"].prominence == "BRIEF"     # 1 support
assert "story:question" not in by_id                   # CONTEXT only
```

Also assert ordering follows input card rank within equal prominence.

- [ ] **Step 2: Run and verify RED**

```bash
pytest tests/publication/test_article_coverage.py -k "prominence or context_only" -v
```

Expected: FAIL because `src.publication.article_coverage` does not exist.

- [ ] **Step 3: Implement the coverage dataclasses**

Create `src/publication/article_coverage.py`:

```python
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import re
from dataclasses import dataclass
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")

ArticleProminence = Literal["DEVELOP", "WEAVE", "BRIEF"]


@dataclass(frozen=True)
class ArticleStoryCoverage:
    story_id: str
    topic: str
    rank: int
    prominence: ArticleProminence
    support_ids: tuple[str, ...]
    detail_support_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleCoveragePlan:
    stories: tuple[ArticleStoryCoverage, ...]

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.stories)
```

- [ ] **Step 4: Implement the initial prominence policy**

Add the compatibility helper:

```python

def _story_id_from_support_id(support_id: str) -> str:
    match = _STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""
```

Use only `PUBLISH` supports with a non-empty `story_id` and exclude `resident_question`:

```python
def _publish_supports_by_story(
    context: ArticleEditorialContext,
) -> dict[str, tuple[ArticleSupport, ...]]:
    grouped: dict[str, list[ArticleSupport]] = defaultdict(list)
    for support in context.support_index:
        if support.publication_use != "PUBLISH":
            continue
        if support.evidence_kind == "resident_question":
            continue
        story_id = support.story_id or _story_id_from_support_id(support.support_id)
        if not story_id:
            continue
        grouped[story_id].append(support)
    return {story_id: tuple(items) for story_id, items in grouped.items()}
```

Prominence:

```python
def _prominence(card: StoryCard, support_count: int) -> ArticleProminence:
    if card.importance == "high" or support_count >= 4:
        return "DEVELOP"
    if support_count >= 2:
        return "WEAVE"
    return "BRIEF"
```

- [ ] **Step 5: Implement `build_article_coverage_plan()` without detail scoring yet**

```python
def build_article_coverage_plan(
    cards: Sequence[StoryCard],
    context: ArticleEditorialContext,
) -> ArticleCoveragePlan:
    support_map = _publish_supports_by_story(context)
    stories: list[ArticleStoryCoverage] = []
    for rank, card in enumerate(cards, start=1):
        supports = support_map.get(card.id, ())
        if not supports:
            continue
        stories.append(
            ArticleStoryCoverage(
                story_id=card.id,
                topic=card.topic or card.summary or card.id,
                rank=rank,
                prominence=_prominence(card, len(supports)),
                support_ids=tuple(s.support_id for s in supports),
            )
        )
    return ArticleCoveragePlan(stories=tuple(stories))
```

Do not reorder stories globally in this first implementation; preserve frozen card order and let prominence control requested depth in the prompt.

- [ ] **Step 6: Run planner tests**

```bash
pytest tests/publication/test_article_coverage.py -q
```

Expected: PASS for inclusion/prominence/context-only behavior.

- [ ] **Step 7: Commit**

```bash
git add src/publication/article_coverage.py tests/publication/test_article_coverage.py
git commit -m "feat(article): add deterministic city-life coverage plan"
```

---

## Task 4: Select High-Specificity Microdetail Supports Without Full NLP

**Files:**
- Modify: `src/publication/article_coverage.py`
- Modify: `tests/publication/test_article_coverage.py`

**Interfaces:**
- Consumes: `ArticleSupport.text`, `ArticleSupport.source_text`, `extract_concrete_claims()`.
- Produces:
  - `score_detail_support(support) -> int`
  - populated `ArticleStoryCoverage.detail_support_ids`.

- [ ] **Step 1: Add failing specificity tests**

Add cases:

```python
assert score_detail_support(
    make_support("Жильцы скидываются по 300 рублей на генератор")
) > score_detail_support(
    make_support("Жители обсуждают ситуацию")
)

assert score_detail_support(
    make_support("Автобус ходит примерно раз в час")
) > 0

assert score_detail_support(
    make_support("Житель запитал оборудование провайдера от своего генератора", evidence_kind="service_access")
) > 0
```

- [ ] **Step 2: Add failing detail budget assertions**

For the fixture plan:

```python
assert len(by_id["story:power"].detail_support_ids) == 3
assert len(by_id["story:telecom"].detail_support_ids) == 2
assert len(by_id["story:sport"].detail_support_ids) == 1
```

- [ ] **Step 3: Run and verify RED**

```bash
pytest tests/publication/test_article_coverage.py -k "detail" -v
```

Expected: FAIL because detail scoring is not implemented.

- [ ] **Step 4: Implement a conservative support-level specificity score**

In `article_coverage.py` import:

```python
from src.publication.article_claims import extract_concrete_claims
```

Implement:

```python
def score_detail_support(support: ArticleSupport) -> int:
    text = " ".join(part for part in (support.text, support.source_text) if part).strip()
    if not text:
        return 0

    score = 0
    claims = extract_concrete_claims(text)
    if claims:
        score += 3
    if support.evidence_kind in {
        "community_report",
        "service_access",
        "operational_observation",
    }:
        score += 2
    if len(support.source_text.split()) >= 8:
        score += 1
    if any(marker in support.source_text for marker in ("«", "»", '"')):
        score += 1
    return score
```

Do not attempt city-specific keyword dictionaries.

- [ ] **Step 5: Implement prominence detail budgets**

```python
_DETAIL_LIMIT = {
    "DEVELOP": 3,
    "WEAVE": 2,
    "BRIEF": 1,
}


def _detail_support_ids(
    supports: tuple[ArticleSupport, ...],
    prominence: ArticleProminence,
) -> tuple[str, ...]:
    ranked = sorted(
        supports,
        key=lambda s: (-score_detail_support(s), s.support_id),
    )
    positive = [s for s in ranked if score_detail_support(s) > 0]
    chosen = positive or list(ranked)
    return tuple(s.support_id for s in chosen[: _DETAIL_LIMIT[prominence]])
```

Populate the field when building each coverage item.

- [ ] **Step 6: Run tests**

```bash
pytest tests/publication/test_article_coverage.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/publication/article_coverage.py tests/publication/test_article_coverage.py
git commit -m "feat(article): mark microdetail support anchors"
```

---

## Task 5: Render a Writer Context That Hides Directory Payload but Keeps Raw Evidence

**Files:**
- Create: `src/publication/article_writer_context.py`
- Create: `tests/publication/test_article_writer_context.py`

**Interfaces:**
- Consumes: `ArticleEditorialContext`, `ArticleCoveragePlan`.
- Produces:
  - `sanitize_writer_source_text(text: str) -> str`
  - `render_article_writer_context(context, coverage_plan) -> str`.

- [ ] **Step 1: Write failing sanitization tests**

Create `tests/publication/test_article_writer_context.py`:

```python
def test_writer_source_sanitizer_hides_phone_and_url_but_keeps_route_fact():
    raw = "Ежедневные рейсы в Краснодар и Ростов. Бронирование +79900292947, https://bus.example"
    sanitized = sanitize_writer_source_text(raw)
    assert "Краснодар" in sanitized
    assert "Ростов" in sanitized
    assert "+79900292947" not in sanitized
    assert "https://" not in sanitized
    assert "[contact omitted]" in sanitized
    assert "[link omitted]" in sanitized
```

- [ ] **Step 2: Add a regression proving ordinary numbers survive**

```python
def test_writer_source_sanitizer_does_not_strip_non_contact_numbers():
    raw = "Жильцы скидываются по 300 рублей, автобус ходит раз в час, скидка на проезд 8 рублей"
    sanitized = sanitize_writer_source_text(raw)
    assert "300" in sanitized
    assert "8" in sanitized
```

- [ ] **Step 3: Add a context rendering test**

Assert the rendered text contains:

```text
ARTICLE COVERAGE PLAN
DEVELOP story:power
WEAVE story:telecom
BRIEF story:sport
DETAIL SUPPORTS:
```

and that raw `ArticleSupport.source_text` is unchanged after rendering.

- [ ] **Step 4: Run and verify RED**

```bash
pytest tests/publication/test_article_writer_context.py -v
```

Expected: FAIL because module does not exist.

- [ ] **Step 5: Implement contact sanitization**

Create `src/publication/article_writer_context.py` with generic regexes:

```python
_PHONE_RE = re.compile(
    r"(?:\+?\d[\d\s()\-]{8,}\d)"
)
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\bt\.me/\S+", re.IGNORECASE)


def sanitize_writer_source_text(text: str) -> str:
    if not text:
        return ""
    out = _URL_RE.sub("[link omitted]", text)
    out = _PHONE_RE.sub("[contact omitted]", out)
    return out
```

Do not strip generic numbers, prices, dates, times, addresses, organization names, or route destinations.

- [ ] **Step 6: Implement deterministic coverage-plan rendering**

```python
def _render_coverage_plan(plan: ArticleCoveragePlan) -> str:
    lines = ["ARTICLE COVERAGE PLAN"]
    for item in plan.stories:
        lines.append(
            f"- {item.prominence} {item.story_id}: {item.topic}"
        )
        lines.append(f"  SUPPORTS: {', '.join(item.support_ids)}")
        if item.detail_support_ids:
            lines.append(
                f"  DETAIL SUPPORTS: {', '.join(item.detail_support_ids)}"
            )
    return "\n".join(lines)
```

- [ ] **Step 7: Render the support corpus without mutating raw support**

Mirror the existing `ArticleEditorialContext.to_prompt_context()` structure, but write:

```python
source_for_writer = sanitize_writer_source_text(sup.source_text)
```

and include `source={source_for_writer}` in the prompt.

Keep all existing fields: temporal role, evidence kind, source roles, framing, observed/effective times, and `fact=`.

- [ ] **Step 8: Run tests**

```bash
pytest tests/publication/test_article_writer_context.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/publication/article_writer_context.py tests/publication/test_article_writer_context.py
git commit -m "feat(article): render coverage-aware sanitized writer context"
```

---

## Task 6: Change the Narrative Contract from Selective to Broad, Hierarchical, Detail-Rich Long Read

**Files:**
- Modify: `src/publication/narrative_contract.py`
- Modify: `tests/publication/test_narrative_contract.py`

**Interfaces:**
- Consumes: existing length profile and single-call writer prompt.
- Produces: `ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v5"`.

- [ ] **Step 1: Add failing contract assertions**

Add to `test_article_narrative_contract_invariants()`:

```python
assert "broad city-life coverage" in contract_lower
assert "prominence controls depth, not inclusion" in contract_lower
assert "do not collapse concrete evidence into generic summaries" in contract_lower
assert "microdetail" in contract_lower
assert "brief stories" in contract_lower
assert "phone numbers" in contract_lower
assert ARTICLE_NARRATIVE_PROMPT_VERSION == "event-article-narrative-v5"
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
pytest tests/publication/test_narrative_contract.py::test_article_narrative_contract_invariants -v
```

Expected: FAIL on the new phrases/version.

- [ ] **Step 3: Bump the prompt version**

```python
ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v5"
```

- [ ] **Step 4: Replace the old selective-product wording with explicit broad-coverage rules**

Add a section equivalent to:

```text
City-Life Coverage & Editorial Hierarchy:
- The product is a broad city-life long read, not a minimal three-story analysis.
- Prominence controls depth, not inclusion.
- DEVELOP stories deserve substantial narrative depth when evidence exists.
- WEAVE stories should be integrated into related sections with compact but concrete treatment.
- BRIEF stories should usually receive at least one factual sentence and may be grouped into a natural city-life section.
- Do not omit a legitimate PUBLISH Story merely because it is smaller than the main themes.
- Do not give all Stories equal space.
```

- [ ] **Step 5: Add microdetail rules**

Add:

```text
Microdetail Preservation:
- Do not collapse concrete evidence into generic summaries when useful supported specifics exist.
- When DETAIL SUPPORTS are provided, use their concrete anchors where they improve reader understanding: neighborhood, amount, interval, resident action, service name, timing, or a short exact quote.
- Prefer "residents pooled 300 units for a shared generator" over "residents are adapting" when the amount and action are supported.
- Prefer one or two strong specifics over a raw inventory of every source sentence.
```

Do not include city-specific names or exact local examples in production prompt code; phrase examples generically.

- [ ] **Step 6: Add anti-directory rules without suppressing real city detail**

Add:

```text
Directory / Promotion Hygiene:
- Do not print phone numbers, booking URLs, handles, or call-to-action copy in the long read.
- Do not turn a service-access Story into an advertisement.
- Organization names, locations, prices, schedules, or addresses may appear when the detail itself is editorially relevant and supported.
```

- [ ] **Step 7: Run narrative contract tests**

```bash
pytest tests/publication/test_narrative_contract.py -q
```

Expected: PASS, including the no-city-specific-leakage test.

- [ ] **Step 8: Commit**

```bash
git add src/publication/narrative_contract.py tests/publication/test_narrative_contract.py
git commit -m "feat(article): define broad hierarchical city-life long-read contract"
```

---

## Task 7: Widen Adaptive Rich-Day Length and Section Targets

**Files:**
- Modify: `src/config_loader.py`
- Modify: `src/publication/article_length.py`
- Modify: `tests/publication/test_article_length.py`
- Modify: existing config-loader tests that assert publication-editorial defaults.

**Interfaces:**
- Consumes: existing support/story counts.
- Produces: wider soft targets and larger default hard ceiling; same `ArticleLengthProfile` interface.

- [ ] **Step 1: Update tests to the new target matrix**

Change expected values to:

```python
# thin
assert profile.target_min_words == 350
assert profile.target_max_words == 800
assert profile.target_min_sections == 2
assert profile.target_max_sections == 4

# standard
assert profile.target_min_words == 700
assert profile.target_max_words == 1400
assert profile.target_min_sections == 3
assert profile.target_max_sections == 6

# rich, with config article_max_words=2200 and article_max_sections=8
assert profile.target_min_words == 1200
assert profile.target_max_words == 2000
assert profile.target_min_sections == 4
assert profile.target_max_sections == 8
```

- [ ] **Step 2: Add a default-config regression**

Assert:

```python
config = PublicationEditorialConfig()
assert config.article_max_words == 2200
assert config.article_max_sections == 8
```

- [ ] **Step 3: Run and verify RED**

```bash
pytest tests/publication/test_article_length.py tests -k "publication_editorial and default" -q
```

Expected: FAIL on old values.

- [ ] **Step 4: Update config defaults**

In `PublicationEditorialConfig`:

```python
article_max_words: int = 2200
article_max_sections: int = 8
```

Keep `article_min_words=800` unchanged for backward-compatible configuration semantics; Event-First hard minimum continues to come from `ArticleLengthProfile.hard_min_words = min(180, config.article_min_words)`.

- [ ] **Step 5: Update length profile targets**

Implement:

```python
if is_thin:
    return ArticleLengthProfile(
        richness="thin",
        target_min_words=350,
        target_max_words=min(800, hard_max),
        target_min_sections=2,
        target_max_sections=min(4, config.article_max_sections),
        hard_min_words=hard_min,
        hard_max_words=hard_max,
    )
elif is_standard:
    return ArticleLengthProfile(
        richness="standard",
        target_min_words=700,
        target_max_words=min(1400, hard_max),
        target_min_sections=3,
        target_max_sections=min(6, config.article_max_sections),
        hard_min_words=hard_min,
        hard_max_words=hard_max,
    )
return ArticleLengthProfile(
    richness="rich",
    target_min_words=1200,
    target_max_words=min(2000, hard_max),
    target_min_sections=4,
    target_max_sections=min(8, config.article_max_sections),
    hard_min_words=hard_min,
    hard_max_words=hard_max,
)
```

- [ ] **Step 6: Run focused tests**

```bash
pytest tests/publication/test_article_length.py -q
```

Expected: PASS.

- [ ] **Step 7: Run config tests**

```bash
pytest tests -k "PublicationEditorialConfig or publication_editorial" -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/config_loader.py src/publication/article_length.py tests/publication/test_article_length.py tests
git commit -m "feat(article): widen rich-day long-read targets"
```

---

## Task 8: Integrate Coverage Planning into the Existing Single-Call Writer

**Files:**
- Modify: `src/article_generator.py`
- Modify: `tests/publication/test_article_fail_closed.py`
- Modify: `tests/integration/test_event_first_narrative_publication.py`

**Interfaces:**
- Consumes:
  - `build_article_coverage_plan(cards, article_ctx)`
  - `render_article_writer_context(article_ctx, coverage_plan)`
- Produces: the same `StructuredArticleDraft` and publication result as before; no schema change.

- [ ] **Step 1: Expose the StoryCard sequence needed by the planner**

The Event-First generation path currently receives only `article_ctx`. Add the minimal immutable card metadata required for coverage planning to `ArticleEditorialContext`:

```python
@dataclass(frozen=True)
class ArticleEditorialContext:
    ...
    story_cards: tuple[StoryCard, ...] = ()
```

In `build_article_editorial_context(...)`, set:

```python
story_cards=tuple(cards),
```

Add/update `tests/publication/test_article_context.py` to assert the tuple preserves frozen card order.

- [ ] **Step 2: Add a failing one-call integration assertion**

In the Event-First article integration test, use the fake provider and assert:

```python
assert fake_provider.chat_call_count == 1
assert "ARTICLE COVERAGE PLAN" in fake_provider.last_user_prompt
assert "DETAIL SUPPORTS:" in fake_provider.last_user_prompt
```

- [ ] **Step 3: Run and verify RED**

```bash
pytest tests/integration/test_event_first_narrative_publication.py -k "coverage_plan" -v
```

Expected: FAIL because the prompt still uses `article_ctx.to_prompt_context()`.

- [ ] **Step 4: Build the plan before the writer call**

In `generate_from_event_article_context()`:

```python
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_writer_context import render_article_writer_context

coverage_plan = build_article_coverage_plan(article_ctx.story_cards, article_ctx)
context_str = render_article_writer_context(article_ctx, coverage_plan)
```

Keep the existing length profile derivation and single provider call.

- [ ] **Step 5: Add explicit writer instruction tying schema output to the plan**

Inside `_build_event_article_system_prompt()` add these generic rules near structure guidance:

```text
- Use the ARTICLE COVERAGE PLAN as a presentation-depth contract.
- DEVELOP / WEAVE / BRIEF are not factual status labels.
- Cover BRIEF stories compactly instead of dropping them solely for being minor.
- Do not mechanically create one section per Story; group related WEAVE/BRIEF stories naturally.
```

Do not add plan fields to the model JSON response.

- [ ] **Step 6: Preserve fail-closed behavior**

Add a test with a draft containing an existing real blocker (for example unsupported location or unsupported concrete number) and assert:

```python
with pytest.raises(ArticlePublicationRejected) as exc_info:
    await generator.generate_from_event_article_context(...)
assert exc_info.value.reason == "validation_failed"
assert fake_provider.chat_call_count == 1
```

No coverage plan behavior may catch or downgrade that exception.

- [ ] **Step 7: Run focused tests**

```bash
pytest tests/publication/test_article_context.py tests/publication/test_article_fail_closed.py tests/integration/test_event_first_narrative_publication.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/article_generator.py src/publication/article_context.py tests/publication/test_article_context.py tests/publication/test_article_fail_closed.py tests/integration/test_event_first_narrative_publication.py
git commit -m "feat(article): drive single-call writer from coverage plan"
```

---

## Task 9: Add Non-Blocking Coverage and Microdetail Diagnostics

**Files:**
- Create: `src/publication/article_coverage_diagnostics.py`
- Create: `tests/publication/test_article_coverage_diagnostics.py`
- Modify: `src/article_generator.py`

**Interfaces:**
- Consumes: `StructuredArticleDraft`, `ArticleCoveragePlan`.
- Produces:
  - `ArticleCoverageDiagnostics`
  - `diagnose_article_coverage(draft, plan)`.

- [ ] **Step 1: Write failing diagnostics tests**

Construct a plan with one DEVELOP, one WEAVE, one BRIEF Story and a draft citing only the DEVELOP and BRIEF support IDs.

Assert:

```python
diag = diagnose_article_coverage(draft, plan)
assert diag.planned_story_count == 3
assert diag.covered_story_count == 2
assert diag.uncovered_story_ids == ("story:telecom",)
assert diag.develop_story_coverage == 1.0
assert diag.weave_story_coverage == 0.0
assert diag.brief_story_coverage == 1.0
```

Add a second case where one of two planned detail supports is cited:

```python
assert diag.planned_detail_support_count == 2
assert diag.covered_detail_support_count == 1
assert diag.detail_support_coverage == 0.5
```

- [ ] **Step 2: Add a contact leak test**

Use article prose containing `+79901234567` and `https://example.com` and assert both are reported in `leaked_contact_payloads`.

- [ ] **Step 3: Run and verify RED**

```bash
pytest tests/publication/test_article_coverage_diagnostics.py -v
```

Expected: FAIL because module does not exist.

- [ ] **Step 4: Implement citation collection**

Create a helper that collects support IDs from:

- `title_support_ids`;
- `lead_support_ids`;
- each `heading_support_ids`;
- each paragraph `cited_support_ids`;
- claim atom support IDs as defense in depth.

Return a deduplicated set.

- [ ] **Step 5: Implement the diagnostics dataclass**

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

Use `0.0` for a prominence ratio only when that prominence exists but none are covered; use `1.0` when no stories of that prominence were planned so the metric does not punish an absent class.

- [ ] **Step 6: Implement story/detail coverage**

A Story is covered when at least one of its `support_ids` appears in the draft citation set.

A planned detail support is covered when its support ID appears in the draft citation set.

This intentionally measures support-level retention rather than trying to perform a second semantic validation pass.

- [ ] **Step 7: Implement contact leakage scanning**

Scan `draft.title`, `draft.lead`, headings, and paragraph prose using the same phone/URL regex family as `article_writer_context.py`.

Share exported compiled helpers or small pure functions rather than duplicating incompatible regexes.

- [ ] **Step 8: Prove diagnostics are non-blocking**

Add a test that validates an evidence-correct draft with `detail_support_coverage < 1.0`:

```python
validation = validate_article_draft(...)
assert validation.is_valid is True

diag = diagnose_article_coverage(draft, plan)
assert diag.detail_support_coverage < 1.0
```

Do not import diagnostics into `article_validator.py`.

- [ ] **Step 9: Record diagnostics only after validation passes**

In `ArticleGenerator.generate_from_event_article_context()`, after `val_res.is_valid` and before `attempt_finished(..., "succeeded")`:

```python
coverage_diag = diagnose_article_coverage(draft, coverage_plan)
```

Add to `success_meta`:

```python
"coverage": {
    "planned_story_count": coverage_diag.planned_story_count,
    "covered_story_count": coverage_diag.covered_story_count,
    "uncovered_story_ids": list(coverage_diag.uncovered_story_ids),
    "develop_story_coverage": coverage_diag.develop_story_coverage,
    "weave_story_coverage": coverage_diag.weave_story_coverage,
    "brief_story_coverage": coverage_diag.brief_story_coverage,
    "planned_detail_support_count": coverage_diag.planned_detail_support_count,
    "covered_detail_support_count": coverage_diag.covered_detail_support_count,
    "uncovered_detail_support_ids": list(coverage_diag.uncovered_detail_support_ids),
    "detail_support_coverage": coverage_diag.detail_support_coverage,
    "leaked_contact_payloads": list(coverage_diag.leaked_contact_payloads),
},
```

- [ ] **Step 10: Run focused tests**

```bash
pytest tests/publication/test_article_coverage_diagnostics.py tests/publication/test_article_fail_closed.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/publication/article_coverage_diagnostics.py src/article_generator.py tests/publication/test_article_coverage_diagnostics.py tests/publication/test_article_fail_closed.py
git commit -m "feat(article): add non-blocking coverage diagnostics"
```

---

## Task 10: Add a Golden Regression for "Broad but Not Generic"

**Files:**
- Modify: `tests/fixtures/article_city_life_coverage_cases.json`
- Modify: `tests/publication/test_article_coverage.py`
- Modify: `tests/integration/test_event_first_narrative_publication.py`

**Interfaces:**
- Consumes: fixture from Task 1, coverage plan, fake one-call writer.
- Produces: a regression that guards against both over-pruning and generic abstraction.

- [ ] **Step 1: Add explicit expected presentation metadata to the fixture**

Extend the fixture:

```json
{
  "expectations": {
    "must_plan_story_ids": [
      "story:power",
      "story:safety",
      "story:telecom",
      "story:sport",
      "story:route"
    ],
    "must_not_plan_story_ids": ["story:question"],
    "must_preserve_detail_support_ids": [
      "story:power:evidence:1:frag:2",
      "story:power:evidence:2:frag:3",
      "story:safety:evidence:1:frag:6"
    ],
    "writer_source_must_not_contain": [
      "+79900292947",
      "https://bus.example"
    ]
  }
}
```

- [ ] **Step 2: Assert broad plan coverage**

```python
assert set(plan.story_ids) == set(expectations["must_plan_story_ids"])
```

This guards against reintroducing a hidden 3–4-story selector.

- [ ] **Step 3: Assert microdetail anchors remain in the prompt**

Render the writer context and assert each `must_preserve_detail_support_id` appears under `DETAIL SUPPORTS:`.

- [ ] **Step 4: Assert directory payload is absent from writer context**

Assert all `writer_source_must_not_contain` values are absent.

- [ ] **Step 5: Add one successful fake-writer integration draft covering DEVELOP + WEAVE + BRIEF**

The fake JSON draft must:

- cite at least one support from every planned Story;
- include multiple power/safety details;
- include the sports-school Story in one compact sentence, not a dedicated oversized section;
- not include phone numbers or URLs;
- remain fully valid under existing Evidence Boundary.

Do not assert exact polished prose. Assert support IDs, section count, and coverage diagnostics.

- [ ] **Step 6: Add one valid-but-generic draft to prove diagnostics catch quality loss without blocking**

Create a draft that safely says only generic things while citing supports. Assert:

```python
validation.is_valid is True
coverage_diag.detail_support_coverage < 1.0
```

This is the intentional guardrail against turning editorial quality into another fail-closed verification gate.

- [ ] **Step 7: Run focused tests**

```bash
pytest tests/publication/test_article_coverage.py tests/integration/test_event_first_narrative_publication.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/fixtures/article_city_life_coverage_cases.json tests/publication/test_article_coverage.py tests/integration/test_event_first_narrative_publication.py
git commit -m "test(article): protect broad coverage and microdetail retention"
```

---

## Task 11: Surface Coverage Quality in Real A/B and Benchmark Scripts

**Files:**
- Modify: `scripts/compare_article_approaches.py`
- Modify: `scripts/benchmark_publication_quality.py`
- Modify: `tests/scripts/test_benchmark_publication_quality.py`

**Interfaces:**
- Consumes: generation-attempt `metadata["coverage"]`.
- Produces: human-readable and aggregate metrics; no production behavior change.

- [ ] **Step 1: Add benchmark metric tests**

Add expectations for:

```python
assert metrics["article_planned_stories"] == 12
assert metrics["article_covered_stories"] == 11
assert metrics["article_story_coverage_rate"] == pytest.approx(11 / 12)
assert metrics["article_detail_support_coverage_rate"] == pytest.approx(0.75)
assert metrics["article_contact_payload_leaks"] == 0
```

Keep existing writer-attempt, rejection, publication, and fallback-content metrics.

- [ ] **Step 2: Run and verify RED**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -v
```

Expected: FAIL because coverage metrics are absent.

- [ ] **Step 3: Extend benchmark aggregation**

Aggregate from successful writer attempt metadata:

```text
article_planned_stories
article_covered_stories
article_story_coverage_rate
article_develop_story_coverage_rate
article_weave_story_coverage_rate
article_brief_story_coverage_rate
article_planned_detail_supports
article_covered_detail_supports
article_detail_support_coverage_rate
article_contact_payload_leaks
```

Never reinterpret a rejected run as zero coverage; report rejected runs separately through existing rejection metrics.

- [ ] **Step 4: Improve `compare_article_approaches.py` output**

After the NEW article, print a compact block:

```text
Coverage diagnostics:
  planned stories: N
  covered stories: M (X%)
  DEVELOP: X%
  WEAVE: X%
  BRIEF: X%
  detail supports: A/B (Y%)
  contact payload leaks: Z
```

If the article is rejected, print `coverage diagnostics: N/A — writer draft rejected` unless diagnostics were explicitly computed before rejection (this plan does not require that).

- [ ] **Step 5: Run script tests**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/compare_article_approaches.py scripts/benchmark_publication_quality.py tests/scripts/test_benchmark_publication_quality.py
git commit -m "feat(article): report long-read coverage quality metrics"
```

---

## Task 12: Supersede the Old Selective-Article Decision and Run Full Verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-30-reader-first-editorial-presentation-design.md`
- Modify: `docs/superpowers/plans/2026-08-30-reader-first-editorial-presentation.md`
- Modify: `docs/superpowers/specs/2026-08-30-city-life-long-read-hierarchy-microdetail-design.md` only if implementation names differ from the approved interfaces.

**Interfaces:**
- Consumes: all Tasks 1–11.
- Produces: consistent documentation and final verification evidence.

- [ ] **Step 1: Add an explicit supersession note to the old Reader-First spec**

At the beginning of section 5.2 add:

```markdown
> **Superseded product decision (2026-08-30):** The selective-small-number-of-central-lines rule is replaced by `2026-08-30-city-life-long-read-hierarchy-microdetail-design.md`. Event-First articles now target broad city-life coverage with deterministic `DEVELOP / WEAVE / BRIEF` prominence. Prominence controls depth, not inclusion. Existing Evidence Boundary and fail-closed rules remain unchanged.
```

Do not delete the historical text; preserve the decision trail.

- [ ] **Step 2: Add an explicit supersession note to the old Reader-First plan**

Near the existing fallback supersession note add:

```markdown
> **Superseded product decision (2026-08-30):** Global constraint 9 and tasks that assume selective article coverage or a preferred rich-day ceiling of 800–1400 words / 3–5 sections are superseded by `2026-08-30-city-life-long-read-hierarchy-microdetail.md`. The article is now a broad city-life long read with prominence-based depth and non-blocking microdetail coverage diagnostics.
```

- [ ] **Step 3: Run pure publication unit tests**

```bash
pytest \
  tests/publication/test_article_context.py \
  tests/publication/test_article_coverage.py \
  tests/publication/test_article_writer_context.py \
  tests/publication/test_article_coverage_diagnostics.py \
  tests/publication/test_article_length.py \
  tests/publication/test_narrative_contract.py \
  tests/publication/test_article_claim_support.py \
  tests/publication/test_article_validator.py \
  tests/publication/test_article_fail_closed.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Run Event-First article integration tests**

```bash
pytest tests/integration/test_event_first_narrative_publication.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the existing Attempt 74 / Evidence Boundary golden tests**

```bash
pytest tests/publication/test_article_evidence_boundary_golden.py -q
```

Expected: PASS. Real unsupported location/proper-name/concrete-claim regressions must still block.

- [ ] **Step 6: Run benchmark script tests**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full test suite appropriate for the local environment**

```bash
pytest -q
```

Expected: PASS, or only pre-existing environment-dependent skips explicitly documented by the repository test configuration. Do not claim completion if new failures remain.

- [ ] **Step 8: Run one clean real-data article comparison after all automated tests pass**

```bash
.venv/bin/python -u scripts/compare_article_approaches.py
```

Acceptance criteria for the NEW article run:

```text
- <= 1 generative writer call
- article either passes the unchanged Evidence Boundary or is rejected for a real existing blocker
- no phone numbers / booking URLs in the published article
- broad coverage includes DEVELOP, WEAVE, and BRIEF stories when present
- major lines receive visibly more space than BRIEF items
- microdetails such as resident actions, supported amounts, route intervals, locations, or short quotes remain visible
- the article is not reduced to generic statements like "residents adapt" / "services continue" when the source corpus contains stronger specifics
- coverage diagnostics are printed
```

- [ ] **Step 9: Commit documentation and final verification-related changes**

```bash
git add \
  docs/superpowers/specs/2026-08-30-reader-first-editorial-presentation-design.md \
  docs/superpowers/plans/2026-08-30-reader-first-editorial-presentation.md \
  docs/superpowers/specs/2026-08-30-city-life-long-read-hierarchy-microdetail-design.md
git commit -m "docs(article): supersede selective coverage with city-life long read"
```

---

## Final Acceptance Checklist

- [ ] Every non-question Story with at least one `PUBLISH` support enters `ArticleCoveragePlan`.
- [ ] No new `ARTICLE_WORTHY` hard gate exists.
- [ ] Prominence is exactly `DEVELOP / WEAVE / BRIEF`.
- [ ] `DEVELOP` can receive up to 3 marked detail supports.
- [ ] `WEAVE` can receive up to 2 marked detail supports.
- [ ] `BRIEF` can receive up to 1 marked detail support.
- [ ] Writer prompt explicitly says prominence controls depth, not inclusion.
- [ ] Writer prompt explicitly protects microdetails from generic abstraction.
- [ ] Writer source text hides phone numbers and URLs without mutating raw evidence.
- [ ] Non-contact numbers, dates, times, amounts, locations, service names, and addresses are not globally stripped.
- [ ] Rich target is `1200–2000 words / 4–8 sections` with default hard max `2200 / 8`.
- [ ] Exactly one Event-First writer call remains.
- [ ] Existing fail-closed blockers remain unchanged.
- [ ] Optional story/detail coverage shortfalls are diagnostics only, never ArticleValidator blockers.
- [ ] Coverage diagnostics include DEVELOP/WEAVE/BRIEF story coverage and detail-support coverage.
- [ ] Contact payload leakage is measured.
- [ ] Existing Attempt 74 factual regressions remain green.
- [ ] Old selective-coverage docs are marked superseded rather than deleted.
