# Design Specification: Weekly and Monthly Long-Read Articles (`weekly_article` & `monthly_article`)

**Date:** 2026-09-06  
**Status:** Implemented & Verified  
**Target:** Telebrief Publication & Knowledge Engine  

---

## 1. Context & Motivation

Telebrief continuously ingests and processes multi-source community and official content into PostgreSQL. Over days and weeks, the database accumulates:
- Atomic stories and their cluster trajectories (`stories`, `story_cluster_state`, `story_revisions`);
- Rich event analysis payloads (`event_payload` with epistemic facts, service states, community reports, actors);
- Immutable daily publications (`publications`: daily digests and daily articles) with verified facts and candidate provenance;
- Historical news archives and vector embeddings (`archive_articles`).

Currently, articles only operate within a 24-hour lookback window (`lookback_hours = 24`). 

The goal of this design is to utilize the stored historical data to introduce two new publication experiences:
1. **`weekly_article`** (Weekly Long-Read / Итоги недели): A 1,500–2,500 word narrative retrospective synthesizing the city's week.
2. **`monthly_article`** (Monthly Panorama / Панорама месяца): A 3,000–4,500 word comprehensive chronicle synthesizing the city's month.

### Best Practices Incorporated:
- **Bloomberg News Tickers / Story-Thread Arcs:** Moving beyond flat summaries-of-summaries (which cause abstraction drift and loss of lived experience). Instead, grouping multi-day developments into **Story Threads** that track emergence, peak escalation, official response, and resolution or current status.
- **Anthropic Long-Horizon Synthesis:** Preventing context overflow and "lost-in-the-middle" hallucinations through a two-stage hierarchical model:
  - Stage 1: Deterministic temporal clustering and trajectory classification over the database.
  - Stage 2: Structured chapter-level generation anchored to exact dated milestones (`[01.09]`, `[03.09]`, `[06.09]`).
- **Telebrief Product Contract (AGENTS.md Section 0):** Preservation of concrete microdetails (streets, transit routes, numbers, schedules) and authentic community quotes under a strict fail-closed Evidence Boundary.

---

## 2. Architecture & Data Flow

```
PostgreSQL Database
  ├── stories & story_cluster_state (lookback: 168h / 720h)
  ├── story_revisions (event_payload: facts, quotes, service states)
  └── publications (anchor daily articles & digests for the period)
                          │
                          ▼
            [Story-Thread Clustering Engine]
         (src/publication/story_threads.py)
                          │
        ┌─────────────────┴─────────────────┐
        ▼                                   ▼
Trajectory Classification             Milestone Timeline
 - CHRONIC_EVOLVING (3+ days)          - Date-tagged facts
 - ACUTE_PIVOTAL (1-2 days)            - Verified numbers
 - BACKGROUND_LOCAL (civic life)       - Quote allowlist
        └─────────────────┬─────────────────┘
                          │
                          ▼
             [Longitudinal Coverage Plan]
   (Thematic chapters: Infrastructure, Transit, Markets, Civic Life)
                          │
                          ▼
           [Long-Read Synthesis Writer]
     (Anthropic / Bloomberg structured longform generation)
                          │
                          ▼
          [Fail-Closed Evidence Boundary]
       (Validate timeline dates, quotes, claims)
        ├── Pass -> Persist to PostgreSQL `publications`
        └── Minor flaws -> Targeted editing via ArticleEditor
                          │
                          ▼
               [Delivery Orchestrator]
       ├── Telegraph: full beautifully-formatted long-read
       └── Telegram: executive teaser card + link button
```

---

## 3. Detailed Component Design

### 3.1 Publication Types and Policy Configuration
- Add `weekly_article` and `monthly_article` to supported publication types in `src/publication/policies.py` and `src/jobs/schedules.py`.
- Lookback configuration:
  - `weekly_article`: `lookback_hours = 168` (7 days);
  - `monthly_article`: `lookback_hours = 720` (30 days).
- Configuration settings (`Config.settings.weekly_article` and `Config.settings.monthly_article`):
  ```python
  weekly_article = ArticleScheduleConfig(
      enabled=True,
      schedule_day="sunday",
      schedule_time="19:00",
      target_word_count=2000,
  )
  monthly_article = ArticleScheduleConfig(
      enabled=True,
      schedule_day=1,  # 1st day of month
      schedule_time="20:00",
      target_word_count=3500,
  )
  ```

### 3.2 Story-Thread Clustering (`src/publication/story_threads.py`)
1. **Multi-Day Story Thread Grouping:**
   - Queries all `KEEP` and `LOCAL` stories active in `[snapshot_at - lookback, snapshot_at]`.
   - Links daily stories that belong to the same continuing narrative based on:
     - Vector similarity between cluster centroids (`join_similarity >= 0.70`);
     - Shared taxonomy rubric and primary named entities (e.g. "Водоснабжение", "Бердянский порт", "Маршрут №4");
     - Temporal overlap/continuity.
2. **Trajectory Classification:**
   - `CHRONIC_EVOLVING`: Active across 3 or more distinct days in the window (e.g. utility crisis, major municipal works).
   - `ACUTE_PIVOTAL`: Burst of high volume in 1–2 days (e.g. emergency event, key policy change).
   - `BACKGROUND_LOCAL`: Everyday urban fabric (community mutual aid, seasonal markets, school openings).
3. **Milestone Timeline Building:**
   - For each thread, extracts key events with their exact dates (`published_at` / `first_seen_at`), forming a chronological series of dated anchor events.

### 3.3 Anchor Publications Integration
- Queries existing records from `publications` where `created_at` is within the reporting window and `publication_type in ('daily_article', 'article', 'digest_grouped', 'digest')`.
- Cross-references thread milestones against already-published daily coverage to ensure editorial continuity and prevent narrative contradictions across publication tiers.

### 3.4 Longitudinal Coverage Plan & Thematic Chapters
- Groups the selected `StoryThread` instances into **4–7 thematic chapters**:
  - Chapter 1: *Инфраструктура и жизнеобеспечение* (utilities, water, electricity, heating);
  - Chapter 2: *Городской транспорт и логистика* (roads, bus lines, intercity connection);
  - Chapter 3: *Потребительский рынок и цены* (food prices, market supplies, cash/banking services);
  - Chapter 4: *Социальная жизнь и городская среда* (medicine, schools, recreation, seasonal changes).
- Inside each chapter, threads are assigned editorial weights:
  - `LEAD_THREAD`: Receives multi-paragraph treatment following its timeline arc;
  - `WEAVE_THREAD`: Integrated contextually into the chapter narrative;
  - `BRIEF_THREAD`: Concise milestone mention (date + concrete fact).
- **Zero-loss invariant:** No legitimate local story is dropped. Minor stories enrich the factual texture.

### 3.5 Synthesis & Writer Context (`src/publication/article_writer_context.py`)
The writer prompt is structured for multi-chapter synthesis:
- **Title & Executive Lead:** Captures the core pulse and main tension of the week/month.
- **Thematic Chapters (`## Название главы`):**
  - Narrative progression through time ("В начале недели... к среде... к выходным ситуация изменилась...");
  - Concrete microdetails preserved (streets, bus numbers, prices);
  - 1–2 authentic community quotes per chapter from the allowlist with natural conversational attributions.
- **Horizon & Outlook (`## Городской горизонт`):**
  - Summary of the current state at window closing;
  - Unresolved issues and upcoming scheduled events.

### 3.6 Evidence Boundary & Validation
- Reuses and extends `ArticleValidator` (`src/publication/article_validator.py`):
  - **Date Grounding:** Any mentioned calendar date or weekday must match the timeline evidence for that story thread.
  - **Quote Allowlist:** Direct quotes must be exact substrings of authentic community reports from the database.
  - **Causal & Scope Grounding:** Prohibits invented causes or upgrading rumors into verified municipal statements.
- **Fail-Closed Semantics:**
  - If isolated paragraph-level issues occur, `ArticleEditor` performs targeted copy-editing.
  - If unrecoverable violations remain, the run fails safely (`ArticlePublicationRejected`) rather than publishing hallucinations.

### 3.7 Delivery & Storage
- **PostgreSQL Persistence (Source of Truth):**
  - Saved to `publications` and `publication_runs` tables with full JSON metadata (word count, thread IDs, candidate provenance).
  - **No ephemeral root markdown files:** Suitable for containerized Docker environments where ephemeral disk state is discarded on recreate.
- **Telegraph Publishing:**
  - Full long-read article published to Telegraph using `TelegraphClient` with title, subtitle, formatted headings, and blockquotes.
  - Public Telegraph URL obtained and recorded in `publication.metadata["telegraph_url"]`.
- **Telegram Channel Teaser:**
  - Compact overview message formatted in Telegram HTML:
    - Bold header: `📰 ИТОГИ НЕДЕЛИ: <TITLE>` or `🏛 ПАНОРАМА МЕСЯЦА: <TITLE>`;
    - Executive lead paragraph;
    - Bulleted chapter preview (1 hook sentence per chapter);
    - Inline button or link: `👉 Читать полный лонгрид на Telegraph (время чтения ~7 мин)`.

### 3.8 Scheduling & CLI
- **Procrastinate Scheduling (`src/jobs/schedules.py`):**
  - Evaluates `weekly_article` due conditions (Sunday 19:00 local time);
  - Evaluates `monthly_article` due conditions (1st of month 20:00 local time).
- **CLI Commands:**
  - `scripts/generate_weekly_article.py`:
    - Flags: `--edition <slug>`, `--date <YYYY-MM-DD>`, `--deliver`
  - `scripts/generate_monthly_article.py`:
    - Flags: `--edition <slug>`, `--month <YYYY-MM>`, `--deliver`
  - Both scripts print the full text to stdout, display coverage diagnostics, and if `--deliver` is set, push to Telegraph and Telegram.

---

## 4. Testing & Verification Plan

1. **Unit Tests:**
   - `tests/publication/test_story_threads.py`: Test grouping of multi-day stories into threads, trajectory classification (`CHRONIC_EVOLVING`, `ACUTE_PIVOTAL`), and timeline sorting.
   - `tests/publication/test_longitudinal_policies.py`: Verify policy resolution for `weekly_article` (168h lookback) and `monthly_article` (720h lookback).
   - `tests/publication/test_longitudinal_delivery.py`: Verify Telegraph longform rendering and Telegram teaser card generation.
2. **Integration & CLI Verification:**
   - Run `scripts/generate_weekly_article.py` over current database data in `--dry-run` mode to verify candidate sealing, thread clustering, LLM writer synthesis, and Evidence Boundary validation.
