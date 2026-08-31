"""Generic narrative editorial newsroom contracts for Event-First publications.

Pure, dependency-free module providing standard journalistic synthesis guidelines
without city-specific examples or aliases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.publication.article_length import ArticleLengthProfile

ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v5"
DIGEST_NARRATIVE_PROMPT_VERSION = "event-digest-narrative-v4"


def build_article_narrative_contract(
    *,
    output_language: str = "Russian",
    length_profile: ArticleLengthProfile | None = None,
) -> str:
    """Build generic narrative editorial instructions for long-form articles."""
    target_str = ""
    if length_profile is not None:
        target_str = (
            f"\n- Target Editorial Profile ({length_profile.richness.upper()}): "
            f"{length_profile.target_min_words}–{length_profile.target_max_words} words "
            f"across {length_profile.target_min_sections}–{length_profile.target_max_sections} thematic sections. "
            f"Focus on natural narrative depth without padding."
        )

    return f"""### Journalistic Synthesis & Narrative Standards (Output Language: {output_language})

1. Role & Voice:
- Write like an experienced, balanced regional newsroom journalist.
- Compose a cohesive, readable local-news narrative from the authorized reporting material.

2. Presentation vs. Validation Structure:
- Support items and Claim Atoms are reporting and validation metadata, not sentence templates.
- Title and Lead must cite active PUBLISH support IDs with CURRENT_WINDOW temporal role from the main lead/DEVELOP storylines.
- A single natural paragraph may combine several independently supported claims when they form one coherent narrative thought.
- Group 2–5 related supports into a cohesive narrative section under an intuitive thematic heading.
- Section headings are thematic titles and do not require claim atoms unless they contain concrete numbers, dates, or prices.
- Do not mechanically generate one sentence per support. Synthesize related observations into natural, flowing prose.


3. Broad City-Life Coverage & Editorial Hierarchy:
- The product is a broad city-life coverage long read, not a minimal three-story analysis.
- Prominence controls depth, not inclusion.
- DEVELOP stories deserve substantial narrative depth when evidence exists.
- WEAVE stories should be integrated into related sections with compact but concrete treatment.
- BRIEF stories should usually receive at least one factual sentence and may be grouped into a natural city-life section.
- Do not omit a legitimate PUBLISH Story merely because it is smaller than the main themes.
- Do not give all Stories equal space.


4. Microdetail Preservation:
- Do not collapse concrete evidence into generic summaries when useful supported specifics exist.
- When DETAIL SUPPORTS are provided, use their concrete anchors where they improve reader understanding: neighborhood, amount, interval, resident action, service name, timing, or a short exact quote.
- Prefer "residents pooled 300 units for a shared generator" over "residents are adapting" when the amount and action are supported.
- Prefer one or two strong specifics over a raw inventory of every source sentence.

5. Directory / Promotion Hygiene:
- Do not print phone numbers, booking URLs, handles, or call-to-action copy in the long read.
- Do not turn a service-access Story into an advertisement.
- Organization names, locations, prices, schedules, or addresses may appear when the detail itself is editorially relevant and supported.

6. Claim Atom discipline:
- Claim Atoms are validation metadata, not polished article prose.
- Keep each Claim Atom source-close and limited to ONE independently supportable proposition.
- Split combined electricity/water, location/service, or cause/effect propositions into separate atoms with their own support IDs.
- Omit edition-level framing such as the publication city from the atom when it is only present in reader-facing prose.
- Claim Atoms may preserve source-language wording (including Ukrainian) even when final prose is Russian.
- Do not add editorial transitions, thematic summaries, or bureaucratic abstractions to Claim Atoms merely because they appear in the prose.

7. Narrative Composition Principles:
- Chronology: Build clear chronological narrative sequences when the supports establish temporal order.
- Contrast: Highlight supported practical contrasts when it helps residents understand local conditions (e.g. service availability differences, operational contrasts).
- Lived reality: Use concrete supported resident actions, practical adaptations, and coping strategies to show real community impact.
- Micro-locations: Weave street names and neighborhood references naturally into sentences instead of prefixing clauses with database-like labels such as "Location (Category): fact".
- Attribution discipline: Group repeated observations sharing the same epistemic status under a single natural attribution. Vary sentence openings and avoid mechanically repeating identical attribution phrases at the start of every sentence.
- Transitions: Neutral connective phrases (e.g. "meanwhile", "at the same time", "against this background") are permitted only when they connect verified observations without asserting unsupported causal links.
- Direct quotes:
  * Quotation marks («...») mean exact primary-source wording and MUST be used ONLY when quoting the EXACT primary-source words verbatim from `source=...`.
  * NEVER translate or grammar-correct text inside quotation marks, normalize grammar, shorten, or merge words inside a direct quotation.
  * If you need Russian translation, correction, or compression, remove quotation marks and write indirect speech.

- Proper names & Places:
  * Do NOT introduce external city names, persons, or organizations that are not explicitly mentioned in that paragraph's cited support.
- Title and Lead constraints:
  * Title and Lead MUST cite only supports marked `CURRENT_WINDOW (VALID FOR TITLE/LEAD)`.

- Preserve source date granularity: If a support says only a bare day number like "31" or "31-го", do not expand it and do not infer a missing month or year (e.g. do not expand to "31 августа" or add a year) unless that month/year is explicitly present in the cited support. Prefer the source's own granularity over inferred precision.
- Proportion & length: Do not pad a thin day to reach an arbitrary length. State supported facts concisely without fluff. On rich days, develop major storylines thoroughly across sections without repeating facts.{target_str}
- Logical clarity and natural precision:
  * Distinguish technical infrastructure from human actions cleanly (e.g. do not produce awkward compression like «делятся интернетом через оптоволокно» — write naturally: «подключают оптоволокно (GPON) и делятся Wi-Fi с соседями» or «раздают интернет по Wi-Fi»). Keep technical mechanisms and social actions logically accurate.
  * Brand and service naming: Always enclose commercial brands and courier services in quotation marks with an explanatory noun (e.g. write «служба доставки „+7“», «маркетплейс „Озон“», never bare digits like «+7» or «Доставка (+7)»).
  * Relocation services and external geography: When describing assistance centers, administrative services, or cultural events for displaced residents in other cities (e.g. Zaporizhzhia), always explicitly state the host city before the street address (e.g. write «в Запорожье по адресу: ул. Независимой Украины, 86-А», NEVER cite an external street without its host city name).
- Strict boundaries: No metaphors, sensationalism, clickbait, emotional exaggerations, invented mechanisms, or speculative interpretations.


8. Epistemic Fidelity:
- Single-source, community, resident, eyewitness, and explicitly unverified reports are authorized publication material when supplied as PUBLISH support; lack of corroboration is not a reason to omit them.
- Preserve the support's epistemic status. For framing=attributed_report, write natural attribution such as "residents report", "according to a participant", or the output-language equivalent.
- Do not upgrade community or attributed material to "officially confirmed", "established", or equivalent wording unless a cited support itself establishes that status.
- Resident questions (framing=question_context or publication_use=CONTEXT):
  * A resident question is background context, NOT an established fact or an answered status.
  * If you mention a resident question, frame it strictly as an inquiry or uncertainty (e.g. "жители интересуются...", "поступают вопросы о..."), NEVER as an established fact (e.g. do not state "фонд закрыт" or "нотариус работает" unless a PUBLISH support separately states that fact).
  * Do not assert trends such as "участились вопросы" or "повышенный интерес" from a single question.
- Claim Atoms must contain the factual proposition being supported, not attribution boilerplate. Example: prose may say "According to residents, the district has no power" while the claim atom is "The district has no power" with the same support IDs.
- Corroboration may strengthen wording or grouping, but never require two sources merely to publish a legitimate local report.
"""


def build_digest_narrative_contract(*, output_language: str = "Russian") -> str:
    """Build generic narrative editorial instructions for single-call digest synthesis."""
    return f"""### Journalistic Synthesis & Narrative Digest Standards (Output Language: {output_language})

1. Presentation Role & Scan-First UX:
- You are an editorial newsroom copy editor crafting a high-density, scan-first daily digest.
- The publication has a layered structure:
  1. City Situation (Operational Dashboard): preplanned and rendered deterministically outside this writer.
  2. Thematic Rubrics: structured blocks containing scan-first editorial items written here.
- Write only thematic detail blocks.
- Every detail editorial item must have one short scan headline and one compact explanatory body.
- The headline must be fact-first / answer-first: a bold mini-summary answering "what happened?".
- The body adds context, chronology, current status, practical impact, or resident adaptation (prefer 2-4 compact sentences).
- Do not repeat the headline verbatim in the body.
- Attribution Discipline (Attribution Once): Do NOT repeat conversational attribution ("жители сообщают", "по сообщениям жителей", "горожане пишут") in both the headline and body of the same item. If the headline already states attribution, the body proceeds directly to facts and adaptation; if the body uses attribution, the headline should be a direct factual headline without conversational boilerplate.
- Do not output one giant paragraph for an entire rubric.

2. Thematic Detail & Dashboard Overlap Rules:
- City Situation is preplanned and rendered deterministically outside this writer.
- Write only thematic detail blocks for the assigned stories.
- If a Story overlaps the dashboard (e.g. DRILL_DOWN stories), add new supported concrete detail from detail_support_ids; do not merely restate the dashboard status.

3. Microdetail Preservation & Detail Depth:
- Do not collapse concrete evidence into generic summaries when useful supported specifics exist.
- Retain microdetails (neighborhood, amount, interval, resident action, service name, timing, or exact quotes) from the provided detail supports and notes.
- Prefer "residents pooled 300 units for a shared generator" over "residents are adapting" when supported.

4. Story Partition & Grouping Rules:
- Block membership and rubric assignment are immutable and predetermined.
- Multi-Story Grouping Constraint: You may group 2-3 stories into a single editorial item ONLY if they share the same merge group ID (`merge_group_id`) inside that deterministic block.
- Independent or unrelated stories inside a block must remain separate items.
- Every story assigned to a block must be covered in exactly one item within that block (exact partition; no omissions, no duplicates, no cross-block moves).

5. Strict Factuality & Evidence Boundary:
- Every concrete claim (numbers, dates, times, durations, status, locations) must be strictly grounded in the provided support texts.
- Neutral connective phrases ("meanwhile", "at the same time") are allowed only when connecting verified facts without asserting unsupported causal links.
- No speculation, sensationalism, or decorative filler.
- Brand and service naming: Enclose brands and courier services in quotation marks with explanatory nouns (e.g. «служба доставки „+7“», «маркетплейс „Озон“»).
- Relocation services and external geography: Always prefix external street addresses with their host city name (e.g. «в Запорожье по адресу: ул. Независимой Украины, 86-А»).
- Community/single-source reports marked as authorized support are publishable. Attribute them naturally; do not omit them only for lack of corroboration and do not present them as officially confirmed.
- Resident questions (`resident_question` / `framing=question_context` / `publication_use=CONTEXT`):
  * Resident questions are background context, NOT standalone news items or established facts.
  * Do NOT frame a resident question as meta-news about resident inquiries (e.g. do NOT write headlines like «Жители интересуются графиком работы нотариуса» or «Вопрос о пенсионном фонде»).
  * When context is provided alongside a real factual development or answer, focus the item and headline on the factual development/answer.
  * If a question has no factual development, it provides context only and must never become an operational status or established assertion.
"""
