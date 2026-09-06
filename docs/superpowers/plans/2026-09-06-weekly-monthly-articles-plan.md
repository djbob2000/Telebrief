# Weekly and Monthly Long-Read Articles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `weekly_article` (Weekly Long-Read / Итоги недели, 1500–2500 words) and `monthly_article` (Monthly Panorama / Панорама месяца, 3000–4500 words) publications with story-thread clustering, trajectory classification, milestone timelines, anchor publication integration, Telegraph + Telegram teaser delivery, proactive scheduling, and dedicated CLI scripts.

**Architecture:** 
- Ingestion data across 168h / 720h is grouped into multi-day `StoryThread` objects using cluster centroid similarity and shared rubrics/entities.
- Trajectories are classified as `CHRONIC_EVOLVING` (3+ days), `ACUTE_PIVOTAL` (1–2 days), or `BACKGROUND_LOCAL`.
- A milestone timeline connects dated facts and anchor publications (`daily_article`, `digest_grouped`) into 4–7 thematic chapters with editorial weights (`LEAD_THREAD`, `WEAVE_THREAD`, `BRIEF_THREAD`), maintaining the 100% story coverage invariant.
- Synthesis uses Anthropic/Bloomberg long-read formatting, validated against an Evidence Boundary relaxed for weekly/monthly temporal markers, and published as a formatted Telegraph long-read paired with an executive Telegram teaser card.

**Tech Stack:** Python 3.12+, PostgreSQL / pgvector (`psycopg`), `pydantic` / `dataclasses`, `pytest`, `telegraph` API, Telegram Bot API.

---

### Task 1: Configuration & Policies for Longitudinal Articles

**Files:**
- Modify: `src/config/schemas/publication.py`
- Modify: `src/config/schemas/root.py`
- Modify: `src/config/parsers/publication.py`
- Modify: `src/publication/policies.py`
- Modify: `src/jobs/schedules.py`
- Test: `tests/publication/test_longitudinal_policies.py`

- [x] **Step 1: Write failing tests for longitudinal publication policies and config schemas**

```python
# tests/publication/test_longitudinal_policies.py
import pytest
from src.publication.policies import PublicationPolicyService, compute_config_hash
from src.config_loader import load_config
from src.jobs.schedules import (
    WEEKLY_ARTICLE_PUBLICATION_TYPE,
    MONTHLY_ARTICLE_PUBLICATION_TYPE,
    ARTICLE_PUBLICATION_TYPE,
)

@pytest.mark.asyncio
async def test_policy_service_resolves_weekly_article_lookback():
    svc = PublicationPolicyService()
    # Test lookback resolution helper or ensure_current behavior for weekly_article (168h default)
    from src.publication.policies import resolve_publication_lookback_hours
    
    assert resolve_publication_lookback_hours("weekly_article", None) == 168
    assert resolve_publication_lookback_hours("monthly_article", None) == 720
    assert resolve_publication_lookback_hours("daily_article", None) == 24
    assert resolve_publication_lookback_hours("article", None) == 24

def test_article_schedule_config_defaults():
    from src.config.schemas.publication import ArticleScheduleConfig
    cfg = ArticleScheduleConfig()
    assert cfg.enabled is True
    assert cfg.schedule_time == "19:00"
    assert cfg.schedule_day == "sunday"
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_policies.py -v`  
Expected: FAIL with ImportError / AttributeError (symbols not yet defined)

- [x] **Step 3: Implement `ArticleScheduleConfig` and policy lookback resolution**

1. In `src/config/schemas/publication.py`, define:
```python
@dataclass
class ArticleScheduleConfig:
    """Configuration for periodic longitudinal articles (weekly/monthly)."""
    enabled: bool = True
    schedule_day: str | int = "sunday"  # "sunday" or 1
    schedule_time: str = "19:00"
    target_word_count: int = 2000
    lookback_hours: int = 168
```
2. In `src/config/schemas/root.py`:
Add `weekly_article: ArticleScheduleConfig` (default lookback 168, schedule_day "sunday", schedule_time "19:00", target_word_count 2000) and `monthly_article: ArticleScheduleConfig` (default lookback 720, schedule_day 1, schedule_time "20:00", target_word_count 3500) to `Settings`.
3. In `src/config/parsers/publication.py`:
Parse `weekly_article` and `monthly_article` from YAML/dict if present, falling back to defaults.
4. In `src/publication/policies.py`:
Add `resolve_publication_lookback_hours` helper and update `ensure_current` to handle `weekly_article` and `monthly_article`.
5. In `src/jobs/schedules.py`:
Define `WEEKLY_ARTICLE_PUBLICATION_TYPE = "weekly_article"` and `MONTHLY_ARTICLE_PUBLICATION_TYPE = "monthly_article"`.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_policies.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/config/ src/publication/policies.py src/jobs/schedules.py tests/publication/test_longitudinal_policies.py
git commit -m "feat(publication): add weekly and monthly article config schemas and policy resolution"
```

---

### Task 2: Story-Thread Clustering & Trajectory Classification

**Files:**
- Create: `src/publication/story_threads.py`
- Test: `tests/publication/test_story_threads.py`

- [x] **Step 1: Write failing tests for StoryThread clustering, trajectory classification, and timeline extraction**

```python
# tests/publication/test_story_threads.py
import datetime as dt
import pytest
from src.publication.story_threads import (
    StoryThread,
    StoryMilestone,
    TrajectoryKind,
    cluster_stories_into_threads,
    classify_thread_trajectory,
)
from src.editorial_models import StoryCard

def test_trajectory_classification_chronic():
    # Story active across 3 distinct days
    dates = [
        dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 3, 14, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=5)
    assert trajectory == TrajectoryKind.CHRONIC_EVOLVING

def test_trajectory_classification_acute():
    # High burst in 1-2 days
    dates = [
        dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 2, 18, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=6)
    assert trajectory == TrajectoryKind.ACUTE_PIVOTAL

def test_trajectory_classification_background():
    dates = [
        dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=1)
    assert trajectory == TrajectoryKind.BACKGROUND_LOCAL

def test_thread_clustering_by_rubric_and_entity():
    card1 = StoryCard(id="story:1", topic="Водоснабжение в Нагорной части", summary="Перебои с водой", rubric="ЖКХ", source_count=3)
    card2 = StoryCard(id="story:2", topic="Ремонт водовода на Нагорной", summary="Завершение работ водоканала", rubric="ЖКХ", source_count=4)
    card3 = StoryCard(id="story:3", topic="Бесплатные секции в спортшколе", summary="Набор детей", rubric="Спорт", source_count=1)
    
    threads = cluster_stories_into_threads(
        cards=[card1, card2, card3],
        story_dates={"story:1": [dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)],
                     "story:2": [dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)],
                     "story:3": [dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)]}
    )
    assert len(threads) == 2
    water_thread = next(t for t in threads if "Водо" in t.title or t.rubric == "ЖКХ")
    assert set(water_thread.story_ids) == {"story:1", "story:2"}
    assert water_thread.trajectory == TrajectoryKind.CHRONIC_EVOLVING
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_story_threads.py -v`  
Expected: FAIL with ModuleNotFoundError: No module named 'src.publication.story_threads'

- [x] **Step 3: Implement `src/publication/story_threads.py`**

Implement:
1. `TrajectoryKind` enum: `CHRONIC_EVOLVING = "CHRONIC_EVOLVING"`, `ACUTE_PIVOTAL = "ACUTE_PIVOTAL"`, `BACKGROUND_LOCAL = "BACKGROUND_LOCAL"`.
2. `ThreadEditorialWeight` enum: `LEAD_THREAD = "LEAD_THREAD"`, `WEAVE_THREAD = "WEAVE_THREAD"`, `BRIEF_THREAD = "BRIEF_THREAD"`.
3. `StoryMilestone` dataclass: `date: dt.date`, `date_str: str`, `fact: str`, `support_id: str`, `source_type: str`.
4. `StoryThread` dataclass:
   - `id: str` (e.g. `thread:1`)
   - `title: str`
   - `rubric: str`
   - `story_ids: tuple[str, ...]`
   - `trajectory: TrajectoryKind`
   - `weight: ThreadEditorialWeight`
   - `milestones: tuple[StoryMilestone, ...]`
   - `support_ids: tuple[str, ...]`
5. `classify_thread_trajectory(dates: Sequence[dt.datetime], total_observations: int) -> TrajectoryKind`.
6. `cluster_stories_into_threads(...)`:
   - Match by shared rubric and high entity/token overlap (and centroid vector similarity when embeddings are supplied).
   - Build chronological milestones from support items / observations.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_story_threads.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/publication/story_threads.py tests/publication/test_story_threads.py
git commit -m "feat(publication): implement story-thread clustering, trajectory classification, and milestones"
```

---

### Task 3: Anchor Publications Integration & Longitudinal Coverage Plan

**Files:**
- Modify: `src/publication/story_threads.py`
- Modify: `src/publication/article_coverage.py`
- Modify: `src/publication/repository.py`
- Test: `tests/publication/test_story_threads.py`

- [x] **Step 1: Write failing tests for anchor publication querying and Longitudinal Coverage Plan**

```python
# tests/publication/test_story_threads.py (additions)
from src.publication.story_threads import (
    build_longitudinal_coverage_plan,
    AnchorPublicationSummary,
)

def test_build_longitudinal_coverage_plan_thematic_chapters():
    # Create sample threads across different domains
    t1 = StoryThread(
        id="thread:infra",
        title="Кризис водоснабжения и ремонты водовода",
        rubric="ЖКХ",
        story_ids=("story:1", "story:2"),
        trajectory=TrajectoryKind.CHRONIC_EVOLVING,
        weight=ThreadEditorialWeight.LEAD_THREAD,
        milestones=(),
        support_ids=("story:1:ev:1", "story:2:ev:1"),
    )
    t2 = StoryThread(
        id="thread:transit",
        title="Сбои в движении пригородных маршрутов",
        rubric="Транспорт",
        story_ids=("story:3",),
        trajectory=TrajectoryKind.ACUTE_PIVOTAL,
        weight=ThreadEditorialWeight.WEAVE_THREAD,
        milestones=(),
        support_ids=("story:3:ev:1",),
    )
    
    plan = build_longitudinal_coverage_plan(threads=[t1, t2])
    assert len(plan.sections) >= 2
    # Verify 100% story coverage invariant: all story IDs must be in coverage plan
    assert set(plan.story_ids) == {"story:1", "story:2", "story:3"}
    sec_titles = [s.title for s in plan.sections]
    assert any("Инфраструктура" in st or "ЖКХ" in st for st in sec_titles)
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_story_threads.py -k "test_build_longitudinal_coverage_plan" -v`  
Expected: FAIL with ImportError / AttributeError

- [x] **Step 3: Implement Longitudinal Coverage Plan and Anchor Publications queries**

1. In `src/publication/repository.py`:
Add `query_anchor_publications(conn, *, edition_id: int, since: dt.datetime, until: dt.datetime) -> list[Publication]` querying existing daily articles and digests within the period.
2. In `src/publication/story_threads.py`:
Implement `build_longitudinal_coverage_plan(threads: Sequence[StoryThread], anchor_pubs: Sequence[Any] = ()) -> ArticleCoveragePlan`.
- Map threads into the 4–7 thematic chapters specified in Section 3.4:
  - Инфраструктура и жизнеобеспечение
  - Городской транспорт и логистика
  - Потребительский рынок и цены
  - Социальная жизнь и городская среда
- Translate `LEAD_THREAD` -> `DEVELOP`, `WEAVE_THREAD` -> `WEAVE`, `BRIEF_THREAD` -> `BRIEF`.
- Guarantee the Telebrief 100% story coverage invariant: every single constituent `story_id` in each thread is represented in `plan.stories` and `plan.sections`.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_story_threads.py -k "test_build_longitudinal_coverage_plan" -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/publication/story_threads.py src/publication/repository.py tests/publication/test_story_threads.py
git commit -m "feat(publication): implement longitudinal coverage plan and anchor publications lookup"
```

---

### Task 4: Writer Context, Prompts, and Evidence Boundary Adaptation

**Files:**
- Modify: `src/publication/article_writer_context.py`
- Modify: `src/publication/article_validator.py`
- Modify: `src/publication/article_length.py`
- Modify: `src/article_generator.py`
- Test: `tests/publication/test_article_validator.py`
- Test: `tests/publication/test_longitudinal_writer_context.py`

- [x] **Step 1: Write failing tests for longitudinal writer context and validator relaxation**

```python
# tests/publication/test_longitudinal_writer_context.py
import datetime as dt
import pytest
from src.publication.article_models import StructuredArticleDraft, ArticleDraftSection, ArticleDraftParagraph
from src.publication.article_validator import validate_article_draft
from src.publication.article_context import ArticleEditorialContext, ArticlePublicationWindow
from src.publication.article_length import derive_article_length_profile
from src.config_loader import PublicationEditorialConfig

def test_weekly_article_allows_weekly_temporal_markers():
    # In a weekly window (168 hours), title/lead containing "Итоги недели" or "хроника недели" must NOT be rejected
    draft = StructuredArticleDraft(
        title="Итоги недели в Бердянске: ремонт сетей и транспорт",
        lead="За прошедшую неделю город столкнулся с масштабными восстановительными работами.",
        sections=(
            ArticleDraftSection(
                heading="Инфраструктура",
                paragraphs=(ArticleDraftParagraph(text="На улице Нагорной завершился ремонт водовода.", cited_support_ids=("sup:1",)),)
            ),
        ),
        title_support_ids=("sup:1",),
        lead_support_ids=("sup:1",),
    )
    window = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_hours=168,
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    ctx = ArticleEditorialContext(
        story_cards=(),
        evidence_index=(),
        operational_timeline=(),
        support_index=(),
        source_records=(),
        publication_window=window,
        edition_name="Бердянск",
    )
    val = validate_article_draft(draft, ctx)
    # Must NOT have UNSUPPORTED_TEMPORAL_EXPANSION
    violations = [iss.code for iss in val.issues]
    assert "UNSUPPORTED_TEMPORAL_EXPANSION" not in violations

def test_length_profile_scales_for_weekly_and_monthly():
    cfg = PublicationEditorialConfig()
    window_weekly = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_hours=168,
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    ctx_weekly = ArticleEditorialContext(
        story_cards=(),
        evidence_index=(),
        operational_timeline=(),
        support_index=(),
        source_records=(),
        publication_window=window_weekly,
        edition_name="Бердянск",
    )
    prof = derive_article_length_profile(ctx_weekly, cfg)
    assert prof.target_min_words >= 1500
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_writer_context.py -v`  
Expected: FAIL

- [x] **Step 3: Implement validator updates and prompt extensions**

1. In `src/publication/article_validator.py`:
   - Inspect `context.publication_window.lookback_hours`. If `lookback_hours >= 168` (weekly or monthly), skip flagging `_EXPANSION_RE` for weekly terms; if `lookback_hours >= 720` (monthly), skip flagging monthly terms as well.
2. In `src/publication/article_length.py`:
   - When `lookback_hours >= 720` (monthly): target 3000–4500 words, target 4–7 sections.
   - When `lookback_hours >= 168` (weekly): target 1500–2500 words, target 4–6 sections.
3. In `src/publication/article_writer_context.py`:
   - Render trajectory summaries (`CHRONIC_EVOLVING`, `ACUTE_PIVOTAL`), milestone dates (`[01.09]`, `[03.09]`), and instruction for the "Городской горизонт" outlook section.
4. In `src/article_generator.py`:
   - Update prompt builder to support weekly and monthly long-read formats with date anchors and chapter progression.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_writer_context.py tests/publication/test_article_validator.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/publication/article_validator.py src/publication/article_length.py src/publication/article_writer_context.py src/article_generator.py tests/publication/test_longitudinal_writer_context.py
git commit -m "feat(publication): adapt evidence boundary, length profile, and writer prompts for weekly/monthly articles"
```

---

### Task 5: Event-First Integration & Generation Pipeline

**Files:**
- Modify: `src/publication/generation.py`
- Modify: `src/publication/event_editorial_adapter.py`
- Modify: `src/publication/selection.py`
- Modify: `src/publication/selection_ai.py`
- Test: `tests/publication/test_longitudinal_generation.py`

- [x] **Step 1: Write failing test for weekly/monthly article run generation flow**

```python
# tests/publication/test_longitudinal_generation.py
import pytest
from src.publication.policies import resolve_publication_lookback_hours

def test_article_publication_types_inclusion():
    from src.publication.generation import ARTICLE_PUBLICATION_TYPES
    assert "weekly_article" in ARTICLE_PUBLICATION_TYPES
    assert "monthly_article" in ARTICLE_PUBLICATION_TYPES
    assert "daily_article" in ARTICLE_PUBLICATION_TYPES
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_generation.py -v`  
Expected: FAIL

- [x] **Step 3: Update generation, selection, and adapter services**

1. In `src/publication/generation.py`:
   - Define `ARTICLE_PUBLICATION_TYPES = frozenset({"daily_article", "article", "weekly_article", "monthly_article"})`.
   - Update lines checking `run.publication_type in ("daily_article", "article")` to check `run.publication_type in ARTICLE_PUBLICATION_TYPES`.
2. In `src/publication/event_editorial_adapter.py`:
   - Update check for `run.publication_type in ARTICLE_PUBLICATION_TYPES`.
   - When building `ArticleEditorialContext`, if `run.publication_type in ("weekly_article", "monthly_article")`, invoke `cluster_stories_into_threads` and `build_longitudinal_coverage_plan`.
3. In `src/publication/selection.py` & `src/publication/selection_ai.py`:
   - Ensure `run.publication_type in ARTICLE_PUBLICATION_TYPES` preserves all candidates (`coverage_preserving = True`).

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_generation.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/publication/generation.py src/publication/event_editorial_adapter.py src/publication/selection.py src/publication/selection_ai.py tests/publication/test_longitudinal_generation.py
git commit -m "feat(publication): wire weekly_article and monthly_article through event adapter and generation pipeline"
```

---

### Task 6: Telegraph Full Article & Telegram Teaser Card Delivery

**Files:**
- Modify: `src/publication/delivery.py`
- Modify: `src/publication/renderers.py`
- Test: `tests/publication/test_longitudinal_delivery.py`

- [x] **Step 1: Write failing test for longitudinal Telegram teaser card and Telegraph payload**

```python
# tests/publication/test_longitudinal_delivery.py
import pytest
from src.publication.renderers import render_longitudinal_telegram_teaser

def test_render_longitudinal_telegram_teaser():
    title = "Бердянск: главное за неделю с 31 августа по 6 сентября"
    lead = "Неделя прошла под знаком масштабных ремонтов водовода и подготовки школ."
    body = (
        "## Инфраструктура и жизнеобеспечение\n\n"
        "Главным событием недели стали ремонты на Нагорной части.\n\n"
        "## Городской транспорт\n\n"
        "Маршрут №4 вернулся к обычному графику движения.\n\n"
        "## Городской горизонт\n\n"
        "К концу недели основные аварии ликвидированы."
    )
    telegraph_url = "https://telegra.ph/Berdyansk-Weekly-09-06"
    
    teaser = render_longitudinal_telegram_teaser(
        publication_type="weekly_article",
        title=title,
        lead=lead,
        body=body,
        telegraph_url=telegraph_url,
    )
    assert "📰 <b>ИТОГИ НЕДЕЛИ:" in teaser or "📰 ИТОГИ НЕДЕЛИ" in teaser
    assert "Инфраструктура и жизнеобеспечение" in teaser
    assert "Городской транспорт" in teaser
    assert telegraph_url in teaser
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_delivery.py -v`  
Expected: FAIL with ImportError

- [x] **Step 3: Implement teaser rendering and delivery payload formatting**

1. In `src/publication/renderers.py`:
   - Implement `render_longitudinal_telegram_teaser(...)`:
     - Header: `📰 <b>ИТОГИ НЕДЕЛИ: <TITLE></b>` for `weekly_article`, `🏛 <b>ПАНОРАМА МЕСЯЦА: <TITLE></b>` for `monthly_article`.
     - Executive lead paragraph.
     - Extracted chapter previews (1st sentence or hook of each `## Chapter`).
     - Telegraph read link button / formatted hyperlink: `👉 <a href="{telegraph_url}">Читать полный лонгрид на Telegraph (~7 мин)</a>`.
2. In `src/publication/delivery.py`:
   - For `weekly_article` and `monthly_article`, support dual-destination delivery:
     - Destination 1: `telegraph` (full markdown body).
     - Destination 2: `telegram_channel` (teaser card linking to the Telegraph URL).

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_longitudinal_delivery.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/publication/renderers.py src/publication/delivery.py tests/publication/test_longitudinal_delivery.py
git commit -m "feat(delivery): add Telegraph longform formatting and Telegram teaser card renderer"
```

---

### Task 7: Procrastinate Scheduling Dispatcher & CLI Scripts

**Files:**
- Modify: `src/jobs/schedules.py`
- Create: `scripts/generate_weekly_article.py`
- Create: `scripts/generate_monthly_article.py`
- Test: `tests/jobs/test_longitudinal_schedules.py`

- [x] **Step 1: Write failing test for Procrastinate weekly and monthly due actions**

```python
# tests/jobs/test_longitudinal_schedules.py
import datetime as dt
from zoneinfo import ZoneInfo
import pytest
from src.config_loader import load_config
from src.jobs.schedules import due_publication_actions, WEEKLY_ARTICLE_PUBLICATION_TYPE, MONTHLY_ARTICLE_PUBLICATION_TYPE

def test_due_weekly_article_on_sunday_evening():
    config = load_config()
    # 2026-09-06 was a Sunday
    tz = ZoneInfo(config.settings.timezone)
    sunday_1900 = dt.datetime(2026, 9, 6, 19, 0, tzinfo=tz)
    
    actions = due_publication_actions(config, sunday_1900)
    weekly_acts = [a for a in actions if a.task_kwargs.get("publication_type") == WEEKLY_ARTICLE_PUBLICATION_TYPE]
    assert len(weekly_acts) >= 1

def test_due_monthly_article_on_first_day():
    config = load_config()
    tz = ZoneInfo(config.settings.timezone)
    first_1000 = dt.datetime(2026, 9, 1, 10, 0, tzinfo=tz)
    
    actions = due_publication_actions(config, first_1000)
    monthly_acts = [a for a in actions if a.task_kwargs.get("publication_type") == MONTHLY_ARTICLE_PUBLICATION_TYPE]
    assert len(monthly_acts) >= 1
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/jobs/test_longitudinal_schedules.py -v`  
Expected: FAIL

- [x] **Step 3: Implement schedule dispatcher logic and CLI scripts**

1. In `src/jobs/schedules.py`:
   - Update `_publication_types(config)` to evaluate:
     - `weekly_article`: matches `schedule_day == "sunday"` (weekday 6) and `schedule_time == "19:00"`.
     - `monthly_article`: matches `schedule_day == 1` (day 1 of month) and `schedule_time == "20:00"`.
2. Create `scripts/generate_weekly_article.py`:
   - Accepts `--edition <slug>`, `--date <YYYY-MM-DD>`, `--deliver`, `--dry-run`.
   - Runs `PublicationSnapshotService` with `publication_type="weekly_article"`, seals candidates, executes `EditorialSelectionService`, generates via `PublicationGenerationService`.
   - Pretty-prints coverage diagnostics, chapters, full text, and handles Telegraph/Telegram delivery when `--deliver` is supplied.
3. Create `scripts/generate_monthly_article.py`:
   - Accepts `--edition <slug>`, `--month <YYYY-MM>`, `--deliver`, `--dry-run`.
   - Runs with `publication_type="monthly_article"`.

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/jobs/test_longitudinal_schedules.py -v`  
Expected: PASS

- [x] **Step 5: Commit changes**

```bash
git add src/jobs/schedules.py scripts/generate_weekly_article.py scripts/generate_monthly_article.py tests/jobs/test_longitudinal_schedules.py
git commit -m "feat(schedules): add weekly and monthly article schedule triggers and CLI scripts"
```

---

### Task 8: End-to-End Verification & Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-09-06-weekly-monthly-articles-design.md`
- Run: full test suite for publication and schedules
- Run: dry-run CLI test against PostgreSQL

- [x] **Step 1: Run full publication test suite**

Run: `.venv/bin/pytest tests/publication/ tests/jobs/ -v`  
Expected: All tests pass

- [x] **Step 2: Test CLI in dry-run mode**

Run: `.venv/bin/python scripts/generate_weekly_article.py --help`  
Run: `.venv/bin/python scripts/generate_monthly_article.py --help`  
Expected: Clean help text with available options

- [x] **Step 3: Update documentation status and final commit**

```bash
git add docs/superpowers/
git commit -m "docs: complete weekly and monthly long-read articles implementation and verification"
```
