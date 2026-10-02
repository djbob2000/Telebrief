# Article Pipeline Failure and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Event-First evening articles coherent, geographically faithful, recoverable within a bounded run, and inspectable after a failed frozen preview.

**Architecture:** Strengthen the existing `ArticleCompositionPlan`, quality checks, targeted `ArticleEditor`, finalizer, and frozen-run preview path. Keep a single writer invocation, at most two editor invocations, and a generation deadline; keep evidence safety separate from editorial polish and never add filler or a second planner.

**Tech Stack:** Python 3.14, asyncio, existing article composition/finalization modules, YAML configuration, frozen-run preview CLI.

**Spec:** `docs/superpowers/specs/2026-10-01-article-pipeline-failure-architecture-design.md`

## Global Constraints

- Keep `AGENTS.md` §0 authoritative for city-life long-read quality, single-source community reports, microdetails, exact quotes, geography, and fail-closed factual safety.
- Do not add an LLM planner, a writer regeneration loop, raw-fragment fallback, filler, corroboration gate, or 100% prose-coverage gate.
- Limit generation to one writer stage, two editor stage invocations, and the configured shared 1,200-second deadline; report provider attempts separately.
- The composition map retains every visible coverage-plan Story and uses compact IDs; evidence packets remain canonical for exact facts and provenance.
- Keep full rejected article text in explicitly requested preview output only, never in production DB rows or ordinary logs.
- Do not modify digest, ingestion, Gate, Event clustering, automatic delivery, or historical publications.
- Follow the user's no-TDD instruction; do not create a separate worktree or commit per task. Integrate on `dev` and commit/push/deploy once at the end as previously requested.

## Review Focus

- A short useful single-source report with unknown district stays eligible and cannot inherit a neighboring Story's address.
- Separate district reports may share a service chapter but cannot acquire inferred proximity, common cause, or city-wide scope.
- Two consecutive direct speech quotes joined only by list punctuation are caught as a real quote roll; a count of quoted business/place names is not a speech roll.
- An editor patch must never be paired with an assessment for another structured draft or support mapping.
- Rejected, timed-out, and no-draft frozen previews must not publish or overwrite an earlier artifact as if it were a new result.

---

### Task 1: Clarify the composition roadmap and writer prompt

**Files:**
- Modify: `src/publication/article_composition.py`
- Modify: `src/publication/article_writer_context.py`
- Modify: `src/article_generator.py` (composition metadata/version only)

**Interfaces:**
- Consumes existing `ArticleCoveragePlan`, `ArticleCompositionPlan`, and Story packets.
- Produces one compact, versioned roadmap with exact visible Story membership, thematic lines, relation groups, preferred order, and only the geography/time boundaries needed to keep reports distinct.

- [ ] Inspect current serialization and prompt rendering; record how every visible Story maps to one group and how lines currently reorder packets.
- [ ] Update roadmap language so same-service reports can share a chapter while only a supported relation group can be synthesized as related. Permit the writer to adjust chapter order for readable prose while retaining thematic/geographic boundaries.
- [ ] Preserve one membership per visible Story, compact metadata, existing full/compact packet fitting, and source packet authority for exact claims, times, and attribution. Add explicit version to composition metadata/prompt hash.
- [ ] Manually inspect roadmap rendering for a sample containing distinct locations, an ambiguous multi-area Story, a street alias, and an independent Story. Confirm all visible Story IDs remain once and unrelated reports have separate evidence groups.
- [ ] Run `ruff check src/publication/article_composition.py src/publication/article_writer_context.py src/article_generator.py` and `ruff format --check` on those files.

### Task 2: Centralize article finding policy without weakening safety

**Files:**
- Create: `src/publication/article_quality_policy.py`
- Modify: `src/publication/article_quality.py`
- Modify: `src/publication/article_finalization.py`
- Modify: `src/publication/article_editor.py`
- Modify: `AGENTS.md` §0.6–0.7 and §0.9

**Interfaces:**
- `article_quality_policy.py` owns one explicit mapping for reader-quality finding code → class, severity, repair scope, and publication effect. Evidence Boundary findings remain governed by the existing validator.
- The editor, finalizer, and preview read the same policy; unknown reader-quality codes are surfaced as a policy/configuration error, never silently treated as safe.

- [ ] Inventory every current reader-quality finding and whole-draft code; assign each a documented class and repair scope. Preserve material geography/service contradictions as blockers; distinguish quote-count heuristic (`QUOTE_ROLL_PARAGRAPH`, repair) from consecutively dumped speech separated only by list punctuation (new structural finding).
- [ ] Keep supported organization/place names out of direct-speech detection. Detect actual quote rolls using speech spans, quote type, and intervening prose, not a quote count alone.
- [ ] Treat missing major storylines as an explicit `MISSING_DEVELOP_STORY` readiness diagnostic. Attempt repair only within the existing editor budget; if no suitable unit exists, report editorial acceptance incomplete without adding a coverage veto or filler.
- [ ] Route editor and finalizer decisions through the policy. Keep exact quoted words immutable and retain hard Evidence Boundary validation for all factual changes.
- [ ] Update `AGENTS.md` to state the implemented distinction while preserving its no-chat-roll, geography, attribution, microdetail, single-source, and no-filler requirements.
- [ ] Manually review the finding inventory against `AGENTS.md` §0 and run Ruff check/format on changed Python files.

### Task 3: Bound editing, validation reuse, and generation time

**Files:**
- Modify: `src/publication/article_editor.py`
- Modify: `src/article_generator.py`
- Modify: `src/publication/article_finalization.py`
- Modify: `src/config/schemas/publication.py`
- Modify: `src/config/parsers/publication.py`
- Modify: `config.yaml`

**Interfaces:**
- Add `article_generation_timeout_seconds: int = 1200` to `ArticleConfig`; reject non-positive configuration.
- Editor stage cap is `min(configured_article_editor_max_attempts, 2)`; report editor invocations separately from provider transport attempts.
- Each assessment is paired with its full `StructuredArticleDraft` and a fingerprint covering context, supports, material projection, geography profile, length/coverage settings, and policy/validator versions.

- [ ] Enforce the editor cap where the generator calls `ArticleEditor`; configuration values above two cannot increase it.
- [ ] Make each editor pass transactional: reject independently unsafe unit patches, validate the combined candidate, quarantine attributable new blockers and validate once more, otherwise restore the previous draft and matching assessment. Keep safe resolved changes in a non-publishable checkpoint when other old blockers remain.
- [ ] Add the positive 1,200-second setting to schema/parser/config. Start one deadline at frozen-input generation; all provider calls, retries, queue waits, and editor attempts use remaining time. Ensure timeout propagates as timeout and cannot enter deterministic fallback. Stop optional polish at deadline; only return a checkpoint already assessed safe and readable.
- [ ] Reuse finalization assessment only when full structured draft and all validation inputs match; any claim/support map, text, geography, material projection, or policy change requires revalidation. Build claim trace from the accepted final draft.
- [ ] Inspect call paths to ensure one writer stage and no planner/regeneration call. Run Ruff check/format and relevant MyPy scope if configured.

### Task 4: Return inspectable frozen-preview outcomes

**Files:**
- Modify: `src/publication/article_preview.py`
- Modify: `src/article_generator.py` (preview-only candidate capture interface)
- Modify: `scripts/preview_article.py`
- Modify: `AGENTS.md` §0.7

**Interfaces:**
- Frozen preview returns typed `accepted | rejected | failed` status, optional candidate, and safe diagnostics; production rejection remains an exception.
- A preview-only in-memory capture carries the last candidate/checkpoint through rejection without adding article prose to persisted metadata.
- `--run-id` writes requested article/diagnostics files on both success and failure; rejected/failed preview exits non-zero.

- [ ] Capture draft revisions at writer, editor, and finalization boundaries with the paired assessments and stage. Expose capture only to frozen preview.
- [ ] Convert successful and rejected/timeout frozen replay into one diagnostic schema with allowlisted codes, timings, call counts, coverage, composition, versions, and prompt/context length/hash.
- [ ] Make CLI write explicit rejected labels and diagnostics before a non-zero exit. For no candidate, write a no-draft result; never leave stale output looking like current output. Preserve no-delivery behavior.
- [ ] Manually review CLI help and exercise artifact handling with a frozen rejection and a no-draft path without sending a live article. Run Ruff check/format and relevant MyPy scope if configured.

### Task 5: Review run 257 once, deploy, and finish on `dev`

**Files:**
- Review: generated frozen preview and diagnostics only; no source edits unless an acceptance defect is found.

**Interfaces:**
- Input: production run 257 sealed input, replayed through the deployed implementation using `scripts/preview_article.py --run-id 257 --output … --diagnostics-output …`.
- Output: one reviewed article or conspicuously rejected candidate, compact diagnostics, and deployment/commit record.

- [ ] Complete a code review against this plan and `AGENTS.md`; inspect `git diff` and preserve unrelated `.DS_Store` change.
- [ ] Run focused static checks and deterministic no-provider preflight; do not start another paid writer replay to chase a lucky output.
- [ ] Commit all task changes together on existing `dev` branch, force-add the approved spec and plan because `docs/superpowers/` is ignored, and leave the unrelated `.DS_Store` unstaged.
- [ ] Push/deploy through the existing server path. After the no-provider preflight confirms the complete compact dossier fits, run one writer-backed frozen replay of run 257. Inspect title, lead, chapter order, geography, microdetails, quote handling, outcome status, diagnostics, provider attempts, and wall time.
- [ ] Do not send or publish a live article. Report the replay text and actual findings to the user.

#### Run 257 status update (2026-10-02)

The first frozen replay failed during writer-input materialization before any provider call. A read-only inspection identified the original support-projection contract mismatch: one Story had six non-question `PUBLISH` supports, all contact/CTA-only after conservative projection, and none had citable text. The follow-up fix omits empty projected supports and suppresses a Story for this reason only when every non-question `PUBLISH` support is empty; mixed Stories remain eligible. One corrected replay also failed before any provider call because the complete descriptive dossier measured 589,303 characters against the 500,000-character ceiling. A deterministic no-provider replay captured that exact error. The production packetized writer path currently has no complete compact retry despite the approved spec requiring one. The remaining work preserves the 500,000-character ceiling, adds the compact full-inventory path and numeric failure diagnostics, then performs a no-provider preflight before the single writer-backed replay. No article or editor provider call has occurred for run 257.

Ruling: Keep the 500,000-character ceiling and add a complete compact serialization path rather than raising the limit; the approved spec explicitly sets this cap and requires full/compact fitting. Cost if wrong: an unusually large dossier may still fail pre-writer if its complete compact encoding remains over the cap, until model-specific context capacity is verified and the spec is revised.

Review note: The independent reviewer flagged documentation changes outside the implementer's four-source-file assignment. Ruling: retain them as root-owned changes because `AGENTS.md`, the approved spec, and this plan need to state the compact behavior and actual run-257 failure accurately; the implementation agent did not edit docs. Cost if wrong: duplicated or overly specific policy text could need cleanup in a later documentation pass.

---

## Self-review

- Spec coverage: composition boundaries and membership (Task 1); severity/quote/readiness policy plus `AGENTS.md` (Task 2); editor patches, deadline, and validation identity (Task 3); rejected/no-draft previews (Task 4); one stochastic acceptance replay and rollout (Task 5).
- Dependency order: Tasks 1 and 2 have disjoint ownership and may run concurrently. Task 3 consumes the quality policy from Task 2. Task 4 consumes Task 3's capture interface. Task 5 runs only after integration.
- No new automated test files or TDD steps are included, following the user's explicit instruction. Static checks and manual contract review are specified; the single server frozen replay is the agreed end-to-end verification.
- The plan preserves partial prose coverage as diagnostic and requires every visible Story to remain on the map. It adds no subjective editorial eligibility filter; an all-empty material projection is the only new writer-composition suppression case.
- User previously chose subagent-driven execution, direct integration on `dev`, no TDD, a single frozen dry-run, and committing only at the end; this plan retains those choices.
