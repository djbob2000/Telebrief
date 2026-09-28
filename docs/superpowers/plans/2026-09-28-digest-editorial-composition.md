# Digest Editorial Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Work in dependency order; use no more than four active agents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a faithful, readable, geographically coherent single-post digest that removes genuine repetition and directory noise while preserving useful local facts and clearly reporting when quality checks are incomplete.

**Architecture:** Keep the existing Event-First workflow and add a digest-only frozen eligibility and budget-admission stage, a fact-level composition map, one structured writer call, and at most one evidence-aware repair. Render one canonical Telegram artifact, validate that complete artifact and its trace, then return it as a no-delivery preview or fail closed.

**Tech Stack:** Python 3.14, async publication pipeline, PostgreSQL publication snapshots and JSONB metadata, configured LLM provider, Docker Compose server preview.

**Spec:** `docs/superpowers/specs/2026-09-28-digest-editorial-composition-quality-design.md`

## Global Constraints

- Preserve legitimate single-source community reports; lack of corroboration alone is never an exclusion reason.
- The digest is a scan-first coverage product, not a dashboard: no separate City Situation panel, synthetic lead, or footer.
- The Telegram single-message technical ceiling is 4096 characters after entity parsing; target 2500–3700 characters only where material volume permits.
- Preserve 100% coverage of each Story and material fact admitted into the frozen DigestPresentationPlan.
- Never drop selected facts, invent claims, infer geography, or use raw-message concatenation / deterministic prose fallback to rescue a failed narrative.
- Digest-only budget deferrals must not alter article eligibility, stored knowledge, Event-First truth, or model allowlists. The frozen Gate candidate set controls Event-First eligibility; a selector's hard-exclusion label alone never drops an eligible candidate.
- No new TDD cycle or test suite. User requested skipping TDD; use focused static checks, frozen editorial comparisons, and server previews.
- Do not send or schedule a Telegram publication. Final server check uses `scripts/preview_digest.py` only.
- Leave the existing user-modified `docs/superpowers/.DS_Store` untouched and unstaged.
- Include the ignored spec and plan deliberately in the single final commit on `dev`; do not commit intermediate implementation steps.

## Review Focus

- Mixed commercial/community source material: retain its resident-facing fact and remove only promotional directory payload; review both retained and excluded details.
- Same subject reported in distant or unresolved areas: keep locations separate and never infer a shared district from terrain, aliases, or another item's context.
- One location reported at different times or in different states: preserve the material transition and distinguish unknown time from simultaneous reports.
- One Story or source fragment containing multiple claims: do not treat shared Story/support IDs as proof that the claims are duplicates.
- Repaired headline/body text with valid source IDs but missing or upgraded claims: coverage IDs alone cannot pass validation; disclose semantic checks that remain NOT_EVALUATED.

---

### Task 1: Make frozen eligibility explicit and keep selector labels nonbinding

**Files:**
- Modify: `src/publication/repository.py` — project eligibility/evidence-use facts from the frozen Event-First candidate revision into `snapshot_features`.
- Modify: `src/publication/selection.py` — persist selector exclusion labels as diagnostics and preserve Gate-eligible candidates for budget admission.
- Modify: `src/publication/digest_contracts.py` — centralize digest disposition names/constants if needed.
- Modify: `src/publication/policies.py` — bump digest selection semantics.

**Interfaces:**
- `PublicationCandidate.snapshot_features` gains a versioned `digest_eligibility` mapping with frozen source, `gate_keep`/`unknown`, publishable evidence count, excluded evidence count, and decision/version reference where available. For Event-First candidates, derive that the candidate passed the frozen Gate filter from the candidate snapshot itself; never query mutable current Gate state. For legacy candidates mark eligibility unknown and preserve them.
- Add transient `digest_disposition` metadata to selection decisions with `eligible_pending_budget` or `unknown_preserved`. Preserve any selector `HARD_EXCLUSION_REASONS` label in a separate nonbinding `selector_exclusion_suggestion` field. Task 2 resolves eligible candidates to final `selected` or `budget_deferred`; article decisions retain their current behavior.

- [x] **Step 1: Trace and close the frozen candidate boundary.** Inspect the candidate query and EventPayload. Require Event-First candidates to have the cutoff-matched Gate `KEEP`, local/direct-impact scope, and at least one `PUBLISH` evidence item in the frozen candidate revision. Confirm `event_payload.evidence_items` uses `PUBLISH`/`CONTEXT`/`EXCLUDE`; retain legacy/unavailable provenance as unknown without inventing exclusion grounds.
- [x] **Step 2: Project minimal eligibility provenance into candidate snapshot metadata.** In `repository.py`, add versioned frozen source, `gate_keep`/`unknown` status, PUBLISH/EXCLUDE evidence counts, and decision/version references where available. Event-First candidates missing the required PUBLISH evidence must be filtered before selection; legacy candidates remain preserved with unknown provenance. Keep source text and IDs frozen as currently defined.
- [x] **Step 3: Keep selector decisions as presentation suggestions in `selection.py`.** Ensure every Gate-eligible Story with PUBLISH evidence survives the zero-omission priority overlay, even when the selector returns `OMIT` with a hard-classification label. Retain the label/reason as diagnostic metadata for review; never drop mixed useful Story content on that basis. Preserve all existing article selection behavior.
- [x] **Step 4: Version the digest selection metadata contract.** Increment the supported selection semantic version only for the changed digest-specific decision metadata; do not change upstream Gate/triage retention or article selection.
- [x] **Step 5: Run focused static checks.** Run `python -m compileall -q src/publication/repository.py src/publication/selection.py src/publication/digest_contracts.py src/publication/policies.py` and `ruff check` on changed Python files. Do not run pytest. (compileall, `git diff --check`, and Ruff 0.16.9 passed.)

### Task 2: Compose facts conservatively and admit a feasible digest plan

**Files:**
- Create: `src/publication/digest_composition.py` — fact relations, composition units, budget admission, and candidate disposition result.
- Modify: `src/publication/digest_presentation.py` — attach the composition result to the existing `DigestPresentationPlan` and provide fact-level geography/time details.
- Modify: `src/publication/digest_coverage.py` — count admitted and deferred candidate/fact coverage separately from final selected-to-rendered coverage.
- Modify: `src/publication/generation.py` — build the composition map from frozen selected inputs before drafting.

**Interfaces:**
- `DigestFactRelationKind`: `SAME_FACT`, `UPDATE_OF`, `LOCAL_CONTRAST`, `RELATED_ONLY`.
- `DigestFactRecord`: stable `fact_id`, `story_ids`, `support_ids`, `rubric_id`, canonical subject/service, canonical and original location, effective/observed time (`datetime | None`), service state, epistemic kind, and source publication time.
- `DigestCompositionUnit`: stable `unit_id`, `rubric_id`, member fact/story/support IDs, canonical area key, priority, and allowed presentation relation edges. A unit groups evidence for writing; it does not itself prove that two facts are interchangeable.
- `DigestCandidateDisposition`: `selected` or `budget_deferred`, with reason, priority, estimated character cost, and any nonbinding selector-exclusion suggestion. Upstream hard exclusions are counted only if the frozen candidate snapshot exposes them.
- `DigestCompositionResult`: immutable `units`, `relations`, per-candidate `dispositions`, admitted Story/fact ID sets, estimated visible character count, and composition-policy version.
- `build_digest_composition(plan, cards, evidence, *, edition_slug, snapshot_at, max_chars, reserved_chars, include_statistics) -> DigestCompositionResult`.

- [x] **Step 1: Build fact records from existing required-fact and publication evidence.** Reuse the edition geography resolver and frozen snapshot/cutoff. Preserve aliases, unknown locations, epistemic status, and fact-level supports; partition multi-location Stories by fact. Do not use source overlap as a SAME_FACT shortcut.
- [x] **Step 2: Add conservative relation classification.** Assign `SAME_FACT` only with compatible subject, area, state, and effective-time evidence plus a strong normalized-fact match. Keep supported state changes as `UPDATE_OF`, same-area state differences as `LOCAL_CONTRAST`, and other topical affinity as `RELATED_ONLY`. Uncertain candidates remain separate and may be flagged for editorial review.
- [x] **Step 3: Create composition units and stable grouping order.** Related area/service facts remain together under their existing thematic rubric where practical. Order by reader priority, then coherent area/service order; never remap a fact's service domain to match a rubric.
- [x] **Step 4: Add deterministic pre-write budget admission.** Reserve title, rubric, separator, and configured statistics overhead. Admit grouped material by importance, urgency, current resident usefulness, and domain breadth with deterministic tie-breaks. Treat 12–16 topic bundles as a rich-day target only, not a quota. Persist every budget deferral with its reason and estimated cost, and selector hard labels as nonbinding diagnostics. If essential facts still cannot fit, return a specific budget failure before writing.
- [x] **Step 5: Freeze composition data in `DigestPresentationPlan`.** Keep one presentation plan as the single source of selected Story/fact membership; do not create a parallel competing plan. Expose both eligible-to-admitted dispositions and admitted-to-rendered coverage.
- [x] **Step 6: Run focused static checks.** Run compileall and Ruff on the new module and changed publication modules. Do not run pytest. (compileall, `git diff --check`, and Ruff 0.16.9 passed.)

### Task 3: Give the writer coherent evidence and make editing trace-safe

**Files:**
- Modify: `src/publication/digest_narrative.py` — writer input/output schema, prompts, parser, and narrative validation.
- Modify: `src/publication/digest_editor.py` — evidence-aware targeted repair and explicitly approved merge contract.
- Modify: `src/publication/narrative_contract.py` — align digest-specific contract text with composition units and flexible item structure; do not weaken article contracts.
- Modify: `src/publication/policies.py` — bump digest writer/editor prompt version.

**Interfaces:**
- Writer input adds `composition_units`, each carrying only its approved fact/story/support IDs, allowed relations, geography/time constraints, compact evidence packet, and rubric. Each output item names one or more units with the same configured rubric and a subset of their fact IDs; Python derives Story/support membership from only those facts. Subitems may partition unit facts, with exact once-only coverage. Zero-fact summary units remain explicit Story-level obligations and may be woven together within one rubric, with each Story represented exactly once. The writer cannot create memberships or move facts between rubrics.
- Every parsed writer item gets a deterministic draft-scoped `item_id` from its composition-unit IDs and position. `allowed_merges` is a list of authorization records containing a stable `merge_id`, exact source `item_ids`, `SAME_FACT` relation IDs, connected fact endpoints, source unit IDs, and the common rubric. A record may authorize three or more items only when its `SAME_FACT` edges connect them all. Cross-rubric merges are forbidden. `DigestEditor.polish_and_compress(..., allowed_merges: Sequence[DigestMergeAuthorization], affected_item_ids: Sequence[str], ...) -> DigestNarrativeDraft` may merge only those exact source items; output records their `source_item_ids` and preserves the full union of their claim atoms, facts, Stories, and supports. No unlisted item may be merged, and no carried claim may be dropped.
- A blank headline is valid; a short scan label is optional. The `emoji` field exclusively owns the rendered item icon.

- [x] **Step 1: Update structured writer schema and prompt.** Require each item to name an exact admitted composition unit and a fact subset; derive Story/support membership in Python. Writer-facing evidence includes PUBLISH items, with CONTEXT only for explicitly needed interpretation and EXCLUDE items omitted. Preserve natural prose, single-source attribution, time, exact location, and concrete practical details. Align the digest-specific narrative contract. Remove rigid per-item sentence and character quotas that force an inventory style; keep only the whole-message budget. State that questions, sarcasm, and source/channel mechanics are not reader-facing facts. Do not add an LLM per Story or a new model.
- [x] **Step 2: Validate writer item membership against the frozen composition map.** Reject unknown facts/stories/supports, unsupported relation transitions, geography/rubric moves, duplicate fact presentation, and coverage loss. Derive Story/support IDs only from the item’s explicit fact subset; validate each zero-fact Story obligation exactly once. Do not accept identifier union as proof that visible prose states a fact.
- [x] **Step 3: Make the editor's merge contract explicit.** Add stable input item IDs and approved merge authorization records. Each record lists exact source item IDs and a connected graph of frozen `SAME_FACT` relations; all source items must share one rubric. Include only the relevant evidence packet, preserve the full union of claim atoms without widening support mapping, and reject unapproved item/fact IDs, cross-rubric merges, extra merged items, or missing claims.
- [x] **Step 4: Keep headings optional and normalize icon ownership.** Remove prompt requirements that every item must carry an emoji heading. Ensure headline text contains no duplicate leading icon while preserving meaningful numerals and punctuation.
- [x] **Step 5: Run focused static checks.** Run compileall and Ruff on changed narrative/editor/policy modules. Do not run pytest. (compileall, `git diff --check`, and Ruff 0.16.9 passed.)

### Task 4: Use one bounded repair and validate the canonical rendered artifact

**Files:**
- Create or extend: `src/publication/digest_quality_diagnostics.py` — check results with `PASS`, `FAIL`, and `NOT_EVALUATED`, separating publication blockers from style observations.
- Modify: `src/publication/renderers.py` — construct a canonical rendered digest artifact including complete post text and formatting/entity representation.
- Modify: `src/publication/generation.py` — unify validation and prose repair into one maximum repair call and validate after final rendering.
- Modify: `src/publication/facade.py` and `scripts/preview_digest.py` — return and print the same canonical artifact used for delivery; add optional ISO-8601 `--as-of` for reproducible snapshots.
- Modify delivery adapter only if required: `src/publication/adapters.py` or the actual digest delivery call site found during implementation.

**Interfaces:**
- `RenderedDigestArtifact`: canonical visible text, Telegram parse mode/entities, visible character count after entity parsing, UTF-16 entity offsets, and stable content hash. Add `render_grouped_digest_artifact(...) -> RenderedDigestArtifact`; keep a compatibility wrapper only for callers that still require the old tuple.
- `audit_rendered_digest(artifact, draft, evidence, presentation_plan, coverage_trace) -> DigestQualityAudit` evaluates the whole title/rubric/body/configured-statistics post, not just item text.
- A required check without a reliable implementation reports `NOT_EVALUATED`; it cannot be represented as passed. `is_publishable` depends only on completed mandatory hard checks and contains no blocking `FAIL`.

- [x] **Step 1: Render the full post once into `RenderedDigestArtifact`.** Include title, thematic blocks, separators, and statistics only when configured. Do not include a City Situation panel, duplicate lead, or synthetic footer. Preview and delivery use the same artifact and no post-render rewrite.
- [x] **Step 2: Audit the exact rendered result.** Check the 4096 Telegram ceiling after entity parsing, UTF-16 offsets, duplicate emoji/heading, source/channel mechanics, repeated facts, directory dominance, repeated generic caveats, unrepresented required facts, and unsupported high-risk relations. Tie every finding to visible text and evidence IDs. Mark general semantic entailment `NOT_EVALUATED` when no reliable checker exists.
- [x] **Step 3: Consolidate writer validation and quality repair.** Replace the current possibility of separate narrative-validation and prose-quality editor calls with one bounded repair using the union of blocking findings. Revalidate evidence, relation constraints, coverage, geography, render length, and the final artifact after repair.
- [x] **Step 4: Fail closed on blocking findings.** A missing provider, timeout, malformed edit, unresolved blocker, incomplete material-fact coverage, or budget overflow returns a clear failed preview. Never publish raw concatenation or deterministic fallback prose as recovery.
- [x] **Step 5: Make preview reproducible.** Add `--as-of YYYY-MM-DDTHH:MM:SS+00:00` to `scripts/preview_digest.py`, thread it through `build_publication_preview(snapshot_at=...)`, and print exactly the canonical artifact without sending it.
- [x] **Step 6: Run focused static checks.** Run compileall and Ruff on changed renderer/generation/facade/preview/diagnostics modules. Do not run pytest. (compileall, `git diff --check`, and Ruff 0.16.9 passed.)

### Task 5: Version and persist outcome metadata for honest review

**Files:**
- Modify: `src/publication/generation.py` — persist plan, dispositions, diagnostics, repair, and outcome metadata.
- Modify: `src/publication/digest_coverage.py` — report eligible, deferred, admitted, and rendered denominators.
- Modify: `src/publication/policies.py`, `src/publication/digest_quality_diagnostics.py`, and `src/publication/digest_composition.py` — version all changed semantics.
- Create: `data/evaluation/2026-09-28-digest-editorial-comparison.json` — compact fixed-case manifest with frozen preview/run references and human-readable scoring notes, not a unit-test fixture.

**Interfaces:**
- Publication metadata stores only version IDs, candidate disposition summaries, relation/group membership IDs, pre/post finding codes, required-coverage outcome, canonical rendered-text hash/count, repair attempt/outcome, and generation latency/failure reason. Frozen upstream exclusion counts are reported only when available; selector labels remain suggestions, not exclusions. Keep raw provider prompts, credentials, and redundant full source copies out of metadata.
- The editorial manifest identifies input snapshot/cutoff, edition profile version, prompt/semantic versions, baseline preview reference, and case tags; keep its source material minimal and non-sensitive.

- [x] **Step 1: Bump digest semantic/prompt/diagnostic versions.** New preview runs freeze all changed behaviors; existing publication snapshots and article versions remain unchanged.
- [x] **Step 2: Persist disposition and audit metadata.** Ensure selected-to-published coverage is 100%, report preselection deferrals separately, and retain attempt timing/status so failures and latency are visible.
- [x] **Step 3: Capture a reproducible comparison manifest.** Use the available Berdyansk preview 86 and frozen 2026-09-28 cutoff if the persisted input is available. Compare a 12-hour case to its prior text and a rich 24-hour case under the same frozen source cutoff/config. If exact historical inputs/configuration cannot be recovered, mark that comparison NOT_AVAILABLE and use an explicitly labeled reproducible current snapshot; do not claim old/new parity. (Historical preview 86 text/cutoff/config and the failed rich 24-hour body are unavailable in the workspace; the manifest records this without claiming parity. The deployed preview reference will be reported with its outcome.)
- [ ] **Step 4: Review actual output side by side.** Review factual fidelity, scanning ease, useful breadth, repeated claims, geography/time clarity, natural Russian, retained concrete detail, excluded/deferred material, and operational failure/repair/latency. Fix critical factual/geographic defects before release; do not treat an automated clean flag or one preview as proof of editorial quality.
- [x] **Step 5: Run final static verification.** Run `git diff --check`, compileall, Ruff 0.16.9 checks and formatting, and mypy 2.3.1 on `src/`. Do not run tests. (compileall, JSON parsing, `git diff --check`, Ruff, and mypy passed.)

### Task 6: Commit, deploy to `dev`, and run server previews

**Files:**
- Include in final commit: all approved implementation files plus the ignored specification and this ignored plan using `git add -f` for those two documentation files.
- Exclude: `docs/superpowers/.DS_Store`.

**Interfaces:**
- Deployment: push the final commit to the existing `dev` branch; `.github/workflows/deploy-dev.yml` builds and deploys the `dev` image to the server.
- Preview: from `/home/opc/Telebrief`, execute the repository's canonical no-delivery script inside the deployed app container: `docker compose exec -T telebrief-app python scripts/preview_digest.py --edition berdyansk --hours 24`.

- [x] **Step 1: Review the complete diff and repository state.** Confirm only intended files are staged and `.DS_Store` is untouched.
- [ ] **Step 2: Create the one final commit on `dev`.** Include all implementation and design/plan artifacts together; use a commit message describing digest composition/quality.
- [ ] **Step 3: Push `dev` and wait for the existing deployment workflow.** Confirm image revision and healthy app/worker containers before generating previews.
- [ ] **Step 4: Run a 24-hour server no-delivery preview.** Return its exact text and diagnostics to the user. Also run a 12-hour preview only if needed to isolate the previously observed 12-hour duplicate/wording defects. Do not use Telegram send APIs.
- [ ] **Step 5: Report outcome honestly.** Show the final text, character count, selected/deferred counts, upstream hard-exclusion counts only when available, audit statuses including `NOT_EVALUATED`, selector-label suggestions, repair usage, and deployment revision. If generation fails, show the specific failure and do not call it successful.

## Dependency order

Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6. The change is one editorial workflow with shared frozen interfaces, so subagents should own sequential bounded tasks and review each completed task before the next dependent interface is changed. Keep no more than four agents active, including implementation and review roles.
