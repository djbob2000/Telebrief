# Evening Long-Read Composition and Quality Gate

**Status:** Draft for user review
**Scope:** Event-First city-life articles. Digest generation and delivery are unchanged.
**Relationship:** This specification refines the approved direction in [`2026-09-22-evening-longread-quality-design.md`](2026-09-22-evening-longread-quality-design.md) based on a server-side dry-run of frozen publication run 189.

## 1. Purpose

Make a rich reporting window read as a pleasant evening city-life long read: one coherent account with a clear hierarchy, local texture, natural movement between themes, and no invented facts. Keep legitimate local reporting and useful concrete details available to the writer, while preventing the article from turning into a directory or a roster of source messages.

The target remains the product contract in `AGENTS.md` §0.3–0.10:

- Major city developments receive room; supporting stories enrich them; smaller useful information may stay brief.
- Grouping is for presentation. It does not alter Event-First evidence or silently delete a legitimate hard-eligible Story.
- `DEVELOP`, `WEAVE`, and `BRIEF` express depth and prominence, not eligibility.
- Compression removes repetition and fragmentation, not supported reality.
- Community reports remain publishable with honest attribution; neither a second source nor official confirmation is generally required.
- The Evidence Boundary remains strict. Every material edit is checked against its supporting evidence.
- A cohesive article is the goal. Mechanical coverage chasing, raw source concatenation, and synthetic filler are prohibited.

## 2. Evidence from the September 23 dry-run

The server-side dry-run used frozen Event-First inputs from publication run 189 and the latest committed article code. The app services were not restarted and the result was not delivered. The writer materialized 205 Story packets, with a context of 109,035 characters and a total prompt of 176,565 characters (59,519 input tokens in the provider response).

The returned article still showed the failure shape this work is meant to address:

- Electricity reports became a long address-and-duration roster inside a few long paragraphs.
- A broad “power” theme combined different narrative material: outages, voltage drops, solar panels, and prices.
- Small notices and household/service details accumulated in a catch-all city-life chapter; the Ozon point-of-sale fact appeared more than once.
- The opening, body, and closing revisited the same city-wide waiting theme without enough progression.
- A generic opening promised a festive strand that the body barely developed.

The first writer draft contained 1,337 words and covered 191 of 205 planned Stories. Its validation and reader-quality findings triggered targeted editing. The visible list and repetition patterns were not reliably identified as article-level composition failures. The current code explains why: the writer receives both per-Story packets and a second detailed plan, composition groups by broad keyword signatures, the city-life section is a residual catch-all, and inventory detection checks runs of short one-sentence paragraphs rather than overloaded paragraphs. Some `repair` findings may also remain after the bounded editor loop because only `blocking` quality findings stop finalization.

Relevant implementation surfaces include `src/publication/article_composition.py`, `src/publication/article_coverage.py`, `src/publication/article_writer_context.py`, `src/article_generator.py`, `src/publication/article_quality.py`, `src/publication/article_editor.py`, and `src/publication/article_finalization.py`.

## 3. Proposed architecture

Keep the existing Event-First evidence pipeline and article writer call. Refine the intermediate editorial composition and the final reader-quality gate:

```text
ArticleCoveragePlan (complete eligible material and depth)
        ↓
relation-aware composition map (narrative lines, groups, contrasts)
        ↓
one compact writer brief + each Story/support packet once
        ↓
one main article-writer call
        ↓
deterministic reader-quality diagnostics
        ↓
targeted ArticleEditor patches when needed
        ↓
strict Evidence Boundary + final reader-quality gate
        ↓
verified cohesive article or ArticlePublicationRejected
```

The composition map remains a presentation view. Story, support, source provenance, evidence kind, location, time, uncertainty, and service-state evidence remain independently traceable. No component may use thematic grouping to upgrade a community report, infer an unsupported relation, or delete a legitimate Story.

### 3.1 Relation-aware narrative map

Retain `ArticleCoveragePlan` as the complete source of candidate article Stories, rank, and depth. Replace the current single-level `(section_id, keyword_signature)` grouping as the only composition structure with a hierarchical map:

1. **Narrative lines** describe coherent reader-facing developments or themes for the reporting window.
2. **Evidence groups** collect Stories with a supported relationship, such as the same service issue and compatible time/state, a meaningful localized contrast, or one shared practical consequence.
3. **Story/support members** preserve the exact evidence and provenance behind every grouped point.

Use the edition geography resolver and evidence fields where available. Effective/event time orders the described situation; `observed_at` orders reports and must not be presented as the start time of a service state. Keep unknown times unknown. Do not transfer distance or location relationships between distinct neighborhoods. Existing recognized aliases can resolve to the same physical place.

Grouping rules:

- A broad domain such as electricity is a subject, not sufficient evidence that every electricity report belongs in one undifferentiated group.
- Combine reports only when their subject and narrative relationship are supported. Preserve positive/negative or time-varying evidence as explicit local or temporal contrast, never as a uniform city-wide state.
- When the relation is ambiguous, keep Stories distinct in the map. Do not guess proximity, cause, chronology, or shared mechanism.
- Keep every hard-eligible Story represented in the coverage/composition metadata. A group may provide one cohesive narrative treatment for related members; prose does not need one sentence or paragraph per source Story.
- Preserve concrete details that materially change the reader's understanding: specific places, intervals, service state, resident action, cost, or practical consequence. Do not flatten them into generic language.

Remove section-spreading behavior that moves a Story only to manufacture a minimum number of headings. Do not force every unmatched or standalone Story into a full “City life” chapter. A small useful item remains available at `BRIEF` depth and can be placed compactly where it has a reader connection; if no such connection exists, it may remain a short independent mention without a fabricated bridge. Do not add a new `ARTICLE_WORTHY` yes/no gate, a corroboration threshold, or a new promotion-only policy.

### 3.2 Single writer-facing roadmap

The writer should receive one coherent brief rather than two inventories. The roadmap should include:

- the central reader-facing lines and their relative depth;
- the role of each evidence group, such as development, localized contrast, practical consequence, or brief supporting detail;
- the supported place/state/time distinctions that must remain explicit;
- Story/support IDs only as compact traceability references where needed.

The factual packet section remains the writer's source of exact claims. Do not repeat the complete Story list, support IDs, and bundle membership a second time in a parallel plan block. Avoid repeating all group members in every Story packet, including the full-context materialization path.

The prompt must tell the author to:

- open on a supported central development and give the reader a natural local-time frame;
- organize the article around coherent movements, allowing chapter count and paragraph shape to follow the material;
- synthesize shared conditions without listing every address simply to show coverage;
- retain significant local exceptions and contrasts that change the story;
- weave related practical details into the relevant narrative and mention each useful notice once;
- avoid repeating the lead's thesis in every chapter or restating it as a generic conclusion;
- use effective times for event chronology and report times only for attribution chronology;
- write natural epistemic attribution without exposing internal source or Telegram mechanics;
- avoid unsupported causes, relationships, scenes, intentions, or city-wide generalizations.

The title and section headings must be distinct from one another. The lead should introduce a line that the body develops; it should not promise a major strand that receives no meaningful treatment. These are quality constraints on the assembled reader-facing article, irrespective of whether the title is rendered by the destination platform or by Markdown.

Do not impose a fixed article length, heading count, address count, or one-paragraph-per-topic template. Keep the existing length profile as guidance. Do not add a separate routine LLM planning or reviewer call: retain one main writer call and use the existing targeted editor only when diagnostics identify an actionable issue.

### 3.3 Material projection boundaries

Keep the current conservative distinction between useful local information and directory payload. Early material projection may trim contact lists, booking links, and repeated promotional copy while preserving a useful supported civic fact. This revision must not globally remove commercial, conversational, single-source, or informal material. A short community report or useful local notice remains eligible under existing publication policy.

Handle article-level repetition in the composition/draft stage: the same useful fact should have one natural home. Do not turn de-duplication into an eligibility decision, and retain raw provenance internally for validation and traceability.

### 3.4 Reader-quality diagnostics and targeted editing

Extend `ArticleReaderQualityReport` with high-confidence structural checks that cover the observed forms:

- **Overloaded roster paragraph:** many distinct place/state/time claims are packed into a long list-like paragraph without a meaningful comparison or narrative relationship.
- **Cross-section repetition:** the same supported development is restated across the lead, one or more chapters, and the closing without adding a new state or consequence.
- **Heading/lead duplication:** repeated title and section heading, repeated section headings, or an opening promise that the body does not develop.
- Keep and refine existing checks for one-fact paragraph runs, quote rolls, missing major `DEVELOP` storylines, useful detail signals, and contradictory service states.

Detection must be evidence-aware and generic. Use support/Story links and edition place resolution where available. A compact list of places can be valid when the locations establish a real contrast; avoid rejecting it solely because it contains several place names. Do not make a numeric style score or tune prose to maximize coverage percentages.

The existing `ArticleEditor` remains a targeted patcher. For an article-level pattern it receives the target paragraph or heading, adjacent paragraph text and headings for context, and only the relevant projected supports. Its instructions should be finding-specific:

- condense repeated conditions while preserving the meaningful local/time contrasts;
- retain concrete details with reader value and remove redundant enumeration;
- consolidate a repeated claim into its best-supported narrative location;
- repair a heading/lead mismatch without inventing a new development;
- preserve direct-quote exactness or use indirect speech;
- leave facts outside the targeted unit unchanged.

Each patch must be re-grounded and strictly revalidated for claims, source support, quotes, entities, geography, numbers, time, causal relations, and service-state consistency. Rerun reader-quality diagnostics against the edited draft and its final rendered form.

Severity semantics:

- A minor omission of one optional supporting detail is diagnostic and does not by itself block an otherwise safe article.
- A high-confidence serious composition defect, including a sustained address roster or repeated major storyline, requires an edit attempt.
- If a serious defect remains after the configured bounded editing attempts, fail closed through `ArticlePublicationRejected` with the quality findings and patch history in metadata. Do not treat an unsuccessful repair attempt as a clean pass.
- Warnings that do not materially damage reader comprehension remain visible in diagnostics but do not cause rejection.

Factual safety remains authoritative and independent of editorial style: unresolved unsupported claims continue to fail closed. Deterministic fallback concatenation, raw-fragment dumps, and synthetic filler remain prohibited.

### 3.5 Replay and diagnostics

Persist compact, versioned generation metadata sufficient to identify where a poor result came from:

- context and prompt hashes, local snapshot time, and edition timezone;
- Story count, narrative-line/group count, group membership, group-size distribution, and support counts;
- material projection actions and reasons, without copying contact payload into diagnostics;
- prompt/context character counts and which packet representation was rendered;
- quality findings before/after editing, patched unit IDs, attempt count, and unresolved findings;
- final Evidence Boundary result and final quality-gate result.

Do not persist provider secrets or unnecessary raw response copies. Provide a reviewable dry-run path using a frozen publication input and prevent all delivery side effects. For edition-local time rendering, the article runtime must have the edition's configured timezone available. Missing timezone data should fail clearly before the writer call; never silently substitute UTC or invent a local date.

## 4. Failure behavior and safety

- Ambiguous grouping leaves the material separate; it never hides it.
- A grouped narrative must preserve the member evidence's original epistemic status and support trace.
- Distinct service, place, and temporal states must remain distinct in prose unless evidence explicitly resolves the difference.
- High-impact factual and serious coherence issues are fail-closed after bounded repair.
- Low-priority coverage/detail diagnostics are not a requirement to append raw facts or filler.
- No digest behavior, source ingestion, Gate semantics, Event clustering, delivery, or scheduled publication behavior is changed in this scope.

## 5. Acceptance criteria

1. Composition metadata represents every non-projection-suppressed coverage Story exactly once in a narrative line/group and preserves each Story's own support IDs and depth.
2. Groups are not formed from a broad keyword alone when place, time, state, or subject evidence shows materially different situations. Local and temporal contrasts remain traceable and are expressed explicitly.
3. The writer-facing prompt has one compact roadmap. It does not repeat the complete Story/support roster in a second outline or repeat the same full group-membership header for every member.
4. The writer brief does not request listing every neighborhood in one sentence. It preserves meaningful supported locations and consequences while giving them a clear hierarchy.
5. Unrelated useful `BRIEF` items are not forced into an unrelated city-life chapter and are not repeated in multiple sections. Classified/directory trimming continues to preserve any supported useful fact.
6. Generic regression examples show that a long address roster, semantic lead/body repetition, and title/heading duplication are detected. A legitimate localized contrast and a short single-source community report are not falsely rejected.
7. Targeted editor patches receive neighboring context and narrow evidence. Every patched unit passes strict factual validation.
8. An unresolved serious composition finding after bounded editing causes fail-closed rejection. An isolated optional detail omission or non-actionable warning does not.
9. The September 23 dry-run and synthetic multi-edition fixtures are evaluated as qualitative regression examples; no city-specific names, streets, or dates become generic prompt logic.
10. The implementation adds no per-message LLM calls or routine extra reviewer/planner call, does not change the digest, and does not add an `ARTICLE_WORTHY` gate or corroboration threshold.

## 6. Verification outline

The implementation plan should include focused tests for grouping boundaries, contrast preservation, local/unknown time behavior, duplicate-plan rendering, directory mixed notices, single-report retention, list/repetition/heading diagnostics, finding-specific editor patches with neighboring context, and final rejection when a serious finding remains unresolved. An integration fixture should replay a frozen article context representative of the September 23 failure shape and inspect the assembled reader-facing article plus quality metadata.

The verification target is structural readability and evidence fidelity, not exact wording or a fixed number of cited source rows. A full server dry-run must use frozen inputs, current committed code, local timezone data, and an explicit no-delivery mode.

## 7. Non-goals

- Rewriting or publishing the September 23 article.
- Changing the digest, Event clustering, Gate, ingestion, or publication selection semantics.
- Requiring multiple sources or official confirmation for a community report.
- Suppressing an otherwise legitimate Story because it is brief, conversational, or commercial in source form.
- Adding a second routine generative planning/review stage or per-message LLM calls.
- Forcing all hard-eligible source material into a one-row/one-sentence narrative inventory.
- Weakening the Evidence Boundary or enabling deterministic article fallback.
