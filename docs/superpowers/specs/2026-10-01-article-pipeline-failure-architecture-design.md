# Article Pipeline Failure and Recovery Design

**Status:** Approved for implementation
**Revision:** 2026-10-01, reviewed against the full `AGENTS.md` and current implementation.
**Scope:** Event-First city-life articles. Digest generation and delivery are unchanged.
**Relationship:** Refines the approved long-read designs from 2026-09-22 through 2026-09-24. This document supersedes their proposal to add a separate LLM editorial-planner call. The product contract in `AGENTS.md` remains authoritative.

## 1. Goal

Generate a cohesive, concrete city-life long read from a large reporting window without repeatedly losing the whole article to a repairable editorial defect. Preserve strict factual and geographic safety, retain useful community reporting and supported microdetails, and make every failed dry-run inspectable without requiring another writer call.

Success means:

- the writer receives an explicit, evidence-grounded composition map;
- factual safety and reader-quality readiness are separate decisions;
- valid local edits are retained even when a separate edit fails;
- a style-only defect does not erase an otherwise safe article after bounded editing;
- unresolved unsupported claims or material geographic errors still fail closed;
- rejected previews return the candidate text and actionable diagnostics;
- there is one writer call and a strict cap on targeted editor calls.

## 2. Evidence and diagnosis

Production publication run 257 completed as `article_quality_rejected` after approximately 16 minutes. Its final metadata recorded:

- Evidence Boundary passed and the final quality gate failed.
- Two blocking findings remained: `QUOTE_ROLL_PARAGRAPH:P015` and `ARTICLE_PLACE_AREA_MISMATCH:P002`.
- Three repair attempts were recorded. The writer attempt metadata reported `candidate_remains_invalid` after editing; the finalizer subsequently reached an Evidence-Boundary-safe draft but still rejected it for the two quality blockers.
- The writer covered 221 of 316 planned Stories. Partial coverage is diagnostic under `AGENTS.md` and was not the rejection cause.

The failure exposes four architectural weaknesses: composition guidance is advisory and inconsistently enforced; the targeted editor and caller can lose useful changes when a combined candidate fails; a paragraph-style heuristic can block publication in the same way as factual geography; and the preview path discards the candidate and diagnostics when generation raises.

The current code already has `ArticleCompositionPlan`, narrative lines, relation groups, ordered Story packets, and conservative finalization validation reuse. Improve these contracts rather than building a second planner, map, validator, or orchestration loop. Current `config.yaml` permits three editor attempts and a 900-second provider request timeout; a written two-call limit alone does not bound actual elapsed time.

## 3. Constraints

- Keep the Event-First knowledge model and existing publication eligibility semantics.
- Keep one main LLM writer call. Do not activate the dormant standalone LLM planner or add routine whole-article regeneration.
- Permit at most two targeted editor stage invocations for one writer draft; enforce the cap at runtime even when configuration requests more. Count provider retries/failover separately and bound them by the same generation deadline. Stop early when there is no progress.
- Keep single-source community reports eligible when they are represented faithfully. Do not add corroboration or `ARTICLE_WORTHY` gates.
- Keep `DEVELOP`, `WEAVE`, and `BRIEF` as depth and prominence guidance. Do not require one paragraph per Story or hard 100% coverage.
- Preserve the Evidence Boundary, quote exactness, service-state semantics, time, geography, and provenance.
- Never publish deterministic concatenation, raw fragments, or synthetic filler.
- Do not change digest, ingestion, Gate, Event clustering, or delivery behavior.
- Do not introduce city-specific streets, areas, organizations, or prompt examples into generic production logic.
- Follow the user's request to work without TDD. Evaluate deterministic contracts with focused offline checks and inspect one frozen-input server dry-run; do not repeatedly rerun a stochastic writer looking for a lucky draft.

## 4. Architecture

```text
frozen eligible Event-First material
        ↓
ArticleCoveragePlan (candidate Stories, rank, depth)
        ↓
deterministic ArticleCompositionPlan (ordered narrative sections and evidence groups)
        ↓
one writer call
        ↓
assess draft (Evidence Boundary + reader quality)
        ↓
bounded targeted editor (0–2 calls, independently accepted patches)
        ↓
finalize and assess exact candidate (reuse unchanged assessments)
        ↓
publish safe, readable article OR fail closed with inspectable rejected preview
```

### 4.1 Composition map

Extend the existing composition layer rather than adding another planning model. Derive a deterministic, versioned article map from `ArticleCoveragePlan`, the existing composition relations, support metadata, and the edition geography resolver.

Keep the existing two levels explicit:

- **Narrative line:** a thematic home, such as electricity or connectivity. Independent reports from different districts may share a chapter about the same service.
- **Evidence group:** facts that may be synthesized through a specific supported relationship. Sharing a chapter does not prove a relationship between its groups.

Each narrative line in the map carries:

- a neutral reader-facing topic and its intended order;
- included group/Story IDs, each Story's existing depth/rank, and compact support references;
- supported relation type (such as shared condition, temporal progression, localized contrast, practical consequence, or independent topic);
- geography and time boundaries needed to keep reports distinct.

Rules:

- Combine Stories into a relation group only when subject and relationship are supported. A shared category is sufficient for a thematic chapter, but insufficient for a factual relation between its reports.
- Distinct neighborhoods remain distinct groups unless evidence establishes a useful relationship. The map must not infer proximity, common cause, chronology, or shared conditions.
- When a relation is ambiguous, retain the Stories separately. Do not invent a central thesis to bridge unrelated reports.
- Preserve exactly one map membership for every visible Story in the coverage plan. Composition adds no new suppression rule. Existing material exclusions must remain explicit and justified as noise, unsafe material, or directory payload.
- A section may synthesize multiple groups. Group boundaries protect fact ownership, not paragraph boundaries; do not force one group or one Story per paragraph. Aim to retain meaningful secondary material and microdetails compactly. Partial final prose coverage remains diagnostic; it is not permission for the map to discard smaller legitimate Stories.
- The map supplies a preferred order and thematic continuity. The writer may adjust chapter order and paragraphing for readability while preserving compatible thematic homes and clear geography/time distinctions. Avoid repeatedly returning to the same district or subject without a supported temporal or narrative reason.
- A relation label authorizes only its named relationship. A practical-consequence cue, shared district, adjacency in the map, or co-occurrence does not authorize causal wording, proximity, or a city-wide conclusion.
- Unknown geography stays unknown; multi-area Stories stay explicitly ambiguous. Resolve aliases through the edition profile and retain source-level place/time distinctions. A street name is not a descriptive claim about its position in the city.
- The map contains compact IDs and minimal boundaries, not duplicate source excerpts. Story packets remain the canonical source of exact facts, attribution, quotes, and effective times. Preserve meaningful detail anchors and distinguish resident questions and location-only context from publishable facts.

The planner LLM in `article_planner.py` remains dormant. The map adds no network stage. Validate its membership and references before the writer; an invalid map is a configuration/input error, not permission to silently drop Stories or make another planning call.

Account for roadmap and packet size together against the existing 500,000-character context ceiling and configured model capacity, including output allowance. Characters are not tokens. Preserve the existing full/compact packet path and report an explicit pre-writer budget error if compact material still cannot fit; never silently truncate supports or raise limits beyond model capacity. This work does not change Gate/Analysis semantics or require a routine knowledge backfill.

### 4.2 Separate safety from editorial readiness

The final decision has two explicit axes:

1. **Evidence Boundary:** hard safety gate. Unsupported claims, fabricated or over-specified causes, ungrounded direct quotes, false service assertions, and material geography errors remain non-publishable. No stylistic goal may weaken this gate.
2. **Reader quality:** actionable editorial diagnostics classified by impact.

Severity semantics:

- **Safety blocker:** factual/provenance/quote/geography error that changes or overstates source meaning. Fail closed unless a grounded edit or narrowly safe deletion resolves it.
- **Structural blocker:** a severe composition defect that makes the article materially misleading or unreadable (for example, raw fragment dumping or sections that fundamentally misrepresent their contents). Give it a targeted repair path; reject if still present.
- **Repair:** local style/readability issue, such as an excessive number of direct quotes in one paragraph, repetitive phrasing, or a weak transition. Attempt bounded editing. If only repair-level issues remain and the article passes Evidence Boundary and structural readiness, retain the article and report the residual issue; do not reject the whole publication for polish alone.
- **Warning:** non-blocking advisory with no material reader harm.

Use one canonical policy mapping for finding class, severity, scope, and permitted repair. The generator, editor, finalizer, and preview consume that policy; do not introduce separate lists that drift. Audit all current blocking quality codes, not only `QUOTE_ROLL_PARAGRAPH`. Unknown codes require an explicit policy before use, rather than silently defaulting to publication success.

Geographic mismatch and unsupported service-state contradiction remain safety blockers even if currently emitted by `article_quality.py`. Missing district information in a source is not a fabricated location: retain the attributed report with honest uncertainty, without borrowing another Story's district. Missing detail anchors, missing individual Stories, provider-name typography, and duplicate headings do not alone prove factual or whole-article structural failure.

An absent major storyline produces an explicit coverage/readiness diagnostic such as the existing `MISSING_DEVELOP_STORY`, naming the Story and supports. Review it within the same bounded repair budget when there is a suitable unit to amend; otherwise retain the diagnostic and mark editorial acceptance incomplete for the implementation review. Do not turn it into a numerical coverage veto, silently remove it from the plan, append filler, or start another writer loop. A misleading title/lead remains independently subject to evidence and structural checks.

Distinguish a heuristic trigger from the reader-visible defect it suggests. `QUOTE_ROLL_PARAGRAPH` currently triggers on more than two quotes in a paragraph; that count alone is a repair finding, not proof of an unreadable chat roll. Quoted supported organization names are not speech. Preserve the prohibition on actual consecutive source-fragment dumps: a confirmed raw quote roll or materially incoherent section is structural, with explicit text-level evidence and a repair scope. Do not relabel every residual style finding as structural to restore the old rejection behavior.

For a quote-roll structural finding, identify the actual spans where separate speech reports are concatenated with only list punctuation, without narrative synthesis. Ordinary attributed quotations connected by meaningful prose do not meet that predicate. Other structural findings likewise cite the failing section/text pattern rather than only a count, missing anchor, lexical score, or preferred order. The implementation plan must enumerate the predicates and repair targets before changing existing severity assignments.

Exact quote wording stays protected. The editor may synthesize reports as supported indirect speech. It must not grammar-correct or merge text inside a direct quote. Ordinary grammatical variation, faithful translation outside quotes, low lexical overlap, source uncertainty, and lack of corroboration are not factual blockers.

Update `AGENTS.md` §0.7 and the related forbidden-regression guidance so future changes preserve the distinction between evidence-safety blockers and repair-level style findings.

### 4.3 Targeted editor and patch acceptance

The editor receives only targeted units plus enough adjacent section context and exact supporting evidence to repair them. It returns explicit per-unit proposed changes and outcomes.

- Check each proposed unit's syntax, permitted support ownership, quote/entity/number/time grounding, and authorized scope before merging. Unit checks do not substitute for whole-draft validation.
- Reject an independently unsafe patch without discarding other proposed changes. Validate the combined candidate once. Existing unresolved defects in unchanged units do not make a safe local improvement disappear.
- If validation identifies new safety/structural defects attributable to specific changed units, quarantine those patches and validate the remaining candidate once more. If the failure is global, cannot be localized safely, or validation itself raises, restore the previous consistent draft/assessment checkpoint. No combinatorial patch-subset search or full-draft validation per paragraph.
- Retain confirmed improvements in the resulting checkpoint even when other pre-existing blockers remain; that checkpoint remains non-publishable until the final gates pass. Never require the whole draft to be publishable before retaining intermediate progress.
- Do not accept or reject a repair solely because total diagnostics decreased. Progress means resolving a targeted finding or demonstrably reducing its affected scope without new safety/structural defects; mere text changes are not progress. Stable finding identities include code, affected unit, and support ownership, with unit IDs remapped after structural edits.
- Permit at most one follow-up editor call when the first call made measurable progress and actionable findings remain. Do not make a third no-op attempt.
- If a factual/geographic issue remains, prune only an explicitly localized unsupported phrase or sentence when removal cannot change the meaning of the supported remainder. Rebuild affected claims/support mappings and revalidate. Do not prune legitimate single-source reports, difficult paraphrases, or whole major storylines merely to get a passing result. If safe localized deletion is unavailable, fail closed.
- Keep the writer draft, merged editor draft, final validated draft, and patch history distinguishable in attempt metadata. Never substitute a draft that failed factual validation for a safe one.

Make the assessment a small immutable result paired with its full structured draft: Evidence Boundary report, quality report, provenance/coverage diagnostics, and input fingerprint. Pass it through the existing coordinator; do not create a new framework. There is one final publication decision on the exact rendered candidate.

Reuse an assessment only when the full structured draft (including claims and support mappings), frozen context, material projection, geography profile, coverage/length settings, and validator/policy versions match. Rendered-text or normalized-text equality is insufficient. Revalidate whenever any of those inputs changes, including finalizer deduplication or paragraph merges. Build `ArticleClaimTrace` from the accepted final draft.

### 4.4 Bounded execution and failure ownership

The article coordinator owns one generation deadline and the stage budgets; editor and finalizer cannot start their own recovery loops. Add a positive `article_generation_timeout_seconds` setting, initially 1200 seconds, for frozen material preparation through the final assessed draft. Collection, delivery, and cover generation are outside this budget. The default is an operational bound, not a claimed optimal latency; report actual phase timings before adjusting it.

Each provider operation uses the smaller of its configured timeout and the remaining deadline; retries, failover, queue waits, and editor calls share that remaining budget. Report logical writer/editor invocations separately from transport attempts. Use only configured, allowlisted models.

Stop optional polish when the budget is exhausted. A previously fully assessed safe/readable checkpoint may still be returned; an unchecked or blocked checkpoint cannot. Preserve the last candidate and failure stage for explicit preview output. Keep CPU-heavy validation off the async event loop and do not start unbounded background work after cancellation. A timeout is an execution failure, never evidence that a draft is safe.

### 4.5 Failed-preview result

An explicit dry-run must produce a structured result for both success and rejection. When a candidate draft exists, rejection output includes:

- the last available structured candidate, its stage/revision, and exact rendered preview, clearly labeled `REJECTED PREVIEW — DO NOT PUBLISH` (including failures before finalization);
- failure stage and reason;
- finding codes, severities, and paragraph/heading IDs;
- writer/editor call counts, accepted/rejected patch IDs, and phase durations;
- planned/covered Story counts, uncovered major storylines/detail anchors, prompt/context lengths and hashes, and code/config/semantic/profile versions.

If generation fails before any candidate draft exists, report that no draft was produced and still return the failure stage and available diagnostics.

Use a typed preview outcome with `accepted`, `rejected`, or `failed` status and an optional candidate. The production generator retains its rejection exception contract. Capture candidates through an explicitly enabled in-memory preview sink, rather than adding full prose to persisted exception metadata or changing a rejected article into a success tuple. Keep candidate, last assessed checkpoint, and their validity states distinct.

The frozen-run preview CLI writes the candidate and diagnostics to requested paths before returning non-zero for rejection/failure. If no candidate exists, write a clear no-draft report, not a previous successful file; artifact write failures are surfaced. A success and a rejection use the same diagnostic schema, with field-level allowlisting. It must not create a Telegraph page, send to Telegram, or present a rejected draft as a deliverable. Do not copy full raw source packets, secrets, or provider responses into ordinary logs. Production failures retain compact reason codes and attempt summaries; full candidate text is for an explicitly requested preview artifact only.

Acceptance exercises `--run-id` frozen replay using the same generator, editor, finalizer, editorial config, edition timezone, and sealed supports as production. Replay reruns the model; it does not reproduce exact prior wording. Replaying a saved draft through deterministic validation is a separate offline check. Do not claim this preview verifies scheduling, queue dispatch, or delivery, or broaden this change to other CLI modes.

## 5. Acceptance criteria

1. One generation uses at most one writer stage invocation and two targeted editor invocations, even with a configured editor count of three; no separate planner or writer-regeneration call is added. Actual provider attempts and total elapsed time are bounded and reported separately.
2. Composition metadata includes preferred order, exact visible Story/support membership, relation types, and geography/time boundaries. Distant neighborhoods remain distinct evidence groups but may share a correctly named service chapter. Neither shared chapter membership nor a relation cue invents proximity or causality.
3. A legitimate single-source community report remains eligible. All visible coverage-plan Stories remain available to the writer; partial final coverage is diagnostic rather than an automatic rejection. Review the actual text for absent major lines, meaningful secondary breadth, and retained supported microdetails; do not use a percentage as a substitute for that review.
4. An unsupported factual or material geographic claim fails closed. A grounded local correction survives another independently rejected patch or another unit's pre-existing blocker; a global validation error restores a consistent checkpoint. Per-unit text and claims never use stale validation results.
5. If a draft passes the Evidence Boundary and has no demonstrated structural blocker, a quote-count heuristic or another repair-level style finding alone does not reject the article after bounded editing. Actual raw quote/fragment dumps remain forbidden.
6. If a serious structural defect remains after its bounded repair opportunities, the article is rejected with unit-level reasons; no deterministic or fragment-dump fallback is used.
7. A rejected frozen-input dry-run writes its labeled candidate and diagnostics to the requested output path and exits non-zero without any delivery side effect.
8. Run 257's frozen input is used once after implementation to inspect composition, factual safety, quality findings, call costs, and timings. Since model output is stochastic, acceptance is based on the contracts and reviewed article, not exact text reproduction or repeated attempts until one passes. A successful generation alone cannot prove rejection-artifact behavior.
9. Digest behavior, Event-First facts, publication selection, and delivery contracts are unchanged.
10. `AGENTS.md` documents the implemented safety/readability severity boundary and rejected-preview behavior.
11. Focused offline checks exercise both sides without more paid writer calls: faithful community paraphrase survives; an invented district/cause/number or altered quote is blocked; quote-count/name-quotation heuristics do not falsely veto prose; safe and unsafe patches coexist; unchanged structured inputs reuse validation and changed support mappings invalidate it; rejected/no-draft/timeout previews write diagnostics and exit non-zero.
12. Manual review of the generated title, lead, chapter contents, and body finds coherent thematic hierarchy, natural paragraphs and transitions, supported local details, honest attribution, and no directory/fragment dump. Treat a disappointing article as an unmet editorial objective even if technical gates pass; record concrete defects before deciding on further changes.

## 6. Failure and rollout behavior

- A remaining Evidence Boundary or structural blocker rejects the publication and records a concise, actionable reason.
- Repair-level style findings do not by themselves suppress a safe article. They remain visible in diagnostics for continued improvement.
- Failure artifacts are conspicuously labeled as rejected and cannot flow into publication delivery.
- Implement and commit on `dev`, then push/deploy using the existing deployment path as previously requested. Run one frozen-input dry-run on the server after deployment. Do not trigger a live article send as part of this design's acceptance.

## 7. Non-goals

- Activating the standalone LLM planner, adding chapter-by-chapter generation, or adding a second article-writing loop.
- Making every planned Story mandatory in prose or pursuing a numerical coverage score.
- Weakening fact checking, neighborhood accuracy, quote provenance, or single-source attribution.
- Changing digest, ingestion, Event clustering, Event-First truth, or automatic delivery.
- Persisting full rejected article text to production database rows or logs by default, adding a global validation cache, or evaluating every possible patch combination.
- Rewriting or republishing historical Telegra.ph articles.

## 8. Scope of implementation and contract precedence

The implementation changes the existing composition renderer/prompt contract, quality policy, editor/coordinator handoff, finalization assessment reuse, and frozen-preview result. It adds only the generation deadline setting and compact diagnostics required for those contracts. Keep current dataclasses and repository boundaries where possible; do not expand this into a generic workflow engine or new knowledge model.

Apply `AGENTS.md` Section 0 when its historical architecture descriptions conflict with its product contract. For example, the later digest dashboard/fallback descriptions do not override the scan-first, no-dashboard, fail-closed digest contract. This article change does not modify digest behavior or reconcile unrelated digest documentation.

The proposed clarification to §0.7 preserves the direct-quote, geography, microdetail, and no-dump obligations. It distinguishes proof of a product defect from a conservative heuristic; it does not authorize publishing arbitrary bad prose to improve success rates. Do not add a 100% article-coverage veto, a corroboration threshold, or new subjective eligibility gate while implementing this design.
