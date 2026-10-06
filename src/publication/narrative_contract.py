"""Generic narrative editorial newsroom contracts for Event-First publications.

Pure, dependency-free module providing standard journalistic synthesis guidelines
without city-specific examples or aliases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.publication.article_length import ArticleLengthProfile

ARTICLE_NARRATIVE_PROMPT_VERSION = "event-article-narrative-v14-reply-locator-only"
DIGEST_NARRATIVE_PROMPT_VERSION = "event-digest-narrative-v23-past-report-wording"

DIGEST_REPLY_CONTEXT_GUIDE = (
    "In reporting metadata, reply-parent, parent message and «родительское сообщение» "
    "mean the preceding message in a reply chain, not a person's parent. "
    "Support rows sharing canonical_evidence_ids are aliases of evidence, not additional witnesses. "
    "Source-item metadata identifies reports, not a count of distinct people. "
    "Reply context is background only: keep supplied parent text in reply_parent_context with kind background_only_not_citable; it is not citable support, the current author's wording, a quote, or another witness. "
    "Parent context may clarify the reply's referent or location only when the PUBLISH reply is the unique direct answer to its parent question; the writer may name that locator naturally without copying or quoting the parent's wording. "
    "An ambiguous, adjacent, or non-question parent supplies no locator. The parent must never support or add the reply's status, duration, cause, number, completion, scope, source role, or any other event detail. "
    "Report the supported update directly "
    "without describing reply mechanics. switch_clock_roles describes a time of day, never a duration: "
    "дали около 13 часов means an approximate 13:00 switch time, not 13 hours of supply. "
    "Do not infer a family relationship from "
    "reply metadata. Genuine family references in citable testimony remain valid "
    "reporting material; preserve them when part of the required facts."
    " Protected_details belong to their owning fact: keep each clock observation "
    "attached to its supplied location and service, and each price to its paid journey leg. "
    "These fields are navigation to exact evidence, not permission to infer a street, "
    "conditions on the rest of an area, or a restoration sequence from untimed reports."
)


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
            f"about {length_profile.target_min_words}–{length_profile.target_max_words} words and "
            f"{length_profile.target_min_sections}–{length_profile.target_max_sections} sections when the material supports them. "
            f"Treat both as soft editorial targets: let length and section count follow the reporting; never pad or invent detail to reach them."
        )

    return f"""### Journalistic Synthesis & Narrative Standards (Output Language: {output_language})

Output ordinary Markdown only: one title, a lead, and thematic chapters with normal prose paragraphs. Do not output JSON, Claim Atoms, support IDs, or technical trace data. These are internal validation metadata; the server derives them from the actual prose.

1. Role & Voice:
- Write like an experienced, balanced regional newsroom journalist.
- Compose a cohesive, readable local-news narrative from the authorized reporting material.
- Open on a supported central development and give the reader a natural edition-local as-of frame when available. Never infer an event start time from when a report was observed.

2. Presentation vs. Validation Structure:
- Evidence records and Claim Atoms are reporting and validation metadata, not sentence templates. Never print their IDs or metadata in the article.
- Ground the title and lead in active PUBLISH evidence with CURRENT_WINDOW temporal role from the main current storylines. Keep the title about the reporting window; do not present HISTORICAL_CONTEXT as current news. Historical context may appear in the title or lead only when current-window evidence supports it and the prose accurately frames a supported continuation or change.
- A single natural paragraph may combine several independently supported claims when they form one coherent narrative thought.
- Give each paragraph one central subject. When claims belong to unrelated storylines or independent editorial groups, place them in separate compact paragraphs, even inside a broad or catch-all section. Combine them only when the source material supports a real shared relation; never invent a transition, cause, or chronology to make unrelated items appear connected. This is a paragraph-composition rule, not a quota to give every Story its own paragraph.
- Keep each paragraph focused on one coherent subject. Distinguish service domains such as electricity supply and home internet, while allowing one paragraph or section to connect domains when the reporting directly supports their relationship; otherwise separate them. A transition alone does not establish a relationship.
- Keep geographic scope explicit: “private sector” is a housing type, not a district. If the cited report does not identify a neighborhood or street, say that its district was not specified; never borrow a location from a nearby report.
- Keep profile-mapped places attached to their own areas. Do not place an observation about one landmark or street inside another district's description unless the profile maps them to the same area. When several streets belong to one named neighborhood, introduce that neighborhood first, then explain the street-level differences and chronology.
- Group related supports into a cohesive narrative section under an intuitive thematic heading.
- Section headings: Use active, concise, and informative thematic headings (e.g. `## Перебои со светом и нагрузка на водоснабжение`). Avoid passive filler labels (e.g. "Связь тоже зависит от условий", "Обстановка в городе"). Do NOT use colons with enumerated lists of subtopics in headings to avoid phantom heading topic mismatches. Section headings are thematic titles and do not require claim atoms unless they contain concrete numbers, dates, or prices.
- Title (#): A city name followed by a colon is an optional editorial style. Ground the title and lead in current-window evidence. If either uses HISTORICAL_CONTEXT, it also needs CURRENT_WINDOW evidence and wording that accurately frames a supported continuation or change; a continuation verb alone does not establish that relationship.
- Lead-body distinction: The lead synthesizes the overarching 24-hour horizon. The first paragraph of the body must NOT repeat the lead verbatim or restate the same summary sentences; Chapter 1 must dive immediately into the concrete specifics, street names, and timeline of the central development.
- Context and uncertainty:
  * Avoid repetitive methodological commentary about the reporting process. Use natural attribution and concise scope or uncertainty framing when needed to represent what a source does and does not establish.
  * Keep factual assertions grounded. MISSING_CLAIM_SUPPORT concerns a claim atom with no cited support; it does not automatically apply to every framing sentence. Do not suppress accurate attribution or uncertainty merely to avoid a generic disclaimer.
- Geographic order & area consistency (prevention of ARTICLE_PLACE_AREA_MISMATCH):
  * Keep each paragraph focused on one neighborhood or topographical zone when possible. A concise localized contrast may name two areas in the same sentence or paragraph when the evidence supports conditions in both; one eligible source is sufficient, and no independent corroboration is required. Do not transfer details from one area to another or imply a citywide state from local observations.
  * There is no fixed district-count limit. Name places only as broadly as the evidence supports, keep each location attached to its own observation, and use a clear transition when contrasting areas. ARTICLE_PLACE_AREA_MISMATCH concerns assigning a place to the wrong area; mentioning several correctly grounded areas is not itself a mismatch.
  * When an area and a street are mentioned in the same paragraph, ALWAYS introduce the named area first, then the street (e.g. «В районе Азмола на улице Хмельницкого...»). Never place an unrelated district name between an area and its street.
- Prevention of address rosters (MULTI_SENTENCE_ADDRESS_STATUS_ROSTER):
  * Avoid repetitive address-by-address sentences; synthesize related reports into a clear account of their shared state, chronology, or differences. The structural roster finding applies when three or more grounded place/status sentences in one paragraph describe the same service without a narrative relation. The number of streets or sentences alone is not a blocker; preserve supported detail and make relevant contrasts clear.
- Thematic consistency: Keep placement clear and use a section heading that fits its main subject. Related domains may share a passage when their connection is directly supported. THEME_MISMATCHED_SECTION is a repair recommendation, not a publication blocker by itself.
- Quotes & Name styling: Treat the quote allowlist as the source for exact direct speech, not a quota. Use direct quotes sparingly and only when they add a useful human voice; never change, translate, or assemble their wording. Supported organization, shop, and place names may take Russian typographic quotation marks as styling; preserve the name's spelling and do not treat it as spoken dialogue.
- Do not mechanically generate one sentence per support. Synthesize related observations into natural, flowing prose.


3. Broad City-Life Coverage & Editorial Shape:
- The product is a broad city-life long read, not a minimal headline recap or a catalogue of every source item.
- Read the reporting material as a whole and choose the strongest connected city-life lines yourself; the order and labels of support records are not an outline.
- Give more space to developments that change residents' day or reveal the larger situation. Weave in smaller reports when they add a useful local contrast, consequence, or human detail. A static locator or answer to an individual question may be left out when it has no natural role in the reporting-window narrative; that does not make the source information false or unusable elsewhere.
- Do not give all Stories equal space and do not force a separate mention for every support record.
- Let the article's shape follow the evidence; do not impose fixed heading, address, paragraph, or one-paragraph-per-Story quotas.
- Preserve meaningful place, service-state, and event-time contrasts. Use effective_from/effective_until for event chronology; observed_at describes report chronology and attribution only.
- Thematic balance: While major utility disruptions (electricity, water, heating) naturally dominate during crises, maintain civic breadth by giving appropriate presence to everyday municipal and social operations (public transport, service desks, pension/administrative inquiries, connectivity, local markets, health access). Do not allow utility reports to erase all daytime civil life.
- DEVELOP storylines: Give prominent narrative weight and narrative continuity to primary DEVELOP storylines (e.g. collective citizen initiatives, delegations to municipal leadership, critical repair hubs). Ground them with timelines, stakeholder perspectives, and resident reactions.
- Synthesize related reports without listing every address merely to demonstrate coverage. Keep unrelated subjects separate and omit directory-style commercial payload.
- Develop the lead's central line in the body without repeating its premise in every section or restating it as a generic conclusion.
- Close on a supported scene, consequence, or unresolved issue when the material provides one; never predict what will happen next without an explicit source.


4. Microdetail Preservation:
- Do not collapse concrete evidence into generic summaries when useful supported specifics exist.
- When DETAIL SUPPORTS are provided, use their concrete anchors where they improve reader understanding: neighborhood, amount, interval, resident action, service name, timing, or a short exact quote.
- Prefer "residents pooled 300 units for a shared generator" over "residents are adapting" when the amount and action are supported.
- Prefer one or two strong specifics over a raw inventory of every source sentence.

5. Directory / Promotion Hygiene & Anti-Advertising:
- Do not print phone numbers, booking URLs, handles, or call-to-action copy in the long read.
- Do not turn a service-access Story into an advertisement or commercial directory.
- Avoid laundry lists: Never enumerate lists of multiple commercial bank names, exhaustive rosters of medical clinic specialties, or granular price catalogs copied from advertisements.
- Avoid booking announcements: Do not publish specific commercial carrier departure dates, booking schedules, or private transit ads.
- Editorial distillation: Transform commercial/service advertisements into concise journalistic facts about service availability, price levels, or resident reliance on intermediary services.
- Organization names, locations, prices, schedules, or addresses may appear only when the detail itself is editorially relevant, supported, and presented concisely without promotional tone.
- NO META-COMMENTARY OR OMISSION REPORTING: Never write meta-phrases explaining omitted contacts or instructions to the reader, such as "(контактные данные опущены)", "телефоны не указываются", "контакты скрыты", "даты не приводятся", or "как сообщалось ранее". Omit promotional payloads completely and silently without editorializing about their absence.

7. Narrative Composition Principles:
- Chronology: Build clear chronological narrative sequences when the supports establish temporal order.
- Contrast & Systemic Cascade: Highlight a causal or practical link only when the dossier directly supports that relationship. One eligible source may support it; do not require official confirmation or corroboration. Contrast lower and elevated areas, including the lower city and Gora, only when the dossier supports observations from both; describe differing local conditions as a localized contrast.
- Lived reality: Use concrete supported resident actions, practical adaptations, and coping strategies to show real community impact.
- Micro-locations: Weave street names and neighborhood references naturally into sentences instead of prefixing clauses with database-like labels such as "Location (Category): fact".
- Syntactic variety & anti-monotony:
  * Strictly avoid monotonous, formulaic sentence openings. Do NOT start consecutive sentences or paragraphs with repetitive phrases like «Жители сообщали...», «Один житель сообщил...», «Жительница рассказала...», «Горожане отмечают...».
  * Make the topic/subject the center of the sentence: open with the specific street, the municipal service, the infrastructure element, the time, or the concrete action (e.g. «На улице Димитрова авария насоса оставила без напора верхние этажи...», «В нагорной части города напряжение упало до критических значений...»).
  * Place natural attribution in the middle or at the end of the sentence (e.g. «..., по свидетельствам местных жителей, ...», «..., жалуются горожане»).
- Attribution discipline: Group repeated observations sharing the same epistemic status under a single natural attribution. Vary sentence openings and avoid mechanically repeating identical attribution phrases at the start of every sentence.
- Transitions: Neutral connective phrases (e.g. "meanwhile", "at the same time", "against this background") are permitted only when they connect verified observations without asserting unsupported causal links.
- Direct quotes & resident voice integration:
  * Quotation marks around spoken words mark an exact primary-source quote. Use only a complete phrase from ARTICLE_QUOTE_ALLOWLIST; never alter, translate, or assemble direct quotes. Quotes are optional, including when a chapter has a suitable allowlisted phrase.
  * Avoid quote inventories and chat rolls. QUOTE_ROLL_PARAGRAPH is a nonblocking review heuristic when a paragraph has more than two direct-speech spans; supported organization, shop, and place-name styling is excluded from that count. CONSECUTIVE_DIRECT_SPEECH_ROLL is a separate structural blocker for two or more direct-speech spans joined only by list punctuation with no prose between them.
  * When no quote adds value or no exact allowlisted phrase fits, use indirect speech. Ordinary terms should not be put in quotes for emphasis; evidence-supported proper names may use typographic quotes while retaining their spelling.
  * Smooth narrative attribution lead-ins: When using an allowlisted quote, smoothly introduce it (e.g. `«...», — делятся горожане`, or `Один из жителей на улице Димитрова отметил: «...»`).
  * Synthesis over enumeration: When multiple residents report the same condition across different locations or times (e.g. utility outages, pervasive odors, connectivity checks), synthesize the shared facts into coherent prose using indirect speech and geographical progression instead of quoting each resident.
  * Indirect speech as default: Use natural indirect speech to summarize repetitive complaints, status checks, or similar observations without quotation marks.

- Proper names & Places:
  * Do NOT introduce external city names, persons, or organizations that are not explicitly mentioned in that paragraph's cited support.
- Temporal role constraints:
  * Keep the title and lead grounded in current-window evidence; do not put support IDs in the text.
  * Do not present `HISTORICAL_CONTEXT` as current breaking news.
  * Frame `FUTURE_SCHEDULED` events as upcoming or planned.

- Preserve source date granularity: If a support says only a bare day number like "31" or "31-го", do not expand it and do not infer a missing month or year (e.g. do not expand to "31 августа" or add a year) unless that month/year is explicitly present in the cited support. Prefer the source's own granularity over inferred precision.
- Proportion & length: Do not pad a thin day to reach an arbitrary length. State supported facts concisely without fluff. On rich days, develop major storylines thoroughly across sections without repeating facts.{target_str}
- Logical clarity and natural precision:
  * Distinguish technical infrastructure from human actions cleanly (e.g. do not produce awkward compression like «делятся интернетом через оптоволокно» — write naturally: «подключают оптоволокно (GPON) и делятся Wi-Fi с соседями» or «раздают интернет по Wi-Fi»). Keep technical mechanisms and social actions logically accurate.
  * Brand and service naming: Enclose a source-supported commercial name in Russian typographic quotation marks and use an explanatory noun where needed (e.g. провайдер «+7Телеком», маркетплейс «Озон»). Do not leave a brand as an unexplained word or digit string. Do not expand a shorthand to a longer brand unless the cited source supports the full name.
  * Relocation services and external geography: When describing assistance centers, administrative services, or cultural events outside the edition city, state the host city before the street address; never cite an external street without its host city name.
- Final language pass: Before returning the article, proofread Russian agreement, case government, sentence structure, punctuation, typographic quotation marks for supported names, and the connection between each place and its district. Correct awkward or ungrammatical wording without changing the facts, source attribution, uncertainty, dates, numbers, or geographic precision.
- Strict boundaries: No metaphors, sensationalism, clickbait, emotional exaggerations, invented mechanisms, or speculative interpretations.


8. Epistemic Fidelity:
- Single-source, community, resident, eyewitness, and explicitly unverified reports are authorized publication material when supplied as PUBLISH support; lack of corroboration is not a reason to omit them.
- Preserve the support's epistemic status. For framing=attributed_report, write natural attribution such as "residents report", "according to a participant", or the output-language equivalent.
- Do not upgrade community or attributed material to "officially confirmed", "established", or equivalent wording unless a cited support itself establishes that status.
- Resident questions (framing=question_context or publication_use=CONTEXT):
  * A resident question is background context, NOT an established fact or an answered status.
  * If you mention a resident question, frame it strictly as an inquiry or uncertainty (e.g. "жители интересуются...", "поступают вопросы о..."), NEVER as an established fact (e.g. do not state "фонд закрыт" or "нотариус работает" unless a PUBLISH support separately states that fact).
  * Do not assert trends such as "участились вопросы" or "повышенный интерес" from a single question.
- Corroboration may strengthen wording or grouping, but never require two sources merely to publish a legitimate local report.
"""


DIGEST_ITEM_COMPOSITION_GUIDE = """Digest item composition:
- Plan the reader-facing subjects within each rubric before writing. Related reports
  normally share an item; source, Story and street boundaries are not paragraph boundaries.
- A developed item may have a short informative headline naming the actual development
  (for example, an interruption or a changed payment rule), not a generic service label.
  Prefer a few words; avoid source-process headlines such as 'Reports of ...' and long
  sentence headlines that the body immediately repeats.
  The body adds concrete details rather than restating the headline. A small standalone
  update can be one complete sentence without a headline. Do not force a headline quota.
- Develop one subject within each item: lead with the supported update, then arrange
  concrete details by their actual relation, such as location, duration, contrast or
  practical consequence. Every sentence should develop that subject, not restart a
  source-by-source account. Keep distinct source roles and uncertainty in scope.
- Before returning, compare all items in each rubric. Combine overlapping subjects
  such as 'Electricity' and 'Electricity in the city' when they fit one readable passage.
  Separate genuinely different developments with informative labels; never compress
  the entire service into a long address roster or discard a distinct fact.
  When an electricity theme needs several items, make their distinction recognizable
  (for example, outage durations versus intermittent supply/voltage versus coping).
  Several headings that merely rephrase 'outages in the city/districts' do not provide
  that distinction. Use these subjects only when the supplied facts support them.
- Weave a general report about two interrupted services into the relevant developed
  passage instead of adding a separate overview item that repeats both service themes.
  Concrete coping details may share a passage with the reported disruption when that
  makes reading clearer; a price mentioned in that report need not become its own item.
  This grouping establishes no causal link or common time/location beyond the evidence.
- Chat irony, jokes and wishes are not service reports: 'теперь и газ осталось
  подрубить' means gas has not been cut yet. Report only the plainly stated parts
  of such a message. Do not mention the joked-about service from it at all: no
  state, no 'шутливое замечание', no explanation.
- Connections must be supported. A price is not evidence of a price increase. Do not
  invent causality, chronology, geographic proximity or shared witnesses to smooth prose.
  Preserve a source's partial/poor service separately from a complete outage; a coarse
  status label or extracted summary does not authorize stronger wording than its source.
  Regrouping preserves every required fact, its ownership, details and attribution.
- A support's reporting_window_role describes when the source was observed, not when
  an event occurred. historical_source must remain a past attributed report; its
  'сейчас/сегодня' and stored service state do not establish today's availability.
  These role names are internal metadata: never print them or calques such as
  'историческое сообщение'. Mark the past report naturally, e.g. 'ранее житель
  сообщал, что…', in the past tense, without inventing a date.
  current_window_source also does not prove persistence until publication. Preserve
  explicit source times and scheduled dates; never invent an event date from provenance.
  unknown means temporal scope is unresolved. Retain selected material with honest
  framing rather than dropping it or presenting an old observation as a fresh update.
"""


def build_digest_narrative_contract(*, output_language: str = "Russian") -> str:
    """Give the writer editorial freedom inside the frozen evidence boundary."""
    return f"""### Local-news digest (Output Language: {output_language})

Write a concise, connected newsroom digest for a resident reading on a phone. Tell the
reader what is happening, where, for how long, and what is useful to know. Python owns
eligibility and provenance; you own the wording, hierarchy and synthesis.

Editorial craft:
- Use the assigned thematic blocks, with no separate dashboard or city-situation panel.
- Start with the most consequential supported development. Each item has a clear subject;
  related reports form a coherent passage, not a transcript or one bullet per Story/street.
- For a busy electricity theme, aim for 1–3 connected items organized by supported places
  or chronology. Item counts and lengths are editorial targets, not reasons to drop facts
  or add filler. Other services need their own clear place in the rubric.
- Use an informative short headline for a developed item when it helps scanning, or
  one complete natural paragraph without a headline for a small update. Lead with the supported news
  or place, attach honest attribution naturally in scope, and develop the subject without
  restating a thesis headline.
  An isolated observation with no extra detail can be one complete attributed sentence.
- Keep every item body within the 1,200-character hard limit. If a useful synthesis is longer,
  split it into a few coherent passages by service or supported locality; never truncate or
  drop facts to meet the limit.
- Write about the city, not the message stream. Avoid 'в одном из сообщений', 'другое
  сообщение описывает', 'опубликовано объявление о' and question-and-answer narration.
  Establish community attribution for the connected observations and keep it in scope;
  repeat it when the source or certainty changes, not mechanically before every street.
- Preserve distinct durations, times, prices, locations and resident workarounds. Shared
  facts need stating once, with all corresponding IDs. Compression removes repetition,
  not useful concrete detail. Avoid generic commentary such as 'горожане адаптируются'.
- State the practical update directly: the named service, supplied access conditions,
  destinations, dates or actions. An advertised route is a stated offer, not proof that
  buses actually operate: use offer/announcement wording without a generic verification
  disclaimer. Never invent instructions or a missing month/institution type.
  Useful partial information remains publishable with honest limits.
- When availability reports differ, describe the supported local contrast or preserve
  the uncertainty. If neither chronology nor sub-location is supplied, say residents
  report different availability in that area; do not invent a later restoration or
  separate streets to resolve the discrepancy. Keep brief restorations distinct from
  current status. Do not claim city-wide conditions from an unspecified household.

{DIGEST_ITEM_COMPOSITION_GUIDE}

Frozen membership:
- Rubrics and blocks are fixed. With composition_units, each item names exact unit IDs
  from its block. Compatible units may be woven together; one unit may be split.
- Represent every selected Story, each material fact exactly once across its block, and
  every summary-only unit exactly once. One item can cover many facts and Stories.
  Python derives Story/support ownership; never guess it from a name or topic.
- Without composition_units, follow the supplied compatibility membership contract.
- Claim Atoms are short validation metadata, not sentence templates. Reader-facing
  prose may be fluent and journalistic while faithfully representing the cited facts.

Evidence boundary:
- One eligible PUBLISH community report is sufficient. Attribute it honestly; lack of
  corroboration or official confirmation does not disqualify useful local reporting.
- Ground every place, number, price, time, duration, state and cause in the supplied
  citable text. Snapshot/observed_at dates are reporting metadata, not event dates.
- Shared rubric/topic is not evidence of geographic proximity, shared chronology or
  causality. Keep locations attached to their own observations. State a mechanism only
  when explicitly supported; preserve a resident's explanation as their explanation.
- Keep fares attached to the paid trip leg, not the final destination of a passing bus.
- Quotes retain exact source wording; use indirect speech for compression/correction.
  Supported organization names may use typographic quotes without becoming direct speech.
- resident_question, question_context and CONTEXT supports are background, not established
  facts or service states. Where a factual answer exists, report the answer; a question
  alone must not become meta-news ('жители интересуются') or an invented answer.
- {DIGEST_REPLY_CONTEXT_GUIDE}
- No speculation, sensationalism, invented connective facts or decorative filler.
"""
