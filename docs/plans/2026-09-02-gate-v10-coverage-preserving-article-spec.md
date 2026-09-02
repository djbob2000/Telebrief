# Corrective Spec: Gate v10, Fail-Closed Eligibility, and Coverage-Preserving Article Long-Read

**Version:** 1.0  
**Date:** 2026-09-02  
**Status:** Approved  
**Supersedes:** `docs/plans/2026-09-01-digest-legacy-parity-corrective-spec.md` (for article synthesis)

---

## 1. Context and Problem Statement

Recent runs revealed that while the Event-First candidate denominator was successfully cleaned (dropping hundreds of spam items to ~37 genuine local stories), the article reader layer suffered from critical defects:
1. **AI Writer Fail-Closed Validation Failure:** The AI writer draft was rejected with 57 violations—primarily 14 `UNSUPPORTED_DIRECT_QUOTE` violations where the model placed quotation marks around paraphrased community chatter, violating Section 0.6 of `AGENTS.md`.
2. **Deterministic Fallback Degradation:** Per Section 0.7 of `AGENTS.md`, writer rejection correctly triggered deterministic fallback, but the fallback functioned as an unformatted provenance serializer rather than a city-life long read:
   - Leaked internal data structures into reader prose: `[Электроснабжение] Бердянск UNAVAILABLE — вообще нигде нет электроснабжения...`
   - Suffered verbatim repetition: `"По сообщениям жителей, "` repeated 26 times, identical service lists printed multiple times.
   - Built an inventory layout (`1 Story = 1 Paragraph`, 17 standalone sections) rather than theme-first sections.
   - Parroted a raw chat question (*«Ровно месяц без света, и долго это будет?»*) as headline, lead, and section 1.
3. **Authority Gap in Candidate Universe:** 1,824 active stories in the database lacked authoritative triage decisions, silently omitting genuine news (such as the 8 ₽ transport discount).
4. **Mixed Question / Outage Clause Loss:** Gate v9 classified mixed messages (*«Воду дали на Обжоре? У нас нет третий день и порыв на перекрестке»*) as pure `community_report` without `service_state`, and `normalize_question_evidence()` dropped valid operational observations if their fragment was shared with a resident question.

---

## 2. Architecture and Invariant Contracts

### 2.1 Gate v10: Evidence-Aware Normalization & Mixed Question/Outage Decomposition
- `TRIAGE_VERSION = "v10"`.
- Gate postprocessing enforces deterministic extraction: if a source fragment contains both a resident question and a concrete factual service outage clause (narrow predicates: *«нет»*, *«немає»*, *«не работает»*, *«N-й день без...»*):
  - Question clause $\to$ `resident_question / CONTEXT`
  - Factual outage clause $\to$ `service_access / PUBLISH` with `service_state` (`UNAVAILABLE`, `expected_now=True`).
- `normalize_question_evidence()` is **evidence-aware**: when a fragment contains both `resident_question / CONTEXT` and `service_access / PUBLISH`, the factual observation is strictly preserved.
- If deterministic extraction cannot establish confidence, the cluster is deferred, never silently manufactured.

### 2.2 Fail-Closed Frozen Eligibility & Single Universe Query
- Eligibility policy requires non-null `triage_version == "v10"`, non-null `scope_version == "v1"`, and non-empty `scope_config_hash`.
- The repository enforces eligibility via a single canonical `candidate_universe` CTE sharing identical temporal and activity predicates:
  - Consumer A: applies authoritative Gate predicates (`setd.triage_version = 'v10'`, `setd.retention = 'KEEP'`, `sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')`) to yield sealed candidate inputs.
  - Consumer B (`authority_gap`): queries rows with `knowledge_source = 'event_first'` in that exact same universe missing authoritative v10 triage decisions.
- Candidate sealing fails closed (`IncompleteTriageError`) if `authority_gap > 0`.

### 2.3 Selection Overlay Only: Zero Omissions of Sealed Candidates
- Selection AI provides editorial hierarchy overlay (`lead`, `normal`, `brief`, `unverified_operational`, `follow_up`) and priority rank.
- Selection AI **never** reduces the sealed candidate denominator for coverage-preserving publications:
  - `article` / `daily_article` $\to$ selector `OMIT` overrides to `INCLUDE + brief`.
  - `DIGEST_PUBLICATION_TYPES` (`digest`, `digest_grouped`, `digest_channel`) $\to$ selector `OMIT` overrides to `INCLUDE + normal`.
- Any selector hard exclusion attempt is recorded as diagnostic metadata (`disagreement_with_gate`).
- Coverage plan invariant: `ArticleCoveragePlan.story_ids == sealed_candidate_ids` (100% coverage).

### 2.4 Selection-Driven Prominence Hierarchy
- `ArticleSelectionSignal(story_id: str, intent: str, rank: int)` is carried into `ArticleEditorialContext`.
- Depth hierarchy:
  - First `lead` by rank $\to$ `DEVELOP`.
  - Subsequent `lead` $\to$ normalized to `normal`.
  - `normal` $\to$ default `WEAVE`. Top-ranked `normal` stories are elevated to `DEVELOP` within explicit `develop_story_budget = max(0, min(3, length_profile.target_max_sections - 1))`.
  - `follow_up` $\to$ `WEAVE`.
  - `unverified_operational` $\to$ `BRIEF`.
  - `brief` / missing $\to$ `BRIEF`.
- `urgency` is completely decoupled from prominence.

### 2.5 Reader-Safe Operational Support Text (`render_operational_state_fact`)
- In `article_context.py`, `ArticleSupport.text` for operational observations is immediately reader-safe prose via exhaustive mapping:
  - `UNAVAILABLE` $\to$ `"отсутствует"` / `"нет"`.
  - `AVAILABLE` $\to$ `"доступно"` / `"работает"`.
  - `DEGRADED` $\to$ `"работает с ограничениями"`.
  - `RESTRICTED` $\to$ `"ограничено"`.
  - `SCHEDULED` $\to$ `"запланировано: {obs.detail}"`.
  - `UNKNOWN` $\to$ `obs.detail`.
  - Fail-closed on unknown state enum.
- Zero raw enums (`AVAILABLE`, `UNAVAILABLE`) and zero bracketed tags (`[...]`) in `ArticleSupport.text`.
- `ArticleSupport.source_text` remains raw source text for provenance and exact quote validation.

### 2.6 Quote Allowlist & Hardened Writer Contract
- Quotation marks `«...»` / `"..."` are strictly forbidden unless quoting an exact verbatim substring from an explicit `Quote Allowlist` containing only verified source excerpts.
- Paraphrases must use indirect speech.
- Writer prompt rules for claim atoms require exact normalized propositions without added adjectives or speculative numbers.

### 2.7 Theme-First Fallback Recovery with Deduplication
- `resolve_article_theme(card: StoryCard, supports: Sequence[ArticleSupport]) -> ArticleTheme` groups stories into **theme-first sections**:
  1. explicit stable category / rubric
  2. known service family / canonical tags
  3. transport
  4. communications
  5. municipal/social services
  6. city_life fallback
- Sections $\le \text{article\_max\_sections}$, ordered by minimum selection rank.
- Deduplication: conservative exact normalization (Unicode, casefold, whitespace, punctuation, stripping canonical attribution).
- When duplicate sentences are merged, **union their `support_ids`** on the single claim atom, maintaining 100% story coverage diagnostics.
- Title and lead synthesized support-closed from top 2–3 thematic axes by selection rank/prominence; validated under `ArticleValidator`.

---

## 3. Verification & Acceptance Criteria

1. **Zero Authority Gap:** `in_window_latest_assignments_missing_v10 == 0`.
2. **Lineage Preservation:** The 8 ₽ transport discount (`source_item_revision_id` 2083/2173) is triaged under v10 and included if in-window.
3. **Zero Selector Omissions:** `ArticleCoveragePlan.story_ids == sealed_candidate_ids`.
4. **Reader Quality Invariants:**
   - 0 leaks of `[...] AVAILABLE/UNAVAILABLE` in published markdown.
   - 0 consecutive duplicate sentences.
   - No parroted single chat questions as headline/lead.
5. **Validator & Evidence Boundary:**
   - AI draft: `UNSUPPORTED_DIRECT_QUOTE == 0`.
   - Fallback draft: passes `validate_article_draft` with 0 blocking violations.
6. **Regression Floors:** Full test suite passes (`0 failed`), `legacy_floor_coverage == 1.0`, `legacy_microdetail_retention == 1.0`.
