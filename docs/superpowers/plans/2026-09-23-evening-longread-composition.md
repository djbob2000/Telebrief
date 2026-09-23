# Evening Long-Read Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Event-First evening articles read as coherent, locally grounded long reads while keeping eligible city-life material and every published claim tied to evidence.

**Architecture:** Keep `ArticleCoveragePlan` as the complete candidate/depth map, then build a relation-aware composition map and render one compact roadmap beside each Story's evidence packet once. Deterministic reader-quality checks identify serious composition defects; the existing bounded `ArticleEditor` repairs specific units and the finalizer rejects unresolved serious issues. Add a no-delivery replay path for an existing frozen article run.

**Tech Stack:** Python, dataclasses, existing Event-First publication models and repositories, `ArticleGenerator`, `ArticleEditor`, pytest, existing edition geography profiles.

**Spec:** `docs/superpowers/specs/2026-09-23-evening-longread-composition-design.md`

## Global Constraints

- Change the Event-First city-life article path only; digest generation, ingestion, Gate, clustering, selection eligibility, scheduled publication, and delivery remain unchanged.
- Keep every hard-eligible Story and preserve `DEVELOP`, `WEAVE`, and `BRIEF` as editorial depth, never as an eligibility gate.
- Do not require multiple sources or official confirmation; retain useful short, conversational, single-source community reports with honest attribution.
- Do not invent or upgrade facts, causes, geography, chronology, completion states, quotes, service states, or city-wide scope; keep the Evidence Boundary strict.
- Use grouping only for presentation and retain each Story's support IDs, provenance, epistemic status, location, time, uncertainty, and service-state evidence.
- Remove repetition and directory payload conservatively while retaining useful local facts, prices, actions, intervals, and consequences.
- Preserve one main writer call and the existing bounded `ArticleEditor`; add no routine LLM planner/reviewer call, per-message LLM calls, `ARTICLE_WORTHY` gate, raw-fragment dump, filler, or deterministic article fallback.
- An unresolved serious composition finding after bounded editing fails closed as `ArticlePublicationRejected`; optional detail omissions and non-actionable warnings do not block publication.
- Generic logic must use edition data and profiles; do not hardcode Berdyansk streets, places, source names, or run-specific prose in generic prompts or checks.
- Replay must use frozen inputs and must not create a run/publication, change run state, select inputs, deliver, or write generation-attempt rows.
- Preserve the user's direct-to-`dev` workflow: implement, review, and commit each task on `dev`; do not create a feature branch or worktree.

## Review Focus

- Same broad service, different status/place/time: keep reports distinct or express a supported localized/temporal contrast; test in Task 1.
- Recognized street aliases versus genuinely separate neighborhoods: merge aliases for relation checks but never infer proximity between distinct places; test in Task 1.
- Unknown event time with a known report time: do not turn `observed_at` into the start time or event chronology; test in Task 2.
- A long paragraph listing many addresses versus a short, legitimate contrast paragraph: flag the inventory and allow the contrast; test in Task 3.
- Replay requested for a missing run or a digest run: fail before writer generation and leave persisted rows unchanged; test in Task 5.

---

## File Map

- `src/publication/article_coverage.py` — coverage ranking/depth and thematic assignment; remove synthetic section spreading, catch-all placement, and support pooling while keeping all coverage Stories.
- `src/publication/article_composition.py` — replace broad keyword-only bundles with narrative lines and relation-aware evidence groups; enforce exactly-once visible Story membership.
- `src/publication/article_writer_context.py` — materialize the concise roadmap and one set of writer-facing Story/support packets, preserving edition-local time context and material projection.
- `src/article_generator.py` and `src/publication/narrative_contract.py` — remove the duplicate plan roster, refine long-read instructions, trigger targeted quality edits, and keep prompt/context diagnostics.
- `src/publication/article_quality.py` — deterministic, evidence-aware article-level diagnostics for roster paragraphs, cross-section repetition, headings, lead/body progression, and existing quality findings.
- `src/publication/article_editor.py` — provide adjacent prose/headings to targeted patches and map each finding code to a narrow editing instruction without broadening its evidence.
- `src/publication/article_finalization.py` — retain strict factual validation, evaluate reader quality on the final assembled article, and reject unresolved blocking composition defects with findings and patch history.
- `src/publication/article_preview.py` (new) — load one existing frozen article run read-only and generate a local preview with an in-memory attempt observer.
- `scripts/preview_article.py` — add `--run-id` and optional diagnostics output without changing the existing new-snapshot preview defaults.
- `tests/publication/test_article_coverage.py`, `test_article_composition.py`, `test_article_writer_context.py`, `test_article_writer_prompt.py`, `test_article_quality.py`, `test_article_editor.py`, `test_article_finalization.py`, `test_facade.py`, and new `test_preview_article.py` — pin the above behavior using synthetic generic fixtures and repository mocks.

## Interfaces Shared Between Tasks

Task 1 produces the composition API consumed by Task 2:

```python
ArticleCompositionRelation = Literal[
    "shared_condition",
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "independent",
]

@dataclass(frozen=True)
class ArticleCompositionMember:
    story_id: str
    prominence: ArticleProminence
    support_ids: tuple[str, ...]

@dataclass(frozen=True)
class ArticleCompositionGroup:
    group_id: str
    narrative_line_id: str
    relation: ArticleCompositionRelation
    lead_story_id: str
    members: tuple[ArticleCompositionMember, ...]

@dataclass(frozen=True)
class ArticleNarrativeLine:
    line_id: str
    heading_hint: str | None
    narrative_intent: str
    prominence: ArticleProminence
    group_ids: tuple[str, ...]

@dataclass(frozen=True)
class ArticleCompositionPlan:
    narrative_lines: tuple[ArticleNarrativeLine, ...]
    groups: tuple[ArticleCompositionGroup, ...]
    suppressed_story_ids: tuple[str, ...] = ()

    @property
    def group_by_story_id(self) -> dict[str, ArticleCompositionGroup]: ...
    def to_metadata(self) -> dict[str, object]: ...

def build_article_composition_plan(
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> ArticleCompositionPlan: ...
```

Task 2 keeps `render_article_writer_context_with_stats(context, coverage_plan, *, composition_plan, material_projection, ...)` as the writer-input entry point. Task 3 adds these stable finding codes while preserving the existing finding/report types: `OVERLOADED_ROSTER_PARAGRAPH`, `CROSS_SECTION_REPETITION`, `DUPLICATE_ARTICLE_HEADING`, and `UNDEVELOPED_LEAD_PROMISE`. Existing `ArticleReaderQualityFinding`, `ArticleReaderQualityReport`, and `diagnose_article_quality(...)` remain the public diagnostic interface. Task 5 adds:

```python
@dataclass(frozen=True)
class ArticleRunPreview:
    run_id: int
    publication_type: str
    edition_slug: str
    snapshot_at: datetime
    title: str
    lead: str
    body: str
    diagnostics: dict[str, object]

async def build_article_preview_from_run(
    run_id: int,
    *,
    config: Config,
) -> ArticleRunPreview: ...
```

## Task 1: Make Coverage and Composition Relation-Aware

**Files:**

- Modify: `src/publication/article_coverage.py`
- Modify: `src/publication/article_composition.py`
- Test: `tests/publication/test_article_coverage.py`
- Test: `tests/publication/test_article_composition.py`

**Interfaces:**

- Preserve `build_article_coverage_plan(cards, context, develop_story_budget=2) -> ArticleCoveragePlan` and the complete `ArticleCoveragePlan.stories` source of Story rank, depth, and support IDs.
- Build `ArticleCompositionPlan` using the shared interfaces above. Each non-suppressed coverage Story appears in exactly one `ArticleCompositionMember`; projection-suppressed Stories remain in coverage and are listed only in `suppressed_story_ids`.
- A Story without a supported relationship remains an `independent` group. It may have `heading_hint=None`; that keeps a small `BRIEF` item available without creating a catch-all chapter.

- [ ] **Step 1: Add a coverage regression for synthetic section spreading and support pooling**

Use three generic Stories whose topic signatures all resolve to the same broad service, plus an unrelated standalone Story. Assert that coverage keeps each Story's own `support_ids`, that no Story moves only to reach a minimum section count, and that the standalone Story is still present at its original depth.

```python
def test_coverage_does_not_spread_stories_or_pool_supports_to_fill_sections():
    story_data = (
        ("story:power-a", "Электроснабжение", "На одном адресе света нет."),
        ("story:power-b", "Электроснабжение", "На другом адресе напряжение низкое."),
        ("story:power-c", "Электроснабжение", "В доме поставили солнечную панель."),
        ("story:sport", "Детский спорт", "Школа открыла запись в секцию."),
    )
    cards = [
        StoryCard(id=story_id, topic=topic, importance="medium", summary=topic)
        for story_id, topic, _text in story_data
    ]
    supports = [
        _make_simple_support(
            text,
            story_id=story_id,
            support_id=f"{story_id}:evidence:0:frag:{index}",
        )
        for index, (story_id, _topic, text) in enumerate(story_data, start=1)
    ]
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=tuple(supports),
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    plan = build_article_coverage_plan(cards, context)

    assert set(plan.story_ids) == {card.id for card in cards}
    for story in plan.stories:
        assert set(story.support_ids) == {
            support.support_id for support in supports if support.story_id == story.story_id
        }
    assert len(plan.sections) <= 2
```

- [ ] **Step 2: Run the focused coverage test and confirm the current behavior fails**

Run: `pytest tests/publication/test_article_coverage.py -q`

Expected: the new assertion fails because the current planner pools same-signature support IDs and moves Stories to fabricate section breadth.

- [ ] **Step 3: Remove synthetic section spreading, fallback chapters, and pooled support IDs**

Change `_thematic_section_id()` to return no section when a Story has no explicit generic category. Build `ArticleStoryCoverage.support_ids` from that Story's own publishable supports only. Remove the minimum-three-section donor loop. Build `ArticleThematicSection` values only for genuine category assignments; do not create `city_life` as a residual bucket. Leave every accepted Story in `ArticleCoveragePlan.stories` with its existing rank and prominence.

- [ ] **Step 4: Add relation-boundary composition tests**

Create generic support fixtures for two electricity reports on different streets, one voltage observation, one household solar detail, two water reports with different effective periods, and two equivalent place aliases from an edition profile. Assert that a broad `power` signature alone does not put all electricity Stories into a shared-condition group; a supported same-service localized difference is represented as `localized_contrast`; a supported change at the same canonical place over distinct effective times is `temporal_progression`; and unrelated or unknown relationships remain separate. Assert aliases can resolve together while distinct places never inherit a proximity relation.

- [ ] **Step 5: Implement narrative lines and evidence groups from supported relations**

In `article_composition.py`, resolve location using the existing edition `CityContextResolver`/edition geography facilities and compare only available structured service and effective-time evidence. Use the relation order `localized_contrast`, `temporal_progression`, `practical_consequence`, `shared_condition`, then `independent` only when the exact relationship has support. A shared topic keyword can propose a comparison but cannot prove a relationship. Keep every member's own support IDs on `ArticleCompositionMember`; do not pool its facts or change its epistemic status.

- [ ] **Step 6: Assert exactly-once Story membership and projection behavior**

Extend composition tests to assert that `group_by_story_id` has exactly the non-suppressed Story IDs, each Story appears in one and only one group, each member's support IDs resolve to that Story, and metadata reports line/group counts and group membership without dropping coverage IDs.

Run: `pytest tests/publication/test_article_coverage.py tests/publication/test_article_composition.py -q`

Expected: PASS, including different-state/location/time Stories, alias resolution, unknown relationships, standalone `BRIEF` material, and projection-only suppression.

- [ ] **Step 7: Commit the coverage/composition change on `dev`**

```bash
git add src/publication/article_coverage.py src/publication/article_composition.py tests/publication/test_article_coverage.py tests/publication/test_article_composition.py
git commit -m "feat: compose article stories by supported relations"
```

## Task 2: Give the Writer One Roadmap and One Copy of Each Evidence Packet

**Files:**

- Modify: `src/publication/article_writer_context.py`
- Modify: `src/article_generator.py`
- Modify: `src/publication/narrative_contract.py`
- Test: `tests/publication/test_article_writer_context.py`
- Test: `tests/publication/test_article_writer_prompt.py`
- Test: `tests/publication/test_article_composition.py`

**Interfaces:**

- Consume Task 1's `ArticleCompositionPlan.narrative_lines`, `.groups`, and `.group_by_story_id`.
- Keep `render_article_writer_context_with_stats(...)` returning the current context string plus materialization statistics; extend its statistics with line/group counts, group-size distribution, and the rendered packet representation.
- Keep all current support-level time and attribution fields. `effective_from`/`effective_until` describe event/service time; `observed_at` describes when the report was made.

- [ ] **Step 1: Add tests for duplicate packet membership and duplicate roadmap inventories**

Build a two-Story localized-contrast plan with a different support per Story. Assert the final context has one compact roadmap, one full packet per Story, one occurrence of each support ID, and no second complete Story/support roster. Include one short standalone community report and a mixed useful local notice with contact/link payload; assert the useful fact and single-source attribution remain while contact data is sanitized.

- [ ] **Step 2: Run the focused writer-context tests and confirm the duplicated form fails**

Run: `pytest tests/publication/test_article_writer_context.py tests/publication/test_article_writer_prompt.py -q`

Expected: the new assertions fail while the context renderer repeats the plan and/or bundle membership headers.

- [ ] **Step 3: Render a compact narrative roadmap before the member packets**

Replace repeated bundle headers with one `ARTICLE COMPOSITION ROADMAP` block containing each narrative line's depth and intent, each relation group's role, and compact Story IDs for traceability. Render each Story packet exactly once with its own projected supports exactly once. Do not place a full membership header inside every full-context packet. If a Story is independent and has no heading hint, keep it as a brief writer-facing mention instead of requiring a chapter.

- [ ] **Step 4: Remove the parallel full outline from the writer prompt**

In `article_generator.py`, delete the `plan_sections_lines` block that expands every bundle into repeated Story/support IDs. Retain one roadmap plus source packets. In `narrative_contract.py` and the writer prompt, preserve the `DEVELOP`/`WEAVE`/`BRIEF` hierarchy and instruct the writer to group only supported relationships, preserve meaningful local/time contrasts, use each useful notice once, avoid listing every address to prove coverage, and avoid repeating the lead's premise as a generic closing.

- [ ] **Step 5: Pin event-time versus report-time handling and natural article shape**

Add prompt/context tests where `observed_at` is known but effective event time is unknown, and where a future/current operational observation has explicit effective times. Assert the prompt identifies report time only as attribution chronology and does not synthesize an event start time from it. Assert no fixed heading, address, paragraph, or Story-per-paragraph quota is introduced, and no local city names are inserted into generic prompt text.

- [ ] **Step 6: Preserve edition-local as-of context and projection boundaries**

Keep `PUBLICATION AS OF` rendered from the configured edition timezone and fail before the writer call when that timezone is invalid or unavailable. Verify a mixed commercial/community source keeps the useful fact, a short community report survives, phone numbers/URLs remain sanitized from writer context, and raw `ArticleSupport.source_text` remains unchanged for validation.

- [ ] **Step 7: Run the writer-context and prompt regression set**

Run: `pytest tests/publication/test_article_composition.py tests/publication/test_article_writer_context.py tests/publication/test_article_writer_prompt.py tests/publication/test_article_coverage.py -q`

Expected: PASS; each citable packet is rendered once, the plan is compact and hierarchical, distinct relation groups remain clear, short community material survives, and no unsupported roster instruction remains.

- [ ] **Step 8: Commit the writer-input change on `dev`**

```bash
git add src/publication/article_writer_context.py src/article_generator.py src/publication/narrative_contract.py tests/publication/test_article_writer_context.py tests/publication/test_article_writer_prompt.py tests/publication/test_article_composition.py
git commit -m "fix: give article writer one concise composition roadmap"
```

## Task 3: Detect Long-Read Composition Defects Deterministically

**Files:**

- Modify: `src/publication/article_quality.py`
- Test: `tests/publication/test_article_quality.py`

**Interfaces:**

- Keep `ArticleReaderQualityFinding(code, unit_id, message, support_ids, severity)` and `ArticleReaderQualityReport(findings)`.
- Keep `diagnose_article_quality(draft, coverage_plan, context, material_projection=None, place_resolver=None) -> ArticleReaderQualityReport`.
- Add the four shared codes listed above. Each finding points to a stable paragraph, lead, title, or heading unit and carries only support IDs that establish the finding.

- [ ] **Step 1: Add generic tests for a packed roster, cross-section repeat, and heading/lead mismatch**

Add a long single paragraph with many distinct locations and service-state fragments; assert one `OVERLOADED_ROSTER_PARAGRAPH` finding targets that paragraph. Add a localized contrast paragraph that names several distinct places but explains their different states; assert it is not flagged. Add the same Story repeated across lead, body, and closing with no new state/consequence; assert `CROSS_SECTION_REPETITION`. Add repeated normalized section headings/title-heading duplication and a lead whose supported major promise is absent from body; assert the heading and promise codes point at their owning units.

- [ ] **Step 2: Add false-positive guards for temporal progression and short community facts**

Assert the diagnostic does not flag (a) a concise contrast with separate, supported local states, (b) repeated evidence that reports a materially new effective state/time or resident consequence, or (c) one compact short, single-source `BRIEF` report. Keep the existing tests for quote rolls, missing `DEVELOP`, detail support, aliases, and contradictory same-place service states.

- [ ] **Step 3: Run the focused quality tests and confirm the new patterns are missed**

Run: `pytest tests/publication/test_article_quality.py -q`

Expected: the four new pattern assertions fail while current checks still cover quote rolls, missing major Stories, useful details, and contradictory service states.

- [ ] **Step 4: Implement support-aware, high-confidence composition checks**

For overloaded paragraphs, combine cited Story/support count with resolved location, service-state, and temporal claims; do not gate on paragraph length or place-name count alone. Treat an explicit supported contrast or progression as narrative structure, not an inventory. For cross-section repetition, require shared Story/support or claim linkage and no material change in state, time, or consequence. For heading duplicates, normalize punctuation/case before comparing the title and section headings. For lead promises, compare the lead's supported Story/claim references to the actual body coverage; do not use a generic word list that blocks themes based on style alone.

- [ ] **Step 5: Assign severity and stable targets without weakening factual checks**

Use `blocking` for a high-confidence sustained roster, repeated major line, duplicate-heading pattern that damages navigation, or unsupported major lead promise so it triggers the existing editor path and remains fail-closed if unresolved. Use `repair` for a local quote roll and `warning` for an optional detail or low-confidence style signal. Preserve `CONTRADICTORY_SERVICE_STATE` as blocking only for the same canonical service/place with overlapping effective time. Do not reinterpret observations from different streets as contradictory city-wide claims.

- [ ] **Step 6: Run the quality regression set**

Run: `pytest tests/publication/test_article_quality.py -q`

Expected: PASS; packed rosters and true repetitions are found, while supported contrast/progression and short local reports pass without false blocking findings.

- [ ] **Step 7: Commit the reader-quality diagnostics on `dev`**

```bash
git add src/publication/article_quality.py tests/publication/test_article_quality.py
git commit -m "feat: detect article roster and repetition defects"
```

## Task 4: Patch Locally, Revalidate, and Fail Closed on Unresolved Serious Defects

**Files:**

- Modify: `src/publication/article_editor.py`
- Modify: `src/article_generator.py`
- Modify: `src/publication/article_finalization.py`
- Test: `tests/publication/test_article_editor.py`
- Test: `tests/publication/test_article_finalization.py`
- Test: `tests/publication/test_facade.py`

**Interfaces:**

- Preserve `ArticleEditor.edit_draft(...) -> tuple[StructuredArticleDraft, ArticleValidationResult]`, bounded by `article_editor_max_attempts`.
- Pass the existing `ArticleReaderQualityReport`, `ArticleCoveragePlan`, `ArticleMaterialProjection`, and edition `place_resolver` into the editor and finalizer; do not merge style findings into `ArticleValidationResult`.
- Preserve `ArticleFinalizer.finalize(...)`'s public result shape. Final metadata contains quality report before editing, after editing, unresolved findings, attempt count, and patched unit IDs, without raw prompt/response copies or provider secrets.

- [ ] **Step 1: Add editor tests for adjacent context and issue-specific instructions**

For a roster paragraph in the middle of a section, assert the editor unit context includes that paragraph plus the immediately adjacent paragraph and section heading, while source supports remain limited to the finding's relevant support IDs. Assert `OVERLOADED_ROSTER_PARAGRAPH` requests preserving supported exceptions while removing redundant enumeration; `CROSS_SECTION_REPETITION` requests moving the repeated claim to its best-supported home; and heading/lead findings request a local heading/lead correction. Assert unrelated units are not passed as patch targets.

- [ ] **Step 2: Run the focused editor test and confirm context is insufficient**

Run: `pytest tests/publication/test_article_editor.py -q`

Expected: the new assertions fail because the current editor receives target units and supports but not neighboring prose/headings or code-specific instructions.

- [ ] **Step 3: Add neighboring reader context without widening the evidence boundary**

Extend `_build_unit_contexts()` to include adjacent paragraphs and relevant section/title headings as uneditable context. Keep the patch schema limited to the original target unit IDs. Include only the finding's cited supports and existing factual-validation supports in the evidence block. Add a finding-code instruction map for the four new codes and existing quote-roll/contradiction findings; require untouched facts and direct-quote exactness to remain unchanged.

- [ ] **Step 4: Assert every patch is strictly revalidated and diagnostics are recomputed**

Extend editor tests to return a patched roster that introduces an unsupported number or cause. Assert `validate_article_draft()` reports it and the factual defect is not hidden by a quality pass. Return a supported localized contrast and assert it no longer triggers the roster diagnostic. Assert quality diagnostics rerun after each bounded patch and `last_patched_unit_ids` contains only returned target IDs.

- [ ] **Step 5: Add a finalizer rejection case for unresolved serious composition defects**

Use a draft with a high-confidence roster or repeated major storyline and configure the editor response to leave it unchanged. Assert finalization raises `ArticlePublicationRejected`, metadata includes `article-reader-quality-v3`, the unresolved quality code/unit IDs, patch history, and factual validation outcome. Add a companion case where only an optional detail warning remains and assert the grounded article is accepted.

- [ ] **Step 6: Persist compact before/after quality and writer-input metadata**

Ensure the successful or rejected `publication_generation_attempts.metadata` path includes the versioned quality report before/after edit, composition line/group counts and group-size distribution, projection action counts/reasons, context/prompt hashes and lengths, local `as_of`/edition timezone, edit attempts, patched unit IDs, and final Evidence Boundary/quality-gate status. Do not persist the raw context, full prompt, provider secret, or unnecessary raw response in these diagnostics. Keep final checks on the post-deduplication rendered article.

- [ ] **Step 7: Verify the bounded-edit call path and final gate**

Run: `pytest tests/publication/test_article_editor.py tests/publication/test_article_finalization.py tests/publication/test_facade.py -q`

Expected: PASS; one original writer response is followed only by up to the configured editor attempts, each patch is fact-checked, serious unresolved composition defects fail closed, and optional-detail warnings do not block.

- [ ] **Step 8: Commit the repair/gate change on `dev`**

```bash
git add src/publication/article_editor.py src/article_generator.py src/publication/article_finalization.py tests/publication/test_article_editor.py tests/publication/test_article_finalization.py tests/publication/test_facade.py
git commit -m "fix: gate unresolved article composition defects"
```

## Task 5: Replay a Frozen Article Run Without Creating Publication Side Effects

**Files:**

- Create: `src/publication/article_preview.py`
- Modify: `scripts/preview_article.py`
- Test: `tests/publication/test_preview_article.py`
- Test: `tests/publication/test_facade.py`

**Interfaces:**

- `build_article_preview_from_run(run_id, *, config) -> ArticleRunPreview` reads the `PublicationRun` and sealed inputs through `PublicationRepository`, materializes them with `EventEditorialAdapter.adapt_inputs_on(...)`, and calls `ArticleGenerator.generate_from_frozen_input(...)` with an in-memory `GenerationAttemptObserver`.
- Accept only run IDs whose `publication_type` belongs to `ARTICLE_PUBLICATION_TYPES`; raise `ValueError` before writer generation for a missing run, unsupported type, missing frozen inputs, or missing edition timezone.
- The preview includes article Markdown and compact captured generation/quality diagnostics. It creates no intent, `Publication`, refresh row, selection, durable generation-attempt row, delivery job, Telegraph page, cover image, or Telegram message.
- `scripts/preview_article.py --run-id ID` is mutually exclusive with an explicitly supplied `--date` or `--hours`; ordinary preview mode continues to default to the Berdyansk edition and 24-hour lookback. `--output` writes article Markdown; optional `--diagnostics-output` writes the compact JSON metadata sidecar.

- [ ] **Step 1: Add read-only replay tests for article, invalid type, missing run, and no mutation**

Mock `PublicationRepository`, `EventEditorialAdapter`, `ArticleGenerator`, and `ApplicationInfrastructure`. For a valid frozen article run, assert the exact run ID and sealed inputs reach the adapter, article Markdown and diagnostics are returned, and no orchestrator, selection service, publication generator, delivery service, image generator, or database generation observer is constructed. For a missing run, digest run, or empty sealed input list, assert a clear error occurs before the writer call. Assert repository methods used are reads and the run status is unchanged.

- [ ] **Step 2: Add CLI argument tests for frozen replay mode**

Assert `--run-id 189` dispatches to frozen replay without calling `build_publication_preview`; `--run-id 189 --date 2026-09-23` and `--run-id 189 --hours 24` fail argument validation; default invocation preserves the current new-snapshot behavior. Assert article Markdown goes to `--output` and only metadata (never contact/source payload or provider secrets) goes to `--diagnostics-output`.

- [ ] **Step 3: Implement frozen-run materialization and an in-memory attempt observer**

Within one UoW transaction, load the run and sealed inputs and pass both to `EventEditorialAdapter.adapt_inputs_on(...)`; do not call the orchestrator, selector, `PublicationGenerationService`, or any delivery/image component. After validating the run type and edition timezone, call `ArticleGenerator.generate_from_frozen_input(frozen, attempt_observer=memory_observer)`. Return the local article and safe diagnostics from captured attempt metadata. Serialize only the versioned metadata fields needed to explain composition and quality; do not store full source packets, phone numbers, raw provider output, or credentials.

- [ ] **Step 4: Add `--run-id` replay to the existing preview script**

Make `--hours` default to `None` at parse time so an explicitly supplied value can be rejected with `--run-id`; pass `24` in ordinary preview mode when it is omitted. Preserve the current edition/date validation for ordinary previews. In replay mode, use the frozen run's edition and snapshot, and reject a conflicting explicit edition if supplied. Keep article output on stdout unless `--output` is set; keep optional diagnostics in the JSON sidecar only.

- [ ] **Step 5: Run focused replay and preview tests**

Run: `pytest tests/publication/test_preview_article.py tests/publication/test_facade.py -q`

Expected: PASS; frozen replay uses only selected sealed inputs, rejects non-article/missing runs before generation, and performs no publication/delivery writes. The existing no-run preview path still creates the same no-delivery preview it created before.

- [ ] **Step 6: Commit the frozen-preview path on `dev`**

```bash
git add src/publication/article_preview.py scripts/preview_article.py tests/publication/test_preview_article.py tests/publication/test_facade.py
git commit -m "feat: replay frozen article runs without delivery"
```

- [ ] **Step 7: Inspect the September 23 article from frozen run 189 on the server**

First verify the server checkout revision with this read-only command:

```bash
ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200 'cd /home/opc/Telebrief && git rev-parse HEAD'
```

Run the preview only when that revision is the reviewed `dev` commit. Save the article and diagnostics to temporary files, then copy them locally for inspection:

```bash
ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200 'cd /home/opc/Telebrief && python scripts/preview_article.py --run-id 189 --output /tmp/telebrief-article-run-189.md --diagnostics-output /tmp/telebrief-article-run-189.json'
scp -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200:/tmp/telebrief-article-run-189.md /tmp/telebrief-article-run-189.md
scp -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200:/tmp/telebrief-article-run-189.json /tmp/telebrief-article-run-189.json
```

Do not restart services, change server publication state, or deliver the article. Compare the generated article with the acceptance criteria: distinct utility states remain distinct, one supported localized contrast reads naturally, useful city-life details survive, roster paragraphs and repeated lead theses are absent or are reported as unresolved, and metadata records the final reader-quality gate. If server code is not on the reviewed commit, stop and report that prerequisite rather than changing a running checkout implicitly.

## Implementation Self-Review

- **Spec coverage:** Task 1 covers exactly-once composition, relation-aware grouping, alias handling, and removal of synthetic/catch-all chapters. Task 2 covers the single compact roadmap, per-Story evidence packet rendering, time roles, local time, conservative directory projection, and prose instructions. Task 3 covers roster/repetition/heading/lead diagnostics plus false-positive guards. Task 4 covers targeted neighboring-context patches, strict factual revalidation, bounded attempts, final fail-closed behavior, and generation metadata. Task 5 covers frozen no-delivery replay and the run 189 qualitative regression. The global constraints preserve the one-writer-call architecture, article scope, and all non-goals.
- **Placeholder scan:** no `TODO`, `TBD`, “implement later,” or unnamed implementation step remains. The inline coverage test uses `_make_simple_support`, already defined in `test_article_coverage.py`; all other fixtures are described in their owning task, and all production interfaces are defined above or in the shared interfaces section.
- **Type consistency:** Task 1 exposes `ArticleCompositionPlan.narrative_lines`, `.groups`, `.group_by_story_id`, and `to_metadata()`; Task 2 consumes those exact members. Tasks 3–4 keep the existing `ArticleReaderQualityFinding`, `ArticleReaderQualityReport`, `diagnose_article_quality`, editor return pair, and finalization result; Task 5 defines `ArticleRunPreview` and `build_article_preview_from_run` and uses the existing `ARTICLE_PUBLICATION_TYPES`, repository, adapter, generator, and observer contracts.
- **Review focus:** each of the five input classes is tied to a regression test in its owning task. The invalid replay cases use repository mocks, and the live run 189 check is explicitly gated on server code already matching the reviewed commit.
- **Execution order:** complete one task, run its focused checks, review, and commit on `dev` before starting the next task. Use a fresh implementer and reviewer for each task as already selected by the user; work sequentially because commits target the shared `dev` branch.
