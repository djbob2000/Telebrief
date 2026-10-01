# Article Structural Editing Implementation Plan

> **For agentic workers:** Execute task-by-task with the user-selected `superpowers:subagent-driven-development` approach. Root owns integration and final review. Agents must read this plan, its linked spec, and `AGENTS.md` §0 before editing. Do not commit individual tasks.

**Goal:** Improve thematic coherence in rich city-life articles by giving the writer support-level topic hints and allowing the existing editor bounded, source-preserving paragraph restructuring.

**Architecture:** Project structured service-subject hints from Event-First evidence to individual article supports and writer packets without changing Story membership or digest behavior. Extend the existing ArticleEditor with a fingerprint-bound, pass-local operation registry; apply each structural response atomically, reground rewritten prose, and assess the whole resulting draft under the existing writer/editor/deadline budget.

**Tech Stack:** Python 3.14, frozen dataclasses, existing Event-First publication models, ArticleEditor and validators, configured LLM provider, Ruff, MyPy, frozen article preview CLI.

**Spec:** `docs/superpowers/specs/2026-10-01-article-structural-editing-design.md`

## Global Constraints

- Work directly on `dev`; preserve unrelated changes, including `docs/superpowers/.DS_Store`.
- One writer stage invocation and at most two editor stage invocations; no planner, second writer, or per-operation LLM call.
- Use the existing shared generation deadline and only models allowed by `.env`.
- Keep one canonical composition membership per Story; subject hints are optional advisory context on a support.
- Known exact service keys map as `water_supply` → `water`, `power_supply` → `power`, `gas_supply` → `gas`, `heating` → `heating`, and `connectivity` → `telecom`; unknown keys stay unmapped. `dimension` is not a theme.
- Keep current `article_max_words` and `article_max_sections` hard-limit semantics; the rich-day soft target can use the configured 2400 words. No Story-count quota, coverage veto, or filler.
- Structural operations in one editor response form one atomic batch. Resolve all positional IDs against its immutable base; re-ground changed prose and assess the complete candidate.
- Keep Evidence Boundary, attribution, exact direct quotes, time, service state, geography, and useful single-source local reports intact.
- Do not change Event clustering, Gate, Analysis, eligibility, digest, delivery, database schema, or require backfill.
- User requested no TDD and no new automated tests. Use focused static checks, offline/manual contract inspection, and one final frozen-run preview for article run 257 on the live `telebrief-app` after deployment.
- Keep the spec and plan tracked despite the repository ignore rule for `docs/superpowers/`; stage them explicitly in the single final commit.
- Commit, push, deploy, and run the server preview only after all implementation work and review are complete. The preview is read-only and does not deliver the article.

## Review Focus

1. A Story contains independent water and safety supports: writer places each fact by its own evidence and does not infer a shared cause. Task 1 renders both supports with separate provenance and advisory topics; Task 2 checks this on the final run 257 article.
2. Two paragraphs move and positional `Pnnn` IDs shift: registry resolution and origin mapping preserve paragraph identity, provenance, and accurate blocker comparison. Task 2 manually traces this through candidate creation and rollback.
3. A split adds a claim not grounded by its support allowlist or changes direct speech: regrounding/full validation rejects the complete structural batch. Task 2 inspects this refusal and unchanged base checkpoint.
4. Optional or unknown service hints occur in a legacy support, duplicate support, or compact/grouped packet: absence remains compatible and deduplication does not mislabel a support. Task 1 traces all writer materialization paths.
5. A rich or thin edition reaches the writer: the profile uses existing hard bounds, never pads to a lower target, and exposes breadth/readiness diagnostics without suppressing a safe article. Task 3 inspects the exact profile and final preview.

---

## File Ownership and Interfaces

| Task | Owned paths | Interface delivered |
|---|---|---|
| 1. Support topics | `src/publication/evidence.py`, `event_editorial_adapter.py`, `article_context.py`, `article_writer_context.py` | Frozen optional `ServiceSubjectHint(subject_key: str, subject_label: str, family: str \| None)`; optional `PublicationEvidence.service_subject_hint` and `ArticleSupport.service_subject_hint`, default `None`; `article_support_theme_hints(support: ArticleSupport) -> tuple[str, ...]`; writer packet helpers render hints for each support. |
| 2. Structural editor and prompt | `src/publication/article_editor.py`, `article_preview.py`, `src/article_generator.py` | Typed structural operation payload local to ArticleEditor and immutable-base resolver; bounded editor applies a whole structural batch and exposes sanitized operation outcomes to preview; writer uses the Task 1 support hint without changing Story membership. |
| 3. Composition, quality, and length | `src/publication/article_composition.py`, `article_quality.py`, `article_quality_policy.py`, `article_length.py`, `src/publication/policies.py` | `ArticleCompositionRichnessSummary` reports distinct support themes, DEVELOP lines, and deduplicated anchors; quality diagnostics inspect cited supports for thematic compatibility; daily rich profile has a soft upper target bounded by configured `article_max_words`; sole owner of `ARTICLE_COMPOSITION_VERSION` (in `article_composition.py`) and the shared-policy `ARTICLE_WRITER_VERSION` bump. |
| 4. Editorial contract and rollout | `AGENTS.md` §0 and §6 | Documentation matches implemented evidence safety/readiness boundary and the reviewed manual rollout contract. |

Tasks 1–3 have disjoint file ownership and can be implemented concurrently after the interface above is accepted. Task 4 follows the integrated implementation so the canonical instructions describe the shipped behavior. Root coordinates the shared interfaces, resolves conflicts, runs final checks, and owns the single commit/deploy/preview sequence.

## Task 1: Project Per-Support Service Topics

**Files:**
- Modify: `src/publication/evidence.py`
- Modify: `src/publication/event_editorial_adapter.py`
- Modify: `src/publication/article_context.py`
- Modify: `src/publication/article_writer_context.py`

**Consumes:** Existing `EvidenceItemPayload.service_state`, `PublicationEvidence`, `ArticleSupport`, and writer full/compact/grouped/brief materialization paths.

**Produces:** `ServiceSubjectHint(subject_key, subject_label, family)`; optional immutable fields on publication evidence and article supports; exact-key family mapping; `article_support_theme_hints(support: ArticleSupport) -> tuple[str, ...]`, combining exact structured family with the existing generic `detect_service_families(support.text)` taxonomy; hint-aware semantic/group keys and rendered support-local advisory context.

- [x] Add the frozen hint model and optional default fields. Map only the five keys in Global Constraints; do not infer from localized labels, city names, or `dimension`.
- [x] Populate the hint only from structured `service_access.service_state` in the primary EventEditorialAdapter projection. Leave legacy fallback constructors without a hint.
- [x] Copy the hint to ArticleSupport and include it in `_support_semantic_key`, so evidence with different subjects cannot merge and discard topic meaning.
- [x] Implement `article_support_theme_hints(support: ArticleSupport) -> tuple[str, ...]`. Combine a known exact-key service family and existing generic lexical families for that support; return an empty tuple when neither is known. Keep these as navigation only, never facts or publication eligibility.
- [x] Render each support's hints beside that support in every full, compact, grouped, and brief writer packet. Include both structured and derived hints in writer grouping keys; ambiguous aggregation renders no specific topic.
- [x] Run `.venv-ci/bin/ruff check src/publication/evidence.py src/publication/event_editorial_adapter.py src/publication/article_context.py src/publication/article_writer_context.py` and the same file list with `.venv-ci/bin/ruff format --check`. Manually trace known, unknown, and multi-family support hints through projection, deduplication, and each packet renderer; no LLM call.

**Done when:** The writer can distinguish known service topics at evidence level; `None`/unknown hints preserve legacy behavior; Story membership and non-article consumers are unchanged.

## Task 2: Add Bounded Structural Editing

**Files:**
- Modify: `src/publication/article_editor.py`
- Modify: `src/publication/article_preview.py`
- Modify: `src/article_generator.py`

**Consumes:** Existing `ArticleEditor.edit_draft`, positional H/P unit builder, paragraph claim/support models, quote guard, `apply_patches` regrounding, `ArticleAssessmentCheckpoint`, shared timeout and the Task 1 support hint.

**Produces:** A typed structural operation payload local to ArticleEditor, per-pass registry bound to the full base fingerprint, exact origin mapping, atomic candidate application/rollback, full-draft assessment, versioned writer instructions, and prose-free safe preview operation diagnostics. Task 3 produces the composition summary and extended length profile consumed by the generator.

- [x] Define frozen `ArticleStructuralOperation(operation_id: str, kind: Literal["move", "recompose", "create_section"], source_unit_ids: tuple[str, ...], destination_section_id: str | None, destination_before_unit_id: str | None, heading: str | None, paragraph_texts: tuple[str, ...])` in `article_editor.py`. `move` takes exactly one source and an existing destination, with no output text; `recompose` requires an existing destination and one or more output paragraphs; `create_section` uses `destination_section_id=None`, creates local destination ID `new:<operation_id>`, and requires output paragraphs with a supported heading. Keep existing title/lead/heading/paragraph text patch compatibility.
- [x] Build the immutable pass registry before applying any operation. Reject stale/unknown IDs, unauthorized source or destination units, duplicate/conflicting source edits, invalid insertion anchors, and unresolved operation order before mutating the draft.
- [x] Implement pure MOVE by transferring the paragraph and all its current claims, supports, origin, and provenance unchanged. Implement SPLIT/RECOMPOSE from resulting prose; the model does not author claim/support IDs. Rebuild claim metadata with existing parsing/regrounding, restricted to the explicit union of eligible source supports.
- [x] Preserve direct-quote spans exactly across all base units in one operation; changing quote wording is not authorized. Rebuild section evidence/heading metadata deterministically; do not lose a heading's unique supported fact when its section becomes empty.
- [x] Apply all structural operations in one editor response as one transaction. Resolve destinations/order before application, assess the complete resulting draft, and roll back the whole structural batch to the exact pre-structure checkpoint on a new blocker or assessment error. If the same response also has legacy text patches, retain only text patches already accepted at that checkpoint. Map unchanged/MOVE units to their base registry identity and rewritten paragraphs to operation-local IDs when comparing findings; ambiguous/global attribution rolls back the batch. Keep legacy text-only per-unit quarantine behavior.
- [x] Re-run complete Evidence Boundary and reader-quality checks and create an `ArticleAssessmentCheckpoint` for the exact changed draft and current input fingerprint. Each subsequent editor call gets a fresh registry.
- [x] Keep editor stage calls at two maximum inside the existing shared deadline. Skip a structural operation with a clear outcome if the complete article context or required supports cannot fit current prompt/model budgets; never label a truncated article as complete context.
- [x] Remove fact-shaped city/transport examples from the shared writer prompt. State the source-only rule without concrete service claims. Task 3 owns the shared policy version file and applies the writer prompt version bump.
- [x] Add only compact, allowlisted proposed/applied/rejected/skipped operation metadata to preview diagnostics; do not persist candidate prose or source text in ordinary metadata.
- [x] After Task 3 produces `ArticleCompositionRichnessSummary` and the extended `ArticleLengthProfile`, move profile derivation in `src/article_generator.py` to after frozen coverage/composition preparation; pass the summary to `derive_article_length_profile` and render its three counts as non-factual editorial context. Reuse that exact profile for writer prompt, validation, fingerprint, and diagnostics.
- [x] Run `.venv-ci/bin/ruff check src/publication/article_editor.py src/publication/article_preview.py src/article_generator.py` and the same file list with `.venv-ci/bin/ruff format --check`. Manually inspect the move, split, unknown-ID, and rollback paths against the Review Focus; no writer/editor call.

**Done when:** The editor can repair a specifically targeted thematic defect, cannot rewrite unrelated material, cannot fabricate provenance, and produces either an exactly assessed candidate or the unchanged safe base.

## Task 3: Align Composition Diagnostics and Soft Length

**Files:**
- Modify: `src/publication/article_composition.py`
- Modify: `src/publication/article_quality.py`
- Modify: `src/publication/article_quality_policy.py`
- Modify: `src/publication/article_length.py`
- Modify: `src/publication/policies.py`

**Consumes:** Article coverage plan, projected supports, Task 1's support-local hint, current quality policy registry, and the 2400-word / 8-section editorial configuration.

**Produces:** Frozen `ArticleCompositionRichnessSummary(thematic_line_count: int, develop_line_count: int, detail_anchor_count: int)` from `build_article_composition_richness_summary(coverage_plan: ArticleCoveragePlan, composition_plan: ArticleCompositionPlan, context: ArticleEditorialContext, material_projection: ArticleMaterialProjection)`. Extend `derive_article_length_profile(context: ArticleEditorialContext, config: PublicationEditorialConfig, *, richness_summary: ArticleCompositionRichnessSummary | None = None) -> ArticleLengthProfile` to carry those counts. Support-aware advisory composition diagnostics without a coverage publication gate.

- [x] Preserve one canonical composition group per visible Story. Use per-support hints in writer guidance; do not move or split knowledge Story membership, change candidate selection, or infer cross-support proximity, common cause, or chronology. Bump `ARTICLE_COMPOSITION_VERSION` from `v1` to `v2` for the changed article composition contract.
- [x] Evaluate a section finding from the supports cited by its actual paragraphs. Attach support IDs and actionable paragraph scope when known, so Task 2 can target the right units. An unknown hint or multiple topics with a clear evidence-backed bridge is not itself a blocking finding. Reuse the existing quality registry for any added finding; no count-only publication gate.
- [x] Leave current hard article/section limits and rich/standard/thin classification intact. For the daily rich profile only, set the soft upper target to `min(config.article_max_words, profile.hard_max_words)`; with current config that is up to 2400 words. Do not scale a target by Story count or enforce the lower target.
- [x] Implement the summary from projected support themes plus coverage/composition membership and existing detail support IDs. `thematic_line_count` counts distinct known themes across planned supports; `develop_line_count` counts composition lines with a DEVELOP story; `detail_anchor_count` counts distinct projected detail support texts. Deduplicate anchors by normalized projected support text, expose only counts, and do not classify from location labels.
- [x] Carry its three counts as integer fields on `ArticleLengthProfile`, with defaults preserving existing construction sites. Task 2 renders them in the writer context and safe diagnostics.
- [x] Bump `ARTICLE_WRITER_VERSION` from `v15` to `v16` in `src/publication/policies.py`; keep supported previous versions according to the existing compatibility policy.
- [x] Treat missing breadth/detail as editorial readiness, never as a new publication veto.
- [x] Run `.venv-ci/bin/ruff check src/publication/article_composition.py src/publication/article_quality.py src/publication/article_quality_policy.py src/publication/article_length.py src/publication/policies.py` and the same file list with `.venv-ci/bin/ruff format --check`. Manually inspect rich/thin profile calculations, support-source resolution for themes, and unchanged safe acceptance behavior.

**Done when:** A rich day can use its configured soft headroom, thin days stay concise, and composition/readiness diagnostics cannot suppress a safe article or drop a legitimate Story.

## Task 4: Update the Canonical Editorial Contract

**Files:**
- Modify: `AGENTS.md` §0.7–0.9 and §6.3–6.7

**Consumes:** The integrated code and accepted contracts from Tasks 1–3.

**Produces:** Canonical guidance for support-level hints, bounded structural edits, transactional rollback, article readiness, and safe preview diagnostics.

- [x] Record that support-level topics guide prose but do not change Story membership, geography, or evidentiary truth.
- [x] Document MOVE/SPLIT/RECOMPOSE, immutable pass-local IDs, source regrounding, whole-candidate assessment, one writer / maximum two editor calls, and operation rollback.
- [x] Keep broad coverage and microdetails as editorial goals; preserve the no-coverage-veto/no-filler rule and the distinction between a safe accepted article and editorial readiness.
- [x] Reconcile only article-specific descriptions that would contradict these contracts; do not change digest instructions.
- [x] Review `AGENTS.md` against the final code paths and linked structural-editing spec; confirm no single-source or useful small reports have become ineligible.

**Done when:** `AGENTS.md` describes behavior implemented in the repository and does not conflict with the product contract in its §0.

## Task 5: Integration, Review, and Server Preview

**Files:** All Task 1–4 changes; final commit includes the ignored spec and plan documents.

- [x] Root reviews the integrated diff for task-boundary violations, concurrent ownership conflicts, default/legacy compatibility, exact assessments, prompt/diagnostic version bumps, and preservation of unrelated `.DS_Store` changes.
- [x] For every delegated task, the implementer returns changed paths and focused check evidence; a fresh read-only subagent reviews that task's diff before integration. Start independent tasks concurrently on disjoint paths, refill slots as each review/implementation finishes, and keep no more than three child agents active alongside root.
- [ ] Run `.venv-ci/bin/ruff check` and `.venv-ci/bin/ruff format --check` on all changed Python paths. Run `.venv-ci/bin/mypy src/publication/evidence.py src/publication/event_editorial_adapter.py src/publication/article_context.py src/publication/article_writer_context.py src/publication/article_composition.py src/publication/article_quality.py src/publication/article_quality_policy.py src/publication/article_length.py src/publication/policies.py src/publication/article_editor.py src/publication/article_preview.py src/article_generator.py`; run `git diff --cached --check` and verify the staged allowlist contains no unrelated paths. Do not add or run automated tests.
- [ ] Review the complete behavior against the 14 manual acceptance scenarios in the spec. Record any unmet editorial scenario as a concrete readiness defect; passing safety checks alone is not editorial acceptance.
- [ ] Stage explicit changed paths plus `git add -f docs/superpowers/specs/2026-10-01-article-structural-editing-design.md docs/superpowers/plans/2026-10-01-article-structural-editing.md`; exclude `docs/superpowers/.DS_Store`.
- [ ] Make one integrated commit on `dev`, push `dev`, and wait for the existing deployment workflow to complete successfully.
- [ ] Confirm the live `telebrief-app` revision, then run exactly once from the local checkout: `ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key -o BatchMode=yes opc@92.5.58.200 'docker exec telebrief-app python -u scripts/preview_article.py --run-id 257 --output /tmp/article-run-257-structural.md --diagnostics-output /tmp/article-run-257-structural.json'`. Retrieve and manually review the full article text and diagnostics. Do not send or publish it. Report the actual text, remaining editorial defects, and result to the user.

## Self-Review

- Spec sections 1–3 map to the goal/global constraints and Tasks 1–4.
- Spec §4.1 maps to Task 1; §4.2 to Tasks 2–3; §§4.3–4.5 to Task 2 and integration; §5 to the manual review list; §§6–8 to the global constraints, Task 4, and Task 5.
- Tasks 1–3 have disjoint owned paths and explicit data/behavior interfaces; Task 4 follows integration. No source file has competing concurrent owners.
- No step introduces TDD, new automated tests, a writer/planner call, an extra run, an increased editor budget, or a user-facing publication side effect.
- No new numeric Story coverage goal or new article hard limit is introduced. All optional hint fields preserve legacy constructors and are only populated from structured service state.
