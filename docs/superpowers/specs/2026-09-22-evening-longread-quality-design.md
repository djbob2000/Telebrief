# Evening Long-Read Quality: Grouping, Time Context, and Reader Review

**Status:** Draft for user review

**Scope:** Event-First city-life articles; digest behavior is unchanged.

## 1. Problem

The 2026-09-22 Berdyansk article was generated through the Event-First writer, not a technical fallback. It drew on 88 selected source inputs and produced 70 Story packets, then used two targeted editor passes. The final draft had no unsupported claims reported by the final validator, but still read like a sequence of short updates rather than a relaxed evening long read. Its original draft had 18 validation findings before editing; final Story coverage was 68/70 and detail-support coverage was about 88%.

This points to a composition problem across the whole path, not simply an inactive editor: too many independent packets compete for paragraph space; promotional and directory-like copy can reach the writer beside useful local information; some packet renderings do not give the writer the support's temporal role and local timestamp; and the existing factual validator does not assess whether the prose reads as a connected article.

The Telegra.ph page's visible title/byline treatment is not part of this design. The stored article body did not duplicate the title, and the apparent duplication came from page presentation.

## 2. Goal

Make a rich day's article read as one coherent, pleasant city-life story while keeping its local breadth and evidence fidelity. The pipeline should:

1. Keep useful community reporting, including legitimate single-source reports and short practical details.
2. Reduce promotional and directory payload before it consumes writer attention, while preserving any useful city-life fact inside that material.
3. Group related Stories for composition without changing their underlying evidence or erasing distinct places, times, service states, or unresolved differences.
4. Give the writer clear local time context and each cited support's temporal meaning.
5. Assess the completed article for cohesion and readability, then make bounded, targeted edits and strictly revalidate the result.

The article remains a broad city-life long read. Major developments receive room; useful supporting material is woven in; smaller legitimate items can remain brief. Compression removes repetition and advertising payload, not lived detail.

## 3. Product Constraints

- Preserve the Event-First pipeline and one main article-writer call. Do not add claim-first or per-message generation.
- Do not add a general `ARTICLE_WORTHY` yes/no gate or a corroboration threshold.
- Do not let article grouping or prominence become a new authority to discard a legitimate hard-eligible Story.
- Keep `DEVELOP`, `WEAVE`, and `BRIEF` as depth and presentation guidance, not eligibility labels.
- Preserve source provenance, evidence kind, uncertainty, quote rules, and the strict Evidence Boundary. Every edit is revalidated.
- Do not chase a mechanical 100% Story/detail score by appending fragments, filler, or boilerplate. Track gaps and repair meaningful omissions in context.
- Do not hardcode Berdyansk, its geography, dates, or its article wording into generic production logic. Use the article only as a regression fixture.
- Do not change digest selection, formatting, or budget as part of this work.

## 4. Proposed Flow

```text
sealed Event-First evidence
    → existing eligibility and publication policy
    → article-context cleanup and useful-fact extraction
    → thematic composition bundles with traceable Story/support IDs
    → one writer call with local as-of time and temporal evidence
    → reader-quality diagnostics
    → targeted ArticleEditor patches when needed
    → strict Evidence Boundary and coherence revalidation
    → publish a cohesive article or fail closed
```

### 4.1 Early treatment of advertising and directory payload

Use existing evidence and publication semantics to distinguish useful local information from commercial payload. This is a context-preparation step, not a new broad eligibility gate. Apply deterministic cleanup to obvious directory fields such as phone lists, booking links, and repeated sales copy. For ambiguous items, retain the compact supported city-life fact and lower its prominence rather than dropping it.

- If a notice contains a useful civic or city-life fact, retain that fact at an appropriate depth and remove contact lists, booking links, repeated sales language, and other directory payload from writer-facing text.
- A genuinely useful local offer or service change may remain as `BRIEF`; being commercial or single-source alone is not grounds for suppression.
- Pure classified, repetitive promotional, or directory-only material may be excluded only under existing publication policy or a high-confidence, auditable commercial-only classification. Do not add a per-Story LLM eligibility call; do not reinterpret a sealed legitimate community report as an advertisement just because it is informal.
- Keep original source text and provenance internally. Record the writer-context projection and why payload was shortened or excluded.

The filter must be evaluated on two kinds of examples: directory-heavy notices with one useful civic fact, and ordinary community reports that superficially resemble an announcement. The first should retain its useful fact; the second should remain publishable.

### 4.2 Thematic bundles for composition

Add a composition-level grouping over the existing article plan. Group Stories that share a supported subject, situation, and useful narrative relationship. Do not alter Event clustering or merge the underlying Story/evidence records.

Each bundle carries:

- its thematic purpose and lead Story;
- member Story IDs and their existing depth/rank;
- member support IDs, evidence kinds, and claim traceability;
- distinct location and time values needed to express local contrasts;
- detail supports selected for concrete resident experience.

The writer may synthesize several related Stories in a paragraph or chapter. A bundle must not imply that distinct streets, dates, service domains, or reports describe the same condition. If evidence differs by place or time, provide that contrast explicitly. Existing recognized street aliases should remain canonicalized by the current geography layer.

Grouping is only a presentation aid. Every included Story remains auditable in the plan and final diagnostics; grouping does not require one paragraph or sentence per Story.

### 4.3 Time and place in writer context

Add an explicit `as_of` timestamp for the publication snapshot, rendered in the edition's configured local timezone and human-readable to the writer. Each selected support should also expose available temporal fields: source-observed time, effective time or interval, and temporal role such as current, past, scheduled, or uncertain. Missing values must remain missing rather than be inferred.

The writer instructions should distinguish:

- when a report was observed or published;
- when the described service state applies;
- whether an event is scheduled, ongoing, ended, or not yet confirmed.

For the regression case dated 22 September, a schedule ending on 22 September must not be narrated as an upcoming deadline after that date. Local timestamps should be used for reader-facing chronology; UTC remains available as source metadata for traceability.

### 4.4 Cohesive writer brief

Replace overlapping paragraph-count and per-section quotas with a compact brief. It should ask the writer to:

- build an opening that gives the reader a feel for the reporting window;
- organize evidence into a few natural thematic movements, with the number and depth determined by the material;
- give major lines room and weave related small facts into them;
- retain a useful smaller fact briefly when it does not belong inside a major line;
- prefer supported concrete details over generic phrases;
- vary transitions and paragraph shape, avoiding a repeated Story-summary cadence;
- attribute community reports naturally without exposing internal source mechanics;
- preserve ambiguity or localized differences instead of smoothing them into false consensus.

The brief must not request invented scenes, thoughts, sensory detail, motives, causes, or city-wide trends. No detail may be added only to make the prose more literary.

### 4.5 Reader-quality review and targeted editing

Add a reader-quality report alongside existing factual validation. It should flag actionable issues, not generate a numeric style score that the writer optimizes mechanically.

The report covers:

- **Cohesion:** abrupt topic changes, repeated openings, and article sections that read as independent bulletins.
- **Rhythm:** excessive one-sentence units, repetitive paragraph shapes, and overloaded lists or quote rolls.
- **Specificity:** generic summaries where the context provides a relevant concrete action, local detail, or time.
- **Clarity:** unclear attribution, chronology, referents, or transitions.
- **Internal consistency:** conflicting service, place, or time claims that were not expressed as a supported local contrast.
- **Coverage signals:** omitted major lines or useful planned facts, with evidence IDs for targeted repair; this is diagnostic, not a demand to reproduce every source row.

Deterministic checks should handle objectively detectable structure and consistency risks. Use the existing targeted `ArticleEditor` path to assess and patch prose issues within the same bounded repair process; do not add a separate routine reviewer call. The editor must not decide factual truth or publication eligibility. Patch only affected units and cap attempts using the existing bounded repair policy. Do not rewrite the whole article for a local issue.

After each patch, rerun strict Evidence Boundary validation, including quote, entity, number, temporal, causal, and support checks. Also rerun coherence checks on changed claims. An omitted major planned storyline is a targeted repair finding; it must not be silently dropped. If unsupported material, a major omission, or a serious article-level coherence failure remains after bounded repair, fail closed with the existing article rejection path. Do not publish a deterministic concatenation or add synthetic filler.

## 5. Diagnostics and Replayability

Persist a compact, versioned quality record with the article generation metadata:

- writer-context and prompt version/hash;
- snapshot `as_of` and timezone;
- number of source inputs, Story packets, composition bundles, and supports;
- retained useful facts and shortened/excluded payload counts with reasons;
- reader-quality findings by unit and severity;
- editor patch units and repair count;
- final factual/coherence validation result and unresolved coverage diagnostics.

Keep source provenance and the published result traceable. Do not persist provider secrets. The metadata should make it possible to tell whether a later weak article came from noisy context, grouping, temporal projection, drafting, or repair without requiring a raw provider-response dump.

## 6. Acceptance Criteria

1. An article is composed from thematic bundles; it does not default to one short paragraph per Story.
2. A directory-heavy item with one useful local fact contributes that fact without dumping its contacts and sales payload. Useful single-source community reports survive with natural attribution.
3. Writer context exposes the publication's local `as_of` time and support-level temporal roles when known. The article does not call an already-effective date future.
4. Different service states in different places or time windows are written as localized/time-bounded contrast, not adjacent contradictory city-wide assertions.
5. The writer keeps supported practical detail when it explains residents' experience, while avoiding unsupported literary embellishment.
6. Reader-quality findings cause targeted edits only where appropriate. All patched claims pass the strict Evidence Boundary; unresolved serious issues fail closed.
7. Coverage diagnostics remain visible, but no raw-fragment dump or filler is used to raise a coverage percentage.
8. No change is made to digest output, Event clustering, source truth, or per-message LLM call volume.

The 2026-09-22 article should be used as a qualitative regression example for list-like composition, promotional payload, local-date framing, and service/time contrasts. It must not become a city-specific prompt rule.

## 7. Non-Goals

- Rewriting or republishing the existing Telegra.ph article as part of pipeline design.
- Changing ingestion, Gate semantics, Event clustering, or digest editorial behavior.
- Requiring multiple sources, deleting all commercial notices, or imposing a fixed article length or section count.
- Adding a separate whole-article regeneration loop or routine extra model call.
- Reproducing every source message or reaching a coverage percentage at the expense of a readable narrative.

## 8. Implementation Boundaries

Implementation should be split into independently reviewable changes: (1) writer-context filtering and temporal fields, (2) thematic bundle materialization and prompt simplification, and (3) reader-quality diagnostics and targeted patch integration. Each change should retain deterministic provenance and fail closed under the existing article publication contract. Tests and fixture updates belong in the implementation plan after this design is reviewed.
