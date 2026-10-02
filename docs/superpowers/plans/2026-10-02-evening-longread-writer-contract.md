# Evening Longread Writer Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. User overrides: work directly in dev, no TDD, no new tests, no per-task implementation commits.

**Goal:** Получать связную городскую статью из понятного полного корпуса и сохранять ограниченное восстановление после полного отказа writer без ослабления Evidence Boundary.

**Architecture:** Existing Event-First composition, one logical writer stage and bounded ArticleEditor remain. Consolidate the Markdown prompt and source representation; an article-only response acceptance callback allows one semantic transition in the existing configured provider sequence. The coordinator owns the generation deadline, grounding scope, exact candidate assessments and preview diagnostics.

**Tech Stack:** Python, asyncio, existing AIProvider/ProviderCascade, frozen Event-First models, Ruff/MyPy/pre-commit, Docker Compose server runtime.

**Spec:** [Approved design](../specs/2026-10-02-evening-longread-writer-contract-design.md).

## Global Constraints

- Product/editorial precedence: AGENTS.md §0; useful single-source reports and supported microdetails survive.
- Work on `dev`; preserve unrelated `docs/superpowers/.DS_Store`; do not create a worktree or branch.
- At most four active agents including root; subagents do not delegate or commit.
- No TDD, new tests or pytest runs. Use static checks, source review and narrowly scoped offline executions of existing functions; no new persistent test suite.
- One logical writer stage, at most two nonempty responses and one semantic transition. Configured transport retries remain within the same deadline and attempt sequence.
- At most two editor calls; overall generation timeout remains 1200 seconds; renderer context ceiling remains 500,000 characters.
- No planner, whole-article editor rewrite, per-Story generation, coverage veto, filler or deterministic article fallback.
- No new models or dependency installations; runtime allowlist remains authoritative.
- No ingestion, Gate/Analysis, digest, database schema or delivery changes. No semantic backfill.
- One final implementation commit/push/deployment and one frozen server preview of run 257. A rejected preview is shown honestly and never delivered.

## Review Focus

1. Large, repetitive corpus: all expected writer supports and their distinct details/time/place survive; overflow is explicit. Task 1 offline inspection owns this.
2. Plain prose lacking a title/heading: preserve words/order; missing format does not automatically mean refusal. Task 3 owns this.
3. Primary transport failures followed by unusable success: reuse the fixed attempt order, never restart completed slots. Task 2 owns this.
4. Refusal language quoted within a real report, faithful paraphrase with zero lexical matches, thin day: no semantic retry solely from these conditions. Task 3 owns this.
5. Second response or editor exhausts time: no publication on a stale/unchecked assessment; preview identifies the actual candidate and attempts. Tasks 4–5 own this.

## File Ownership and Interfaces

Root owns `src/article_generator.py`, `src/publication/article_preview.py`, `AGENTS.md`, plans/specs and release integration. Worker A owns `src/publication/article_writer_context.py`, `src/publication/narrative_contract.py` and new `src/publication/article_writer_input.py`. Worker B owns `src/ai_providers.py`. Task 3 worker owns new `src/publication/article_writer_response.py` and later `src/publication/article_editor.py` only after root fixes its interface.

No overlapping concurrent writers. Existing article models and quality policy are read-only unless root identifies an unavoidable interface change; coordinate it before editing. Preserve existing signatures for unrelated callers.

### Task 1: Complete evidence dossier and one Markdown prompt contract

**Owner:** Worker A.

**Files:** Modify `article_writer_context.py`, `narrative_contract.py`; create `article_writer_input.py` under `src/publication/`.

**Interfaces:**

- Preserve `render_article_writer_context_with_stats(context, coverage_plan, *, include_coverage_plan=True, material_projection=None, composition_plan=None, materialization_mode="packetized")` and its existing tuple result for compatibility.
- Add immutable `ArticleWriterInput` with `context_text: str`, `expected_support_ids: tuple[str, ...]`, `exposed_support_ids: tuple[str, ...]`, `quote_allowlist: tuple[str, ...]`, `metadata: dict[str, object]`.
- Add `build_article_writer_input(context: ArticleEditorialContext, coverage_plan: ArticleCoveragePlan, *, material_projection: ArticleMaterialProjection, composition_plan: ArticleCompositionPlan) -> ArticleWriterInput`. Root uses this instead of rebuilding expected/exposed supports after rendering.
- `build_article_narrative_contract(output_language=..., length_profile=...)` remains the owner of generic editorial rules and retains its signature.

- [ ] Derive expected PUBLISH supports from the frozen plan/context and existing projection before rendering; carry each support identity through grouping. Define exposed IDs from rendered representation, not from the expected set.
- [ ] Keep the existing packetized path as the canonical implementation; eliminate duplicate instructions and identical fact/source text, retaining distinct text, exact quote candidates, time, place, epistemic status and parent-context semantics. Preserve related IDs/provenance when deduplicating equivalent entries. Remove character slicing of factual bodies in this path; on overflow after safe deduplication raise a preparation error.
- [ ] Produce one compact map and a canonical evidence inventory. Explicitly distinguish navigation, facts and context. Encode source bodies so a literal source delimiter cannot terminate the reporting envelope; do not drop source messages containing such strings.
- [ ] Verify expected/exposed equality after justified projection and source-group membership. Build the quote allowlist only from exposed eligible evidence and the actual primary text; do not cut the words of an allowed quote to fit a block.
- [ ] Consolidate generic instructions in `narrative_contract.py`: plain Markdown only, no output Claim Atoms/support IDs, exact direct quotes, natural indirect speech, supported organization-name styling, coherent thematic development and geographic/time distinctions. Remove duplicate/contradictory generic instructions from root's wrapper through the Task 4 handoff. Retain explicit source/evidence restrictions.
- [ ] Bump the changed writer-context/narrative prompt versions; keep other semantics unchanged. Metadata reports counts, representation and hashes, no factual prose.
- [ ] Offline inspect a saved frozen context or synthetic local ArticleSupport values: duplicate bodies, different states/times, one-source report, CONTEXT-only question, long late support, quote, literal delimiter and overflow. Compare represented IDs and complete text before/after. No provider calls.
- [ ] Run `.venv/bin/ruff check` and `.venv/bin/ruff format --check` on owned files; hand off changed paths, interface values, observed size difference and any unresolved capacity assumptions.

### Task 2: Opt-in response acceptance within the existing provider sequence

**Owner:** Worker B; can run alongside Task 1.

**File:** Modify `src/ai_providers.py`.

**Interface:** Add `ProviderCascade.chat_completion_with_acceptance(messages: List[Dict[str, str]], model: str, temperature: float | None = None, max_tokens: int = 65536, reasoning_effort: str | None = None, thinking: bool | None = None, response_format: Dict[str, Any] | None = None, *, response_acceptor: Callable[[str], Awaitable[bool]], max_semantic_rejections: int = 1) -> str`.

The ordinary `AIProvider.chat_completion` contract and all existing consumers remain unchanged. The opt-in method returns the last nonempty response when acceptance fails and no recovery is available, including a rejected second response; the article caller still assesses/fails closed. Acceptance means usable for article assessment, not publishable.

- [ ] Factor the existing slot-order selection and transport execution so ordinary and opt-in methods reuse them. Freeze round-robin order once per call; retain started slots, transport failures, cooldown decisions and actual selected-model metadata.
- [ ] After each nonempty response, invoke the async acceptor outside transport-error handling. Callback exceptions/cancellation propagate as assessment failures and never become provider failures or authorise another slot.
- [ ] If acceptance is false, continue after that successful slot at most once, only over remaining unstarted configured slots. No restart or wraparound; transport failures during recovery can advance to further remaining slots until a nonempty response or exhaustion. Evaluate but always stop after the second nonempty response.
- [ ] Preserve transport retry policy within an already started slot; count retries separately. Ensure a quota-retry success follows the same acceptance path and records actual provider metadata. Semantic rejection alone does not apply a transport cooldown.
- [ ] Preserve captured attempt counts and add safe `writer_response_count`, `semantic_transition_count`, `semantic_recovery_reason`/exhaustion state and ordered started-slot identities to `last_metadata`; no response text.
- [ ] Respect the existing outer cancellation/deadline and per-slot timeout caps. Do not add provider-specific waits, new models or a second timeout owner.
- [ ] Offline execute with in-memory fake providers: ordinary success unchanged, transport error before first response, false/true acceptance, false/false acceptance, no next slot, quota-retry response, callback exception and cancellation. No external calls, no test files. Check request equality, counts and no revisited slots.
- [ ] Run focused Ruff/format checks; hand off exact method and metadata fields for Task 4.

### Task 3: Preserve Markdown and distinguish response disposition

**Owner:** Worker C after root confirms interfaces; can run alongside Tasks 1–2. No edits to generator.

**Files:** Create `src/publication/article_writer_response.py`; bounded changes to `src/publication/article_editor.py` if required for missing title/lead repair.

**Interfaces:** Immutable `ArticleWriterResponse` with `parsed: dict[str, Any]`, `disposition: Literal["usable", "repairable", "unusable"]`, `reason: str`, `format_findings: tuple[str, ...]`. Add `parse_article_writer_markdown(response: str) -> ArticleWriterResponse`. JSON compatibility remains with the root-owned legacy parser, which delegates normal Markdown here.

- [ ] Parse canonical title/lead/`##` chapters, explicit `#` title and external fence without losing paragraph text/order. A bare title must be a separate single-line block before a separate lead and explicit chapter headings, without ordinary sentence-final punctuation; otherwise preserve that block as prose and flag missing title. Multiple-sentence prose or paragraph-wrapped text is not a title merely because it is first. Ambiguous or punctuated bare titles may need bounded repair; their original words must remain in the candidate.
- [ ] Preserve titleless/unheaded text in internal unnamed containers, with format findings and no generated reader-facing title or section. No empty-lead reshuffling that silently removes the original opening.
- [ ] Classify empty envelope, unrecoverable parse without prose, and whole-response service/refusal prose conservatively. Refusal detection must account for all blocks; supported report prose or quoted refusal does not become a whole refusal. Ambiguous output goes to assessment. Neither zero grounding/coverage nor the word "не могу" alone triggers `unusable`.
- [ ] Use existing editor issue targeting for missing TITLE/LEAD only when body evidence is available. Offer bounded current-window supports already used by body/DEVELOP material in the article; retain explicit eligible allowlists. If the existing editor cannot safely repair the format, preserve the candidate and return rejection instead of rewriting the article.
- [ ] Offline inspect canonical Markdown, titleless multi-sentence opening, no headings, code fence, literal delimiter, pure refusal, refusal quote in a report, supported short report and malformed JSON retained as text. Confirm every factual paragraph survives and only definite unusable envelopes qualify.
- [ ] Run focused Ruff/format checks; hand off parser outputs and any editor limitations.

### Task 4: Integrate writer, assessment, editor and safe preview

**Owner:** Root, dependent on Tasks 1–3.

**Files:** Modify `src/article_generator.py`, `src/publication/article_preview.py`, `AGENTS.md`; use existing finalizer/model interfaces.

- [ ] Replace the material-preparation tuple assembly with Task 1 `ArticleWriterInput`; preserve observed input hashes, timing, projection and richness profile. Keep the complete frozen context for validation, but restrict initial `_ground_draft_in_coverage_plan` candidates through a new keyword-only `allowed_support_ids: frozenset[str] | None = None`; Event-First caller always passes exposed IDs. Do not broaden provenance through lead fallback.
- [ ] Reduce `_build_event_article_system_prompt` and post-material task to format/window/length/source-envelope instructions around the single generic contract. Keep existing length semantics; neither scale article space per Story nor raise limits. Known provider capacity checks use actual configured-slot metadata or documented capacities; report unknown capacity honestly, never invent a tokenizer conversion or silently shrink the corpus.
- [ ] Delegate Markdown parsing to Task 3; preserve JSON compatibility. Replace blank-lead catastrophic bypass with explicit response disposition; usable/repairable prose goes through existing assessment and bounded editor.
- [ ] Build an async response acceptor that parses and captures each response, returning false only for `unusable`. Parsing runs off the event loop when CPU work warrants it. Use Task 2 method only for ProviderCascade; a plain AIProvider has no configured next slot and returns one candidate. Callback errors never trigger recovery.
- [ ] Keep the request immutable across responses. Capture provider attempts around the whole writer stage, preserve separate writer-response checkpoints and stage metadata, then assess the selected candidate. Existing observer records one logical writer stage and the actual response count/slots.
- [ ] Ensure preparation, acceptance callback, writer requests, final assessment and editor all share the existing 1200-second deadline. Do not validate/repair the first unusable response as though it were the second. No unchecked checkpoint is returned on timeout.
- [ ] Extend only field allowlists in safe preview diagnostics with Task 1–3 non-prose fields. Identify which response produced each displayed checkpoint; no raw request/response persistence. Preserve rejected preview status and nonzero CLI exit.
- [ ] Update AGENTS.md §0.7/§6.7/§13 with the exact one-semantic-transition exception, Markdown/grounding scope and missing-lead handling. Remove contradictory one-physical-call wording for this exception; preserve eligibility, Evidence Boundary, two-editor cap and forbidden regressions. No Gate/Analysis version changes.
- [ ] Run integrated offline inspection of usable writer, titleless repairable draft, pure refusal→second response, ambiguous nonsense rejection, support-ID exposure and timeout checkpoint. Confirm no delivery/state writes and no article-synthesis fallback.

### Task 5: Independent review, static checks, deployment and one real preview

**Owner:** Root coordinates an independent read-only reviewer after integration.

- [ ] Review the full diff against the approved spec and AGENTS.md. Explicitly check expected/exposed support accounting, request identity, all provider return paths, callback failure ownership, quote fidelity, provenance scope and actual editor attempt count. Address concrete findings before release.
- [ ] Run `.venv/bin/ruff check` and `.venv/bin/ruff format --check` for changed Python files, relevant `.venv/bin/mypy --ignore-missing-imports --no-warn-return-any` and `git diff --check`. Use existing pre-commit on staged implementation files. No pytest or new tests.
- [ ] Inspect actual git diff and stage only owned changes, preserving `.DS_Store`. Commit once at integration end and push `dev`. Record SHA and configured CI outcome; do not claim editorial success from these checks.
- [ ] SSH with `/Users/air/Downloads/ssh-key-2026-08-05.key` to `opc@92.5.58.200`. Inspect `/home/opc/Telebrief` branch/revision and container configuration before using its existing deployment path; preserve server-local configuration and credentials. Update/rebuild existing service containers and verify app/worker revision matches the implementation SHA. No one-off replacement generation container.
- [ ] Execute once in the running app: `docker compose exec -T telebrief-app python scripts/preview_article.py --run-id 257 --output /tmp/article-run-257-unified-writer.md --diagnostics-output /tmp/article-run-257-unified-writer.json`. Follow the actual process through completion; report meaningful stage changes while waiting. This is read-only frozen replay, no Telegram/Telegraph publication.
- [ ] Copy requested preview artifacts to local absolute paths, show full text with rejected label when relevant, and report Evidence Boundary, actual providers/responses, editor calls, phase durations, source representation and readiness gaps.
- [ ] Read title/lead, thematic development, neighborhood order, repeated content, concrete actions, service-domain separation, organization typography and ending. Explain any remaining defect using actual passages. A technically accepted flat inventory is not a completed reader-quality goal; do not hide it or rerun the writer for a lucky draft.

## Execution Handoff

The user approved this plan and selected subagent-driven execution with up to four agents including root. Tasks 1–3 were delegated under the disjoint ownership above, Task 4 was integrated by root, and Task 5 is the release gate. Subagents return evidence and verification, not commits.
