# Digest Editorial Composition and Final Quality Gate

**Status:** Revised after architecture review, 2026-09-28; specification only, not implemented
**Scope:** Event-First scan-first digest generation and preview only

## 1. Purpose

Improve the short Telegram digest so residents can scan a broad, accurate account of local developments without encountering repeated reports, a directory of routine commercial notices, malformed headings, or mechanical boilerplate.

The immediate evidence is server preview publication 86 for Berdyansk, generated for a 12-hour window on 2026-09-28. The body repeated reports about electricity in 8 Marta and Koloniya, retained routine commercial/directory material, repeated a headline emoji, and included awkward channel/meta language. The persisted prose audit marked the final result clean. The 24-hour generation failed its character budget before producing a preview.

The reader product remains a scan-first digest, not a long-read article. The current Event-First knowledge model, evidence provenance, and single-publication-call architecture remain in place.

## 2. Product constraints

- Preserve every substantive Story and required material fact that passes hard eligibility and is selected into the digest presentation plan.
- A single reader item may represent multiple related Stories and facts. Never force one visible item per source Story.
- Preserve useful local reports from a single community source. Lack of corroboration is not an exclusion reason.
- Hard-exclude only high-confidence hard-ineligible noise, private classifieds, commercial-only ads, and directory payload. Separately, before plan approval, digest budgeting may defer secondary low-impact material with an explicit reason; this is not a deletion from knowledge or an article exclusion. A business name, address, price, or schedule alone is not enough to exclude a fact when it materially informs residents about a public condition or useful local service.
- Keep distinct neighborhoods and streets spatially distinct. Only synthesize a localized contrast when facts concern the same named or canonical area. Preserve differences in status and time.
- Keep all reader-facing claims grounded in the frozen Event-First evidence and preserve claim/support traceability.
- Do not use raw-message concatenation, synthetic filler, or a deterministic prose fallback. If a serious defect remains after bounded repair, fail closed.
- Keep the digest within one Telegram post (technical ceiling 4096 characters; target range 2500–3700 characters where material volume permits).
- Do not reintroduce one generative call per source message or Story.

## 3. Proposed design

```text
Frozen Event-First inputs
        ↓
Frozen upstream eligibility and publishable evidence
        ↓
Fact composition + priority/budget admission
        ↓
Frozen DigestPresentationPlan + evidence-linked composition map
        ↓
One structured writer call
        ↓
Rendered-text quality audit
        ↓
One bounded structural/style repair with trace preservation
        ↓
Revalidate evidence, coverage, geography, and final quality
        ↓
Preview/publication, or fail closed
```

### 3.1 Separate frozen eligibility from presentation priority

Event-First candidate sealing must require a frozen Gate `KEEP` decision, local/direct-impact scope, and an EventPayload with at least one `PUBLISH` evidence item. The current candidate query enforces Gate retention and scope but does not fully enforce the PUBLISH-evidence condition; add that deterministic check against the candidate's frozen revision before the selector runs. Preserve this upstream eligibility boundary. The publication selector is a priority/presentation layer: an `OMIT` proposal or `HARD_EXCLUSION_REASONS` label from the selector alone must never remove an otherwise eligible candidate. Retain it as diagnostic metadata for editorial review. Do not query mutable Gate state to reinterpret an old publication run. For legacy inputs without frozen eligibility, unknown eligibility is not a new exclusion reason; preserve them and report the provenance as unknown.

At fact level, writer-facing material includes `PUBLISH` evidence only. `CONTEXT` may inform interpretation when explicitly needed, and `EXCLUDE` content remains internal and must not become a reader claim. Mixed material must retain its useful civic or community fact while omitting promotional copy, contacts, and irrelevant directory detail. Do not add a blanket rule against single-source reporting or all commercial names, prices, addresses, and hours. Keep article selection behavior unchanged.

### 3.2 Build an evidence-linked composition map

Extend digest presentation planning to identify high-confidence duplicate/overlapping reports and related facts before writing. Composition relationships may use:

- canonical subject or service dimension;
- edition-profile area and explicit street/micro-location;
- compatible effective time and service state;
- normalized fact identity and overlapping source/Story support.

A composition group is a reader-facing view over immutable Stories and evidence. It stores member Story IDs, required fact IDs, support IDs, and the reason for grouping. It does not alter Event-First truth, merge persisted Stories, or create evidence.

Combine repeated reports only when they describe the same fact or coherent situation in the same place and compatible time. Distinct areas, unresolved geography, incompatible times, or different states remain explicit. A same-area disagreement may be written as a localized contrast; remote areas cannot share an umbrella label. Every member Story and material fact must map to at least one final item, while duplicate underlying facts should appear only once unless a supported change over time makes both states material.

The writer receives a compact map and its evidence packet. Composition units constrain evidence membership, not a fixed item quota. The writer may express a unit in one compact item or approved distinct subitems; do not mechanically turn every unit into a mandatory headline. The writer must not create new memberships or move facts across rubrics.

Each writer item names one or more exact `composition_unit_ids` that share one configured rubric and a subset of those units' `fact_ids`. The application derives the item's Story and support IDs from only the referenced facts, not from every fact in a referenced unit, and never trusts model-authored unions. Multiple subitems may partition units' facts, but the draft must cover every admitted fact exactly once. Units with no fact records remain explicit Story-level coverage obligations and may be woven into an item with other zero-fact units from the same rubric; each such unit's Story must be represented exactly once. The writer may keep an item separate when combining those summaries would read like a bulletin board.

### 3.3 Make emoji and heading ownership explicit

The structured `emoji` field is the sole owner of the item icon. `headline` contains text only. The renderer also performs a deterministic leading-icon cleanup so a non-compliant model cannot produce two icons. Headlines are optional under the product contract: use a short scan-label only when it adds information, and omit it when it repeats the body. Adjust schema/validation accordingly. Preserve meaningful numerals and punctuation during icon cleanup.

### 3.4 Extend the editor contract for safe merging

The current editor can rewrite text by input item index but cannot merge items or remove a duplicate presentation unit. The revised editor contract supports an explicit merge of approved input item IDs into one output item. Each output item returns the union of its approved Story IDs, required fact IDs, and support references; Python checks that mapping against the allowlist, then rebuilds and validates claims against the edited visible text and evidence. Unioning IDs is only a coverage obligation: it is never proof that the final prose contains those facts. Reject unapproved IDs, missing claims, or invalid support.

`allowed_merges` is a list of explicit authorizations. Each has a stable `merge_id`, the exact source `item_id`s, the frozen `SAME_FACT` relation IDs that authorize the merge, their connected fact-ID graph, the source `composition_unit_ids`, and their common rubric. One authorization may join three or more items only when the included `SAME_FACT` edges connect every source item through the duplicated fact endpoints. Items from different rubrics cannot be merged. The editor output identifies the exact `source_item_ids`, retains their complete union of claim atoms, facts, Stories, and supports, and stays in their common rubric; it cannot add other input items or discard unrelated claims carried by either item. Each parsed writer item receives a deterministic draft-scoped `item_id` from its composition-unit IDs and position so an editor repair can target specific items without positional guessing.

The editor receives only the relevant items, merge candidates, and their allowed fact/support context. It does not receive an unrestricted mandate to redesign the digest. It may also repair local wording, remove repetitive generic caveats, and compress copy without changing supported facts, epistemic status, location, or time.

### 3.5 Audit the exact rendered text

Run reader-quality diagnostics over the final text after rendering, alongside the existing Evidence Boundary and coverage trace. Add high-confidence checks for:

- semantic duplicate claims across items, based on fact/support overlap and compatible subject, place, state, and effective time;
- an exact fact presented more than once through separate Story clusters;
- directory or promotional payload dominating a reader item or the digest;
- prohibited ingestion/source mechanics such as references to city channels or chats;
- repetitive unsupported absence caveats and redundant headline/body content;
- duplicate leading emoji or malformed item headings;
- Telegram character limit violations.

Do not flag items as duplicates solely because they share a broad domain, location name, or a topic such as electricity. Distinct streets, areas, states, or effective times remain valid when the evidence supports them. Do not treat a single editorial style preference as a blocking error.

High-confidence structural or factual defects trigger one bounded editor repair and full revalidation. If duplicate coverage, directory dominance, broken geography, unsupported claims, required-fact loss, or the message budget remains defective, generation fails with a specific diagnostic and no digest delivery. Non-blocking style findings may be recorded without rejecting otherwise clean prose.

## 4. Diagnostics and versioning

Bump the semantic/prompt versions for any changed digest selection, composition, writer, editor, or final-quality behavior. Persist compact metadata containing:

- composition-map version and group membership by stable Story/fact/support IDs;
- pre- and post-repair quality finding codes;
- the approved merge mapping and resulting coverage trace;
- final rendered character count, selected/deferred counts, and frozen upstream exclusion counts only when those counts are available from the publication snapshot; report unavailable counts as unavailable;
- final evidence-boundary and selected-Story/material-fact coverage outcomes.

Do not persist full secrets, raw provider prompts, or unnecessary copies of source text in audit metadata. Existing publications and frozen snapshots remain immutable.

## 5. Acceptance criteria

1. Exact or near-duplicate electricity reports for the same place and compatible time produce one reader item with all useful supporting provenance. The same material claim does not reappear under a slightly different headline.
2. The two Koloniya line reports in the observed preview do not repeat the same 11th-line outage in separate items. The 1st and 11th lines remain identifiable where their statuses differ.
3. Reports from distinct neighborhoods remain separate even when they concern the same service. Localized contrast is used only within a shared canonical area and supported time frame.
4. Useful single-source reports survive; classified ads, private listings, and directory-only material do not dominate or inflate the digest. Useful resident-facing service facts survive extraction from mixed promotional material.
5. Each rendered item has no more than one leading thematic emoji, contains no ingestion/channel mechanics, and avoids repetitive generic caveats unless separately supported and materially useful.
6. Every selected substantive Story and required material fact is represented in the final trace with valid evidence. No unsupported cause, scope, status, or geography is introduced by grouping or editing.
7. A valid digest fits within Telegram's 4096-character ceiling, counting the complete message after entity parsing. Admission must make the frozen plan feasible before writing. A rich 24-hour window should be compacted through synthesis; if it still cannot fit after the bounded repair, generation fails closed with a clear budget diagnostic.
8. The final audit evaluates the exact rendered preview text. It must not report `is_clean=true` when any blocking issue above remains.
9. Server verification uses a no-delivery preview only. It generates both the representative 24-hour window and, if needed to isolate the original failure, a 12-hour preview. No Telegram send is part of acceptance.

## 6. Implementation boundaries and verification

Expected code areas: `src/publication/selection.py`, `digest_presentation.py`, `digest_narrative.py`, `digest_editor.py`, `digest_quality_diagnostics.py`, `generation.py`, and `renderers.py`. Exact boundaries may narrow during planning.

Do not change the article pipeline, ingestion, Event-First knowledge semantics, city-specific geography profiles, or model allowlist. Do not add per-message generative processing. Keep quality logic generic across editions and use configured edition profiles for local areas.

Follow the user's preference to skip TDD. Verification for this task is focused static checks plus the authorized server-side digest previews after the implementation is pushed/deployed. The generated preview text must be returned to the user for editorial review; preview generation must not send to Telegram.

## 7. Non-goals

- Redesigning the evening long-read article or its separate composition plan.
- Requiring official confirmation or multiple independent sources for a legitimate local report.
- Removing eligible Event-First Stories from stored knowledge.
- Solving cross-story duplicates by forcing all raw messages into a prose-only prompt.
- Changing Telegram delivery behavior or sending a live digest.

## 8. Architecture review amendments — normative

These refinements resolve gaps in the initial design. Section 0 of AGENTS.md wins over older dashboard and zero-omission descriptions elsewhere in the repository.

### 8.1 Budget admission before coverage becomes mandatory

Frozen upstream eligibility and selection for this specific post are different decisions. Event-First candidate sealing has already applied Gate retention/scope. Do not reapply selector hard labels as candidate-level exclusion. Build coherent fact groups first, estimate their compact faithful cost, reserve the actual title/rubric/statistics overhead, and admit a feasible plan. Prioritize impact, urgency, current usefulness, and domain breadth; use deterministic tie-breaking. Target 12–16 topic bundles only on rich days, never as a minimum or one-item-per-bundle quota. Secondary private inquiries and routine commercial notices cannot displace substantive utility, safety, transport, connectivity, or civic-service updates.

Persist candidate dispositions: admitted or deferred by digest budget with priority/cost explanation; preserve selector hard labels as nonbinding diagnostics. Report frozen eligible-to-admitted coverage separately from admitted-to-published coverage so a 100% score cannot hide excessive preselection loss. Record upstream hard-exclusion counts only when the frozen run can supply them. Never delete knowledge or apply digest deferrals to article selection. Once frozen, every selected Story and required fact must survive. If essential material still cannot fit, return a specific planning/budget failure; do not silently trim requirements after writer failure. Cost estimates are estimates, so final rendered measurement remains authoritative.

### 8.2 Fact relations, geography, and temporal meaning

Keep four explicit relations: SAME_FACT, UPDATE_OF, LOCAL_CONTRAST, and RELATED_ONLY. Only SAME_FACT permits removal of a redundant mention. Support overlap alone does not establish equivalence: one source fragment can contain several different claims. RELATED_ONLY may share a rubric but does not authorize a merge. UPDATE_OF retains a material transition instead of flattening old and new states into a contradiction.

Every required fact carries its service/subject, canonical location and original location wording, observed/effective time, source publication time, epistemic kind, and allowed support IDs. Distinguish unknown time from simultaneous reports. Use edition timezone and the frozen cutoff for relative dates. Recency alone cannot establish that an older report is false or that a service remains unavailable now; use bounded wording when current state is unknown.

Resolve aliases using edition data. Never infer a district from elevation, a street name, or another item's context. Unknown geography remains explicit and cannot inherit a neighbor's area. Preserve the original location qualifier and make ambiguity a composition constraint. A Story containing facts from several locations must be partitionable at the fact level; every subclaim retains its own location and supports. Geographic/topic/time compatibility must hold for every merged member; transitive pairwise overlap is not sufficient.

Keep each area's related material contiguous within its rubric where practical. Sort by reader priority, then coherent area/service order; do not alternate between the same areas unnecessarily. Rubrics organize presentation and must never relabel an electricity fact as water, connectivity, or a strike. Current rubric-based topic remapping must be audited where it touches the new composition path. Existing city-specific grouping anchors in generic code must not be copied into the new mechanism.

### 8.3 Natural writing with sufficient context

Supply one compact evidence packet containing grounded facts, allowed source excerpts for ambiguous wording, dates, geography constraints, and relevant context. Atomic metadata must not become prose templates. The writer composes the whole digest in one call with editorial hierarchy, short natural sentences, optional scan-labels, and concrete practical details. Give the editor the whole short draft for rhythm and repetition, but allow changes only to named affected units and explicitly allowed merges; include the evidence for those units.

Keep community attribution when needed; removing repeated attribution must not upgrade an unofficial report to established fact. Questions, sarcasm, military observations, repairs, and separate utility domains must retain their original meaning. No glossary can license a claim absent from evidence. Edition-specific examples belong in edition context or review cases, not generic prompts. No dashboard, duplicate lead, or synthetic footer; statistics appear only when configured.

### 8.4 Honest validation and bounded execution

Deterministic checks can prove identifier membership, measured length, and some structural defects; they cannot guarantee natural prose or general semantic entailment. Semantic duplicate heuristics must expose their evidence and uncertainty. Do not describe a clean automated audit as proof of editorial excellence. Preserve separate factual/coverage checks, structural findings, and editorial observations, with PASS/FAIL/NOT_EVALUATED states; incomplete checks cannot silently count as passed. Distinguish is_publishable (all required hard checks pass) from no_style_warnings.

Each blocking finding includes a code, affected item/fact IDs, visible text span where applicable, evidence, and an actionable repair. Apply existing deterministic validation to all visible claims, including headline assertions, after edits; ID membership alone does not count as claim validation. Reject violations that current checks can establish, including unsupported high-risk novelty and relations. Mark general semantic entailment as NOT_EVALUATED when no reliable check exists, and include it in editorial review; do not report it as proven by identifier membership or audit cleanliness. Style preferences are advisory unless a concrete product violation is established.

Use one writer call and at most one repair call, not an autonomous critic loop. Reuse existing validators and provider retry/time-limit policy; count attempts at the run level so wrapper retries do not multiply editorial cycles. Persist stage start/end, provider request outcome, timeout/failure reason, and repair outcome. A failed run must terminate visibly rather than wait indefinitely. Do not introduce a new model or a production judge call without separate evidence of benefit.

Render once into the canonical delivery artifact (text plus entities, or parsed equivalent); audit and store a hash of that artifact. Preview and delivery must consume the same artifact without unaudited postprocessing. Measure Telegram's complete text after entity parsing, including title, labels, configured statistics, and separators. Validate entity offsets using Telegram's UTF-16 convention separately from text length; do not count Markdown markers as visible content. Never truncate to fit.

### 8.5 Acceptance must measure reader quality

Static checks and a single successful preview cannot establish improvement. Use a small editorial comparison corpus, not a new unit-test suite or TDD requirement. Freeze the inputs, cutoff, edition profile, model configuration, and semantic versions for old/new comparisons. If earlier preview inputs are unavailable, disclose that and use a reproducible available snapshot; do not claim historical parity. Include representative rich 24-hour and sparse windows plus focused cases for aliases, distant districts, unresolved geography, restoration over time, same-area conflicts, mixed ads with useful facts, questions/sarcasm, and repeated source reports. Cases may overlap in one snapshot.

Review actual texts side by side, preferably with labels concealed, on factual fidelity, scanning ease, useful breadth, repetition, geography/time clarity, natural Russian, and practical specificity. Record concrete evidence and lost details, not only a numeric average. The new result must fix the reported defects, preserve essential selected facts, and show no critical factual/geographic regression. Review eligible-but-deferred facts as well as the post itself. Repeat the richest and most ambiguous cases once to expose obvious generation instability; do not promise statistical certainty from two samples. No Telegram delivery during evaluation.

Report generation failures, budget failures, latency, and repair rate alongside prose quality. A system that suppresses most editions is not an editorial success. Keep the prior implementation available for code/config rollback, but never use rejected old text as an automatic publication fallback. Verify current persisted Gate/Analysis versions; publication improvements do not retroactively repair bad upstream evidence. If that prevents honest output, identify the upstream blocker instead of weakening validation.

### 8.6 Chosen architectural scope and alternatives

Recommended: strengthen the existing deterministic planning → single writer → bounded editor workflow, with typed fact relations, pre-write budgeting, and measurable reader review. Extend existing presentation types and validators rather than maintain a second competing plan or generic orchestration framework.

Prompt-only changes are cheaper but leave admission, coverage, and geometry failures uncontrolled. A multi-agent newsroom with separate planner, writer, critic, and repeated rewrites increases cost, latency, and opportunities for factual drift; reserve that experiment for evidence that the simpler workflow cannot meet the quality bar. Neither alternative is part of this implementation.

The design aims for measurable improvement, not a guarantee of an “ideal” digest. Promotion depends on observed text quality and failure rate, not audit cleanliness alone.

## 9. Primary references and how they inform this design

- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents): start with simple composable workflows, add complexity only when useful, and bound refinement. Applied here as one writer and one conditional repair.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): evaluate outcomes with multiple complementary methods and realistic cases. Applied here as grounded checks plus comparative human reading and operational measurements; no judge is treated as proof of quality.
- [Telegram Bot API: sendMessage](https://core.telegram.org/bots/api#sendmessage) and [MessageEntity](https://core.telegram.org/bots/api#messageentity): 4096 characters after entity parsing and UTF-16 entity offsets. Applied to the exact canonical delivery artifact.
