# Article Editor and Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. User overrides: directly in `dev`, no TDD, no new tests or pytest, one integration commit at the end.

**Goal:** Подготовить статью до существующего редактора, сохранить полезные сообщения и конкретные детали, принимать решение по точному итоговому тексту и показывать этот текст в серверном preview.

**Architecture:** Существующий checkpoint связывает неизменяемый draft с оценкой его фактов и качества. Разрешённая подготовка выполняется перед редактором; finalizer становится решением без переписывания прозы. Полные источники для каждой правки, наблюдаемые исходы и приоритеты второго pass улучшают существующие два вызова редактора.

**Tech Stack:** Python/asyncio, existing Event-First models and validators, CityContextResolver/YAML profile, Ruff, mypy, pre-commit, Docker Compose and the existing GitHub deployment workflow.

**Spec:** [Approved specification](../specs/2026-10-02-article-editor-finalization-design.md). Approved by the user on 2026-10-02, including organization-name quotes for `возле «Илеара»`.

**Status:** Approved by the user on 2026-10-02; implementation in progress. Progress/review ledger: `.superpowers/sdd/2026-10-02-article-editor-finalization/progress.md`.

## Global Constraints

- Read AGENTS.md, especially §§0.1 and 0.3–0.9; its product contract wins over historical plans. A useful single-source report remains publishable with honest attribution.
- Work directly in `dev`; preserve unrelated `docs/superpowers/.DS_Store` and concurrent changes. No new branch or worktree.
- Subagent-driven execution, at most four active agents including root; subagents do not delegate, commit, push or deploy.
- No TDD, new test files or pytest. Use static checks, source review and in-memory executions of existing functions, without provider calls until the final server preview.
- One logical writer; at most two nonempty responses only for existing definitively-unusable next-slot recovery. At most two existing editor calls; the shared generation deadline remains 1200 seconds.
- Writer dossier ceiling remains 999 999 characters with existing complete/compact lossless encoding. Do not change provider slots, request parameters, `.env` model allowlist or completion allowances.
- Text-patch limits remain per unit: 64 support packets, 32 000 characters total, 4 000 per packet. Structural limits apply to the required batch union. Do not reinterpret 32 000 as a new per-call text limit.
- Coverage, missing detail and missing DEVELOP are readiness diagnostics, never numerical article vetoes. No new planner, writer, fact-checking model stage or deterministic source-fragment fallback.
- Exact retained direct quotes are immutable. Organization-name quotes are typography, not direct speech. Names/roles alone do not prove current service states.
- Scope excludes ingestion, Gate/Analysis, Story eligibility, digest, delivery, DB migrations and dependency updates.
- One final integration commit/push/deployment and one server frozen preview of run 257 in the existing app container. No paid retries for a lucky result; no publication or delivery from preview.

## Review Focus

1. A single resident report with empty/mixed roles: preserve the fact and attribution; never invent residents, official confirmation or a district. Tasks 1 and 5 own the in-memory inspection.
2. A store landmark in inflected form or inside actual quoted speech: quote the name without rewriting the spelling, counting it as speech or inventing proximity/status. Task 2 owns the inspection.
3. Required packets exceed a budget or the editor returns malformed/no-op output: do not pretend the repair was attempted successfully; preserve original evidence and give deferred work a chance in pass two. Task 3 owns the inspection.
4. Error after a successful editor pass, deadline expiry or user cancellation: retain the correct checkpoint, distinguish failed/unassessed from rejected, and start no additional model request. Tasks 4–5 own the inspection.
5. Different frozen run, changed input/profile/rule version, or a rejected final candidate: never attach an earlier or foreign assessment; show the exact decided draft and safe final issues. Tasks 1 and 4 own the inspection.

## File Ownership and Execution Order

| Owner | Exclusive implementation paths | Deliverable |
| --- | --- | --- |
| Worker A | `src/publication/article_finalization.py` | Shared assessment interfaces, closed preparation, gate-only finalization |
| Worker B | `src/publication/article_editor.py` | Complete evidence per unit, parser/outcomes, bounded scheduling |
| Worker C | `src/publication/article_quality.py`, `src/publication/article_quality_policy.py`, `src/publication/article_validator.py`, `src/city_context.py`, `data/city_profiles/berdyansk.yaml`, `docs/city_profiles/berdyansk-sources.md` | Name typography and narrow kitchen/colloquial classification |
| Root | `src/article_generator.py`, `src/publication/article_preview.py`, `src/publication/article_models.py`, `scripts/preview_article.py`, `AGENTS.md`, this plan/spec and release | Orchestration, preview, integration and final review |

The shared interfaces below are fixed before handing off. Start Tasks 1–3 concurrently with disjoint ownership; Worker A consumes Task 2's name helper. Workers can implement against declared interfaces, but verify imports after their dependencies are available. Root can prepare Task 4 against those interfaces. Do not edit another owner's path; request a change from its owner. After each worker freezes its files, review its deliverable before integrating it. Root self-reviews this plan; code review during execution is independent.

Keep shared checkpoint/preparation helpers in `article_finalization.py`: its existing import direction does not require a new module. Do not create a second assessment system or perform a broad file split.

### Task 1: Preparation, exact assessment ownership and gate-only finalizer

**Owner:** Worker A. **File:** Modify `src/publication/article_finalization.py`.

**Interfaces:**

- Preserve `ArticleAssessmentCheckpoint`'s existing positional fields; append `source_identity: str | None = None`. Extend `matches(draft: StructuredArticleDraft, input_fingerprint: str, *, source_identity: str | None = None) -> bool` to check exact draft, fingerprint and source identity.
- Extend `article_assessment_input_fingerprint(context, coverage_plan, config, length_profile, material_projection, place_resolver, *, source_identity: str | None = None) -> str`; include source identity and keep existing full-content/version/opaque-input semantics.
- Add `ArticleAssessmentInputObserver = Callable[[str], None]`: trusted producers announce the fingerprint calculated from current assessment inputs before emitting the corresponding assessed checkpoint.
- Add `prepare_article_draft(draft: StructuredArticleDraft, *, context: ArticleEditorialContext, material_projection: ArticleMaterialProjection | None, place_resolver: Any | None) -> StructuredArticleDraft`.
- Add `async assess_article_draft(draft: StructuredArticleDraft, context: ArticleEditorialContext, *, coverage_plan: ArticleCoveragePlan | None, editorial_config: PublicationEditorialConfig | None, length_profile: ArticleLengthProfile | None = None, material_projection: ArticleMaterialProjection | None = None, place_resolver: Any | None = None, source_identity: str | None = None, input_observer: ArticleAssessmentInputObserver | None = None) -> ArticleAssessmentCheckpoint`. Run existing validation, quality and coverage work off the event loop; no provider calls. When plan is absent, quality/coverage keep existing compatible empty/None behavior.
- Preserve `ArticleFinalizer.finalize` parameters and result type; add optional keyword `source_identity` and `assessment_input_observer` with the types above. Loose validation/quality arguments remain diagnostic and cannot authorize reuse.

- [ ] Implement the input identity/checkpoint extensions and shared assessment function; announce expected input fingerprint from actual inputs, not by reading it from a candidate checkpoint. Include projected exclusion accounting in coverage without creating a coverage gate. Bump `ARTICLE_ASSESSMENT_VALIDATOR_VERSION` for the changed validator semantics supplied by Task 2.
- [ ] Implement the closed preparation order: content-preserving formatting; supported organization-name typography and existing unknown-private-sector clarification; unambiguous author attribution; rebuild changed claims/mapping; assess later through the common function. Reuse narrow helpers, consuming Task 2's `supported_organization_name_mentions`.
- [ ] For attribution, require every role supporting the affected assertion to be exactly `community` before saying «жители». Empty/mixed/other roles permit only a meaning-preserving neutral message attribution; ambiguous syntax remains for the editor. Skip direct-speech interiors; do not change questions, times, places, amounts, promises or completion states. Re-ground changed assertions only to their pre-authorized eligible supports; keep other claims/provenance intact. Preparation is idempotent.
- [ ] Turn `finalize` into an immutable gate. Reuse a matching checkpoint, otherwise assess current text. Emit `finalization_candidate` before assessment when needed and the authoritative assessed `finalization` checkpoint before accepted/rejected terminal actions. Assessment failure leaves the candidate explicitly unassessed and propagates as failure; timeout/cancellation do not call a fallback.
- [ ] Remove automatic quote stripping, unsupported-paragraph/source replacement, semantic/exact deduplication, orphan merging, duplicate-heading cleanup, heading/topic/title substitution and repeated post-editor normalization from this path. Remove unused private helpers after checking all callers. Retain source-backed quality findings for existing editor repair. Keep strict safety and structural quality gates, trace construction and existing return shape; no article eligibility/depth-based deletion.
- [ ] Inspect in memory: valid one-source report; unknown private-sector district; all-community versus mixed/empty roles; immutable direct speech; second preparation no-op; mismatching text/input/source identity; unsafe final candidate; error during assessment. Check all retained supported numbers/places/times and that finalization does not change `render_markdown()` or grounded provenance.
- [ ] Run focused Ruff/format checks on owned file after Task 2's helper exists. Hand off interfaces, changed paths, safe inspection results and remaining risks; freeze the file.

### Task 2: Organization-name typography and correct finding classification

**Owner:** Worker C. **Files:** Modify the quality/policy/validator/resolver/profile paths listed above.

**Interfaces:**

- Add `CityContextResolver.commercial_name_mentions(text: str) -> tuple[ResolvedEntity, ...]`, a separate lookup of profile commercial landmark names/aliases. Return exact `matched_text`, stable profile ID and `object_type="landmark"`; no inferred areas or current states. Keep existing `resolve()` behavior unchanged for ingestion/digest callers.
- Add `supported_organization_name_mentions(text: str, support_ids: Sequence[str], context: ArticleEditorialContext, place_resolver: Any | None) -> tuple[tuple[str, tuple[int, int]], ...]` in `article_quality.py`. Combine existing supported provider spans and supported commercial-landmark spans; require an eligible cited support containing the entity and preserve exact prose offsets/spelling. Source-only explicit organization syntax can identify a name without requiring a second source; ambiguous bare capitalized words remain unchanged.
- Register `COLLOQUIAL_AUTHOR_PROSE` with finding class `author_register`, severity `repair`, scope `unit`, effect `repair_recommended`. Preserve actual `CHAT_KITCHEN_LEAK` checking; it no longer includes ordinary slang lexemes.

- [ ] Add a profile commercial-name lookup without adding city-specific strings to production logic. Use profile data and exact alias matching with word boundaries; handle overlapping/ambiguous matches conservatively. Do not classify streets, districts, parks or generic landmark nouns as shop names merely because they are capitalized.
- [ ] Record user-confirmed `Илеар`/`Илеара` as a shop-name entry with `type: commercial`, without a guessed address/area. The existing profile has `Илэар` under supermarket «Зеркальный»: do not assert that the user-confirmed spelling identifies that same business without supporting identity evidence. Record the user's 2026-10-02 clarification in profile source notes; do not publish that note as news.
- [ ] Feed supported organization spans into existing name-quote exclusion and direct-speech counting. `возле «Илеара»` is name typography, while «Илеара сегодня не работает» used as quoted speech stays speech. Profile recognition alone never proves a current service state. Preserve provider handling and exact words inside retained speech. Add no mandatory quote-count or typography veto.
- [ ] Narrow validator kitchen matching to real references to source/collection mechanics. Move ordinary slang detection to the registered quality finding only for author prose outside genuine direct speech. Unknown policy codes still error. Bump `ARTICLE_READER_QUALITY_VERSION`; preserve numbers, names, quotes, epistemic status, causes and geography checks.
- [ ] Inspect in memory with one eligible source: unquoted/quoted `Илеара`, already quoted provider, store inside direct speech, street/area control, absent supported name, alias overlap, real source-mechanics reference, colloquial author wording and colloquial exact quote. Check unchanged spelling, correct speech counts, nonblocking typography/register and no invented district/service state.
- [ ] Run focused Ruff/format checks and load the edited YAML through the existing profile loader without network calls. Hand off the helper and versions; freeze owned files.

### Task 3: Complete repair units, typed outcomes and useful second pass

**Owner:** Worker B. **File:** Modify `src/publication/article_editor.py`.

**Interfaces:**

- Keep `edit_draft`'s tuple return and current arguments; add `source_identity: str | None = None` and `assessment_input_observer: ArticleAssessmentInputObserver | None = None`. Use Task 1's assessment/matching interface on every changed or rolled-back candidate.
- Expose `last_unit_outcomes: list[dict[str, Any]]` and `last_pass_outcomes: list[dict[str, Any]]`, reset per invocation. Unit fields: pass index, current registered unit ID, enum status/reason, issue codes, required/shown support counts and base/result fingerprints. Pass fields: index, enum parse/selection outcome, requested/applied/deferred counts, unresolved target counts and elapsed time. No attempted text, claim text, raw response, heading or source payload.
- Unit statuses: `applied`, `no_change`, `not_returned`, `rejected`, `rolled_back`, `deferred_budget`, `missing_required_support`. Pass reasons include `unparseable_response`, `invalid_response_shape`, `unknown_unit_ids`, `unlocalizable_finding`, `no_eligible_units`, `no_measurable_progress`, `deadline_exhausted`. Unknown IDs are counted, not echoed as arbitrary strings.

- [ ] Build each unit's mandatory support set from cited IDs, its claim IDs and finding IDs, plus authorized structural sources. Require complete eligible projected packets and necessary parent/framing fields. Remove truncation presented as complete context; skip the unit with an enum reason if a required packet is absent/hidden/ineligible or cannot fit. This does not generate a new unsupported-source finding.
- [ ] Keep current packet limits at their existing scopes: per text unit versus structural-batch union. Apply the existing full prompt/context guard without a new 32 000-character call-wide quota. Preserve complete read-only orientation for structural work; skip structural mode safely if complete context cannot fit.
- [ ] Sort first-pass targets: blocking factual issues and real kitchen leaks; misleading/composition repairs; cosmetics. Tie-break by title/lead then current section/paragraph order. In pass two, within each priority class, send never-attempted deferred units before partially improved units and no-progress repeats. Re-localize against current draft/registry; stale positional IDs never authorize edits.
- [ ] Preserve tolerant envelope parsing but distinguish malformed/wrong-shaped output, unknown units, absent requested patches and unchanged patches. Report every considered unit and pass outcome, including no eligible units without making an empty provider call. Send compact first-pass feedback into the second existing call.
- [ ] Preserve server-side re-grounding to only prompt-authorized supports, per-unit text quarantine, exact quote protection, fingerprint-bound MOVE and atomic RECOMPOSE batch rollback. Full-candidate assessment errors restore the exact pre-operation checkpoint. Assign `last_assessment` only to a successfully assessed exact candidate; retain the last such checkpoint before propagating errors/timeouts.
- [ ] Inspect in memory using existing functions and in-memory fake provider responses: missing packet; 4 001-character packet; 65 required supports; 32 001 total per unit; multiple individually fitting units; malformed/empty response; unknown ID; no-op; unknown/global finding; deferred blocker versus repeated blocker; unauthorized structural operation; assessment exception. Check unchanged original source IDs, two-call ceiling, correct rollback and distinct outcomes.
- [ ] Run focused Ruff/format checks after Task 1's interface exists. Hand off safe metadata examples without prose, actual call-count inspections and changed paths; freeze the file.

### Task 4: Frozen input ownership and honest preview outcomes

**Owner:** Root. **Files:** Modify `src/publication/article_preview.py`, `scripts/preview_article.py`. Depends on Task 1 interfaces; source lookup remains read-only.

**Interfaces:**

- Initialize `_MemoryCheckpointObserver` with caller-owned `source_identity: str`; add `bind_assessment_inputs(input_fingerprint: str) -> None` for the trusted producer callback. Accept an assessment only if exact draft, independently bound expected fingerprint and caller source identity match. Missing expectation/mismatch leaves assessment unavailable and increments safe mismatch counts.
- Derive source identity before provider calls as SHA-256 over canonical run ID, edition, snapshot/timezone, ordered sealed-input/revision/claim/fragment identifiers and adapted article context. Only hash/IDs/counts leave memory; full source text does not enter JSON. Pass this identity and input-binding callback through Task 5's generator keywords.
- Preserve `build_article_preview_from_run` and `ArticleRunPreviewOutcome` interfaces/statuses. Preserve latest candidate separately from last assessed checkpoint; `failed` may expose an explicitly unassessed candidate, never attach the older assessment to it.

- [ ] Compute caller-owned source identity after the existing timezone correction; do not derive it from a returned checkpoint. Capture trusted assessment-input bindings and all exact candidates; reject foreign or stale pairings in memory without broadening validation rules.
- [ ] Choose the authoritative finalization checkpoint for accepted/rejected results. Accepted frozen Event-First preview requires an exact matching final assessed checkpoint; remove its unchecked returned-text fallback and return typed `failed` on that invariant error. For assessment failures, label the latest candidate unassessed and identify the last assessed checkpoint separately using safe stage/hash fields. No invented heading enters the article candidate when title is absent; status/explanation belong to the preview wrapper. Use Task 5's text-preserving Markdown rendering for the requested candidate.
- [ ] Extend existing safe metadata projections for final stage/reason, allowlisted factual codes, registered quality class/severity/effect, fingerprints/source identity, requested/applied IDs and Task 3 outcomes. Validate IDs against the captured pass registry; whitelist statuses/reasons and numeric fields. Drop prose and raw exception strings, including nested fields.
- [ ] Keep production exceptions in the typed result, no debug persistence or DB/delivery writes. Ensure CLI writes both requested distinct paths for accepted/rejected/typed failed, with `REJECTED PREVIEW — DO NOT PUBLISH` for rejection and an unassessed/no-draft explanation for failure. Non-success exits nonzero. Deadline timeout returns typed failed; explicit `CancelledError`/process interruption propagates and is excluded from the file-output guarantee.
- [ ] Inspect in memory: accepted/rejected exact candidate, failed assessment after changed candidate, no draft, stale input/draft/source hash, nested metadata with candidate/source text, normal timeout and explicit cancellation. Check CLI output decisions with provider-free outcome objects, no exception-text leakage and no fictitious final rejection of an earlier draft.

### Task 5: Integrate the one authoritative article sequence

**Owner:** Root. **Files:** Modify `src/article_generator.py`, `src/publication/article_models.py`, `AGENTS.md`. Tasks 1–3 must be reviewed/frozen first; use Task 4's preview callback.

- [ ] Add optional keyword `source_identity: str | None = None` and `assessment_input_observer: ArticleAssessmentInputObserver | None = None` to `generate_from_frozen_input`, `generate_from_event_article_context` and `_generate_from_event_article_context`; propagate through assessment, editor and finalizer. Non-preview callers remain compatible. Keep the existing single `asyncio.timeout_at` owner and current provider recovery path unchanged.
- [ ] Preserve raw parsed quote wording until assessment/editor: add opt-in `preserve_quote_text: bool = False` to `StructuredArticleDraft.from_dict`; Event-First writer parsing sets it true, bypassing `_strip_non_allowlisted_quotes` and other edits inside quote interiors only in that path. Legacy defaults stay compatible. This prevents an early parser from erasing the very quote problem the editor should see; direct quotes remain subject to unchanged exact evidence checks.
- [ ] Add keyword `preserve_text: bool = False` to `StructuredArticleDraft.render_markdown`. Event-First production and requested frozen-preview rendering set it true, placing headings/paragraph breaks without `_strip_internal_handles` or other changes to candidate wording. Accepted candidates still must pass internal-handle/Evidence Boundary checks; rejected candidate text is available only in explicitly requested labelled Markdown. Legacy renderer defaults remain compatible.
- [ ] Replace scattered initial normalization/evaluation with Task 1 preparation followed by common assessment. Capture raw writer and prepared candidate checkpoints; pass the prepared checkpoint's exact draft, validation and quality into the existing editor. Do not classify low coverage, lexical mismatch or missing DEVELOP as unusable.
- [ ] On editor success use its exact `last_assessment`; on generic editor error use the last assessed editor checkpoint when available, otherwise the prepared checkpoint. Check matching current inputs/source identity before reuse. Timeout/cancellation propagate to the existing owner; no safe-output fallback bypasses deadline or unresolved safety findings.
- [ ] Call the gate-only finalizer on that same candidate; render exactly its resulting draft. Collect Task 3 safe outcomes without extra quality/model stages. Retain factual safety and proven structural blockers; readiness percentages/typography/register remain nonblocking under policy.
- [ ] Update AGENTS.md §§0.7/6.7 for closed preparation, gate-only finalization, source-bound exact checkpoints, whole-unit evidence/outcomes, organization typography and colloquial-vs-kitchen distinction. Record permitted contexts and versions; do not change digest selection or strengthen corroboration requirements.
- [ ] Integrated provider-free inspection: single-source community prose with name typography; missing district; mixed themes; exact/unsupported quotes; first editor pass accepted then error; structural rollback; unavailable packet; stale checkpoint; timeout. Compare actual draft/claims/support mapping at every boundary, unchanged 487/527 frozen material accounting when that saved input is available, and no source-fragment composition calls.

### Task 6: Independent review, one release and one real server preview

**Owner:** Root coordinates independent read-only code review after integration. Reviewers do not modify source; fixes return to the relevant owner. Reuse already successful checks unless changes or integration risk require repeating them.

- [ ] Review the complete change against every spec section and the Review Focus above. Explicitly inspect all terminal/error paths, exact quotes versus organization names, single-source attribution, per-unit/per-batch budgets, support-set authorization, finalization immutability, debug persistence and actual writer/editor counts. Address material findings before release.
- [ ] Run `.venv/bin/ruff check src/`, `.venv/bin/ruff format --check src/`, `.venv/bin/mypy src/` and `git diff --check`; expected results are exit 0/no new diagnostics. Run configured pre-commit on staged owned files; inspect any autofixes. No pytest/new tests/dependency installations. Version mismatches are reported, not solved by silently updating dependencies.
- [ ] Stage only owned changes. `docs/superpowers/` is ignored: explicitly force-add this spec and plan individually, never the directory or `.DS_Store`. Make the one integration commit in `dev`, record SHA, then `git push origin dev`. Wait for `.github/workflows/deploy-dev.yml` for that SHA, not a previous successful deployment; inspect concrete failures before any rerun.
- [ ] Connect with `ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key -o BatchMode=yes -o ConnectTimeout=10 opc@92.5.58.200`. Verify exact image revision and health of existing app/worker/processing/authority containers under `/home/opc/Telebrief`. No temporary generation container.
- [ ] Verify effective city-profile content separately: Compose mounts host `data` over image `/app/data`, and the current workflow copies config/Compose only. Deliver the approved profile update to the host mount through the existing release operation, preserving unrelated server edits; compare host/container content hashes and inspect expected name lookup without a provider call. Refresh existing services if their profile cache predates the update. Image SHA alone is insufficient proof of profile deployment.
- [ ] Before paying for generation, verify the frozen run/edition/window, current profile, one writer/two-editor configuration, source accounting and debug-disabled state. Do not trigger live publication or alter the sealed run. Then run **once**, from the server's existing app service:

```bash
docker compose exec -T telebrief-app python scripts/preview_article.py \
  --edition berdyansk --run-id 257 \
  --output /app/data/article-run-257-editor-finalization.md \
  --diagnostics-output /app/data/article-run-257-editor-finalization.json
```

- [ ] Follow the process through its actual exit, reporting meaningful stage changes while waiting. Do not start a duplicate because a stage is quiet. Copy both requested artifacts from host `/home/opc/Telebrief/data/` to local `/tmp/telebrief-article-run-257-editor-finalization.md` and `.json`, including on rejection/failure.
- [ ] Show the exact full text with its true status, final findings, actual models/call counts, phase timings, input integrity and safe repair outcomes. Separately assess thematic development, transitions, geography, details, service-domain separation, names and ending. Technical acceptance alone is not editorial success; report remaining defects concretely without a hidden paid rerun, filler or a publication veto based on coverage.

## Plan Self-Review and Handoff

Spec mapping: §§1–4 and 11 → global constraints/orchestration; §5 → Tasks 1–2 and quote-preserving Task 5; §6 → Tasks 1/4/5; §7 → Task 3; §8 → Tasks 2/3/5; §9 → Tasks 1/4/5 including exact rendering; §10 → inspections and Task 6. The five Review Focus conditions have explicit owning inspections. Checkpoint/source-identity and observer types match across tasks; budget scopes and cancellation semantics are explicit.

This plan preserves the user's selected subagent-driven execution, without TDD, directly in `dev`, with one final integration commit and one server preview. User review of this written plan is the remaining prerequisite. After approval, start the three disjoint worker assignments and implement in the declared dependency order.
