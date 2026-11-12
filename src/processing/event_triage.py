"""Batch Gate V2 triage and brief generation service for story clusters."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping

import psycopg

from src.ai_providers import classify_provider_failure
from src.config_loader import EditionScopeConfig
from src.domain.edition_geography import EditionGeographyContext
from src.domain.event_clusters import StoryClusterState
from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    ensure_keep_publishability,
    normalize_question_evidence,
    parse_event_payload,
)
from src.domain.service_state import ServiceStatePayload
from src.llm_telemetry import llm_call_context
from src.processing.conversation import (
    FIGURATIVE_REMARK_RULE,
    SAME_INCIDENT_RELATION,
    ConversationTimeline,
    ConversationTimelineSettings,
    TimelineAnchor,
    clear_context_only_service_locations,
    context_only_details,
    load_conversation_timeline,
    withhold_context_only_evidence,
)
from src.processing.edition_scope import (
    SCOPE_VERSION,
    EditionScopeClass,
    FragmentMessageContext,
    broad_region_without_focus_impact,
    build_scope_contract,
    external_city_without_focus_impact,
    external_relocated_idp_event,
    message_context_names_place,
    story_placed_outside_by_message,
)
from src.processing.fire_reading import (
    LIT_WINDOWS_VS_FIRE_RULES,
    unsupported_fire_reading,
)
from src.processing.gate_fidelity_rules import GATE_ORDINAL_AND_HEDGE_RULES
from src.processing.hard_exclusion import evaluate_story_hard_exclusion
from src.processing.operational_semantics import (
    has_unstructured_publish_service_access,
    normalize_service_state_evidence,
)
from src.repositories.event_clusters import EventClusterRepository

logger = logging.getLogger(__name__)

TRIAGE_VERSION = "v20"

# An explicit claim that the supplied input is absent is a response failure,
# not a geographic or editorial judgment. Keep this narrow: uncertainty about
# locality or evidence is still a legitimate classification.
_MISSING_GATE_INPUT_RE = re.compile(
    r"^\s*(?:нет\s+текста\s+(?:фрагмента|сообщения|источника)|"
    r"no\s+(?:story\s+(?:content|text)|source\s+text)\s+(?:was\s+)?provided)\b",
    re.IGNORECASE,
)

_SERVICE_KEYWORDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("вод", "водопостач", "водоснабж"), "water_supply", "Водоснабжение"),
    (("свет", "электр", "електр", "струм", "питани"), "power_supply", "Электроснабжение"),
    (("газ",), "gas_supply", "Газоснабжение"),
    (("отоплен", "опален", "тепло"), "heating", "Отопление"),
    (("интернет", "інтернет", "связь", "зв'язок"), "connectivity", "Связь"),
)

_OUTAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:нет|нету|немає|нема|відсутн[яєій]|отсутствует|не\s+работает|не\s+працює)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:\d+[-—]?й?\s+день\s+без|без\s+(?:света|води|воды|газа|тепла))\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:порыв|порив|авария|аварія|отключ[еи]н)\b", re.IGNORECASE),
)


def _extract_mixed_question_and_outage(text: str) -> tuple[str, str, str, str] | None:
    """Detect if text contains both a question and a grounded factual outage clause."""
    if "?" not in text:
        return None
    # A question quoted inside a reported exchange («На вопрос «…?» дан ответ
    # «…»») is not a separate clause; splitting it leaves «» дан ответ…».
    depth = 0
    for index, char in enumerate(text):
        if char in "«“":
            depth += 1
        elif char in "»”":
            depth = max(depth - 1, 0)
        elif char == "?" and (depth or re.match(r"\s*[»”\"']", text[index + 1 :])):
            return None

    clauses = re.split(r"([?])", text)
    if len(clauses) < 3:
        return None

    q_parts: list[str] = []
    statement_parts: list[str] = []

    i = 0
    while i < len(clauses):
        part = clauses[i].strip()
        if i + 1 < len(clauses) and clauses[i + 1] == "?":
            q_text = f"{part}?".strip()
            if q_text and q_text != "?":
                q_parts.append(q_text)
            i += 2
        else:
            if part and part != "?":
                statement_parts.append(part)
            i += 1

    if not q_parts or not statement_parts:
        return None

    statement_combined = " ".join(statement_parts).strip()
    has_outage = any(p.search(statement_combined) for p in _OUTAGE_PATTERNS)
    if not has_outage:
        return None

    full_lower = text.lower()
    subject_key = None
    subject_label = None
    for kw_tuple, s_key, s_label in _SERVICE_KEYWORDS:
        if any(kw in full_lower for kw in kw_tuple):
            subject_key = s_key
            subject_label = s_label
            break

    if not subject_key or not subject_label:
        return None

    q_combined = " ".join(q_parts).strip()
    return q_combined, statement_combined, subject_key, subject_label


SAME_MESSAGE_CONTEXT_CHARS = 240


def format_gate_fragment_excerpt(
    fragment: Mapping[str, Any],
    parent_text: str | None = None,
    same_message_text: str | None = None,
) -> str:
    """Render a citable fragment separately from its non-citable reply and message context."""
    fragment_id = fragment["fragment_id"]
    observed_at = fragment["observed_at"]
    timestamp = observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at)
    own_text = " ".join(str(fragment.get("text") or "").split())
    line = (
        f"- [frag={fragment_id} time={timestamp} role={fragment['source_role']} "
        f"source={fragment['source_name']}] PRIMARY SOURCE TEXT: {own_text}"
    )
    if same_message_text and same_message_text.strip():
        clean_message = " ".join(same_message_text.split())
        line += f"\n  [SAME-MESSAGE CONTEXT ONLY — NOT CITABLE]: {clean_message}"
    if parent_text and parent_text.strip():
        clean_parent = " ".join(parent_text.split())
        line += f"\n  [REPLY-PARENT CONTEXT ONLY — NOT CITABLE]: {clean_parent}"
    return line


def _fragment_message_context(
    fragment: Mapping[str, Any],
    same_message_context: Mapping[int, FragmentMessageContext],
) -> FragmentMessageContext:
    """Pair a story fragment's full text with the other paragraphs of its message."""
    text = str(fragment.get("full_text") or fragment.get("text") or "")
    context = same_message_context.get(int(fragment["fragment_id"]))
    if context is None:
        return FragmentMessageContext(text=text)
    return replace(context, text=text)


def same_message_geography_excerpt(
    context: FragmentMessageContext,
    *,
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
    limit: int = SAME_MESSAGE_CONTEXT_CHARS,
) -> str | None:
    """Return the message's other paragraphs that name a place, earliest first.

    Only paragraphs that name a local anchor or a known external place help Gate
    locate a split paragraph, so place-free paragraphs are left out to save input.
    """
    texts = [
        " ".join(text.split())
        for text in (*context.earlier_message_texts, *context.later_message_texts)
        if message_context_names_place([text], scope=scope, geo_context=geo_context)
    ]
    if not texts:
        return None
    joined = " … ".join(texts)
    if len(joined) <= limit:
        return joined
    return joined[: limit - 1].rstrip() + "…"


def decompose_mixed_outage_evidence(
    payload: EventPayload,
    fragment_texts: Mapping[int, str] | None = None,
) -> EventPayload:
    """Deterministic decomposition of mixed question + factual outage evidence."""
    new_evidence: list[EvidenceItemPayload] = []
    mutated = False
    represented_fragments = {
        fid
        for item in payload.evidence_items
        if item.kind != "resident_question" and item.publication_use == "PUBLISH"
        for fid in item.source_fragment_ids
    }

    for item in payload.evidence_items:
        if item.publication_use == "EXCLUDE":
            new_evidence.append(item)
            continue
        candidates_to_check = [item.text]
        # Atomic claims already extracted from a mixed source stay atomic.
        # Recover a missed statement only from a question-only extraction,
        # under its single owning fragment; never replace every claim with
        # the entire source or turn coping behavior into a service state.
        if (
            fragment_texts
            and item.kind == "resident_question"
            and len(item.source_fragment_ids) == 1
            and not represented_fragments.intersection(item.source_fragment_ids)
        ):
            for fid in item.source_fragment_ids:
                if fid in fragment_texts:
                    candidates_to_check.append(fragment_texts[fid])

        extracted = None
        for cand in candidates_to_check:
            extracted = _extract_mixed_question_and_outage(cand)
            if extracted:
                break

        if extracted and item.kind in ("community_report", "resident_question", "service_access"):
            q_clause, outage_clause, sub_key, sub_label = extracted
            new_evidence.append(
                EvidenceItemPayload(
                    text=q_clause,
                    kind="resident_question",
                    publication_use="CONTEXT",
                    source_fragment_ids=item.source_fragment_ids,
                )
            )
            new_evidence.append(
                EvidenceItemPayload(
                    text=outage_clause,
                    kind="service_access",
                    publication_use="PUBLISH",
                    source_fragment_ids=item.source_fragment_ids,
                    service_state=ServiceStatePayload(
                        subject_key=sub_key,
                        subject_label=sub_label,
                        dimension="availability",
                        state="UNAVAILABLE",
                        location="",
                        expected_now=True,
                        basis="direct_failure",
                    ),
                )
            )
            mutated = True
            represented_fragments.update(item.source_fragment_ids)
        else:
            new_evidence.append(item)

    if not mutated:
        return payload

    return replace(payload, evidence_items=tuple(new_evidence))


_ALLOWED_EXCLUSION_REASONS = frozenset(
    {
        "commercial_classified",
        "private_classified",
        "directory_payload",
        "obvious_noise",
    }
)

_GATE_V2_SYSTEM_PROMPT = (
    """You are a fast geographic, editorial retention, and operational triage classifier for a regional newsroom digest.
You are evaluating candidate event Stories for ONE configured edition.
For every Story return three judgments in the same result:
1. geographic scope: exactly LOCAL, DIRECT_IMPACT, OUT_OF_SCOPE, or UNCERTAIN;
2. retention: KEEP or DROP;
3. enrichment: NONE, BRIEF, or ANALYZE.

Scope is evaluated first. Source membership alone does not make an event local.
An event in another city/region is OUT_OF_SCOPE unless the excerpts explicitly state a concrete consequence inside the configured focus area.
Same-region, national importance, front-line direction names, and broad strategic relevance are not DIRECT_IMPACT by themselves.
Mentions of places, districts, neighborhoods, or streets listed in the configured Focus Places or GEOGRAPHIC CONTEXT establish LOCAL scope, unless the same message explicitly places them in another city or region. District and neighborhood names repeat across cities.
OUT_OF_SCOPE or UNCERTAIN is normalized to DROP+NONE without requiring a brief.

SAME-MESSAGE CONTEXT:
- A long source message is split into several fragments. A fragment may be followed by [SAME-MESSAGE CONTEXT ONLY — NOT CITABLE]: other paragraphs of its own message that name a place.
- Use that context only to decide where the fragment's event takes place. When it explicitly places the message in a city or region outside the focus places and the message names no focus settlement, the fragment's unlocated statements and its district, neighborhood, or street names refer to that external place: classify OUT_OF_SCOPE unless the fragment's own text states a concrete consequence inside the focus places.
- When that context places the message in a focus place and the fragment names no other place, the fragment shares that placement; cite the fragment itself as the scope basis.
- Never cite same-message context in scope_basis_fragment_ids or source_fragment_ids, and never copy its facts into evidence.

EDITION-LOCAL DEICTIC CITY REFERENCES:
- If a PRIMARY SOURCE TEXT itself contains an explicit generic city-level locator such as "в городе", "по городу", "по всему городу", or "во всём городе", and that fragment's source metadata name exactly matches the configured Target Edition name, resolve the generic city reference to that edition and allow LOCAL scope when no competing city/place is mentioned. Cite that primary fragment as the scope basis.
- This rule requires both the source's own city-level wording and the exact edition-name match. Source membership or source name alone does not establish locality.
- This establishes only city-level scope. Do not infer a district, street, neighborhood, or more specific service area.
- A source name alone, or a primary fragment saying only "у меня", "да", or an unlocated pronoun, does not establish locality under this rule.

For LOCAL or DIRECT_IMPACT content:
- Lack of corroboration, a single community source, or lack of official confirmation is NOT by itself a reason to DROP an otherwise legitimate LOCAL or DIRECT_IMPACT report.
- Represent source uncertainty through evidence kind, wording, and confidence; do not erase the event.
- For retention=KEEP, brief_payload.publishability must be "news" or "brief". Do not use "internal_only" or "noise" merely because evidence is community, conversational, single-source, or unverified.
- DROP is only for high-confidence hard noise/commercial-only content and must use enrichment=NONE with exclusion_reason in ('commercial_classified', 'private_classified', 'directory_payload', 'obvious_noise').
- The request supplies the minimum confidence for a hard DROP. Never inflate your confidence to meet it. If a DROP is less certain, provide a faithful brief so normalization can retain useful material. Pure background, nostalgia without a new development, or context-only conversation belongs in CONTEXT evidence, never invented PUBLISH evidence.
- In-scope KEEP uses BRIEF for simple useful local information, and ANALYZE only when rich synthesis is justified.
- Publication use is semantic, not topic-based.
- Evidence kind describes semantic content, not source trust.
- Use service_access for a concrete current or scheduled resident-facing service availability/access state even when reported by a community source.
- Use community_report for useful community facts that are not themselves service availability/access states.
- A sales offer, discount, product listing, seller phone number, or promotional price is EXCLUDE.
- A resident describing the price they paid or the cost of coping with an outage is not a sales offer. Preserve the supported amount as community_report when it explains local lived experience; one price or price range does not establish a price increase.
- Do not convert EXCLUDE commercial details into useful_details merely to preserve them.
- Resident questions, resident answers, service availability, outage reports, and operational workarounds are not noise merely because they are conversational. Preserve current local actionable information about everyday civilian access to services.
- Use resident_question for a resident asking whether/where/when/how something works when the excerpt itself does not provide the answer.
- resident_question is CONTEXT, not PUBLISH.
- A question alone MUST NOT create a service state.
- If another fragment answers the question, represent the answer separately as service_access/community_report/official_statement as appropriate.
- Do not infer trends such as "повышенный спрос" or "участились вопросы" from one question.
- A reply or remark that only generalizes or opines (for example "мало у кого остались рабочие печи", "многие давно забыли", "всем всё равно") without its own place, time, event, number or service state is conversational CONTEXT, not a PUBLISH community_report or a new Story. Short reports with a place and a state ("На Димитрова с водой печально") stay PUBLISH.
"""
    + FIGURATIVE_REMARK_RULE
    + """- A reply that only locates a street/building or explains a neighborhood's name is CONTEXT, not a new event. Preserve that location when it anchors a separately supported development or concrete service/access detail. Do not manufacture a digest item from a location clarification or an unanchored discussion about an institution.
- A private location answer stays context even when it names street numbering or nearby shops/landmarks. For example, explaining which residential "line" a relative's home belongs to, in answer to where that home is, is not a city update; the parent's concern about whether the relative has power does not establish a power state. Keep it publishable only if the reply itself also reports a concrete current service state/change or public access detail.
- A bare personal ranking such as "MTS is the best provider" is opinion/context, not evidence that the service is available, superior, or broadly useful. Keep a service recommendation only when the message adds concrete first-hand access details, prices, coverage, or a current change; do not create sales copy from an endorsement.
- A greeting, wish, or vague city mood such as "город спит, и пусть так будет всегда" is not a report of a safety condition or city event. A casual historical anecdote/correction about the city's origin is context unless it is tied to a current public event, anniversary, or concrete present-day consequence.

CONTEXT VS EVIDENCE & REPLY INHERITANCE:
- source_fragment_ids must identify the exact source fragments whose own text directly supports the material claims in that evidence item. Never cite a reply fragment for a separate claim stated only by its parent.
- Reply-parent text is supplied separately as unnumbered context, not as part of the reply's source text and not as citable support. It may clarify the referent, service, or local place in a clearly dependent reply (for example, "Да, минут десять назад").
- A direct short answer may support the answer it gives. It does not support separate status, duration, cause, or other details stated only by the parent. If a parent-only fact has its own separately listed source fragment, cite that fragment; otherwise do not turn the parent-only fact into a PUBLISH claim for the reply.
- Chronologically adjacent messages without an explicit reply link are CONTEXT_ONLY for interpreting tone and flow, and must NEVER serve as factual grounding for a claim. The only thing they may settle is which service a remark leaves unnamed, under the CHAT TIMELINE rule below.
- An unanchored conversational remark with no local place mentions and no explicit reply-parent anchor is UNCERTAIN scope (normalizes to DROP), unless the CHAT TIMELINE rule below establishes where it happens.

CHAT TIMELINE:
- The request may include one CHAT TIMELINE block: the messages around the judged Stories, oldest first, labelled C1, C2 and so on. "↩ C5" marks a reply to line C5. "[Story #N]" marks a line that belongs to that judged Story. A "--- pause ---" line means the chat fell silent there, so lines on its two sides are separate conversations.
- Residents write short remarks that only make sense in sequence. Read a short remark against the lines right before and after it to learn what it refers to. Many neighbouring lines are unrelated announcements or arguments; ignore those.
- A timeline line is background, never evidence. Never cite it, and never copy its place, number, time, cause, or event into the evidence_items, headline, or digest_summary of a Story that does not state that detail itself. "Двери чуть не вынесло" next to a line about blasts in a named district stays a report about the doors; it does not become a report about that district or about a blast.
- Scope: a remark with no place of its own may be LOCAL when the lines right around it show residents discussing a concrete event at a focus place at that moment and the remark plainly continues that discussion. Cite the remark's own fragment as the scope basis. A local place appearing somewhere in the timeline is not enough.
- Retention: a first-hand observation of an event the timeline shows to be under way (what was seen or heard, how many times, when it stopped) is useful local reporting even when short or emotional. A reaction that adds nothing observable stays CONTEXT or DROP.
- Unnamed service: when a remark states availability without naming the service ("Колония нету", "на Гайдара нет") and its reply chain or the lines right before it talk about exactly one service, the evidence may name that service, attributed to the resident. If several services are being discussed, keep the remark's own words and name none.
- Questions and answers: a Story whose own lines only ask stays a question. An answer on another timeline line belongs to that line's Story, so never put it into this Story or quote it here.
- same_incident_context_ids: the labels of timeline lines that report one concrete event at one time (a blast, a fire, an accident, a burst main) and reports of that event's effects. Lines of other judged Stories count. Use [] for ordinary availability talk: questions and answers about whether there is power or water, complaints, jokes and reactions, even within one conversation about one outage. Lines from a different place or across a "--- pause ---" are a different incident unless a line says they share one failure. Never list a line that belongs to this Story.

EVIDENCE SOURCE OWNERSHIP:
- Every factual clause in an evidence item's text must be supported by its cited source_fragment_ids. Cite multiple IDs only when their own source texts support the corresponding parts of the item.
- Do not copy an independent parent statement into a reply's evidence text. A reply may inherit only the missing referent/location needed to understand its own direct answer; it may not inherit the parent's event, status, duration, number, cause, or chronology.
- When only the unnumbered parent context contains a claim, keep that claim out of the reply's PUBLISH evidence. Preserve other directly supported useful content in the Story.
- Do not mark a useful direct reply CONTEXT solely because it uses a pronoun when the explicit parent makes the referent unambiguous. If the reply's own text gives a concrete local place and current status or duration (for example, parent: "no lights in the center for three days"; reply: "at the Third Beach it has been absent for three months"), retain it as KEEP/BRIEF and PUBLISH as a community_report. Resolve only the pronoun naturally from the parent, cite only the reply fragment, and preserve only the reply's own place, status, and duration. Do not copy the parent's place, status, duration, number, cause, or chronology. Use CONTEXT when the referent is genuinely ambiguous or the reply has no publishable detail of its own.
- For this concrete exchange — parent: "В центре в другой половине, света уже нет 3 суток"; reply: "На 3 пляже его уже нету третий месяц" — output a PUBLISH community_report such as "По сообщению жителя, на 3-м пляже света нет уже третий месяц." The parent supplies only the referent "свет"; the reply supplies the place, current absence, and duration. Do not call the object unknown or use CONTEXT solely because the child wrote "его".
- Preserve polarity from the primary reply. Parent: "На восьмухе свет не появился?"; reply: "По Павлова вроде как был так и остался и ещё где то появился" reports continued availability on Pavlova, with the author's uncertainty. It does not report a continuing outage. The question's negative wording is not the reply's status.
- Preserve the role of a duration, not just its number. "Слободка 1 раз кажется тоже со светом побыла 3 дня" means an episode of availability lasting three days, reported tentatively. It does not mean one switch-on within a three-day observation period. Do not infer present availability from this past episode.
- An interval-only reply such as "На Тверской один раз в неделю на сутки" may answer a unique explicit question about how often power is supplied or interrupted. Keep the question's action and the reply's own interval together. If the parent instead mixes a house with power and a house without power, the reply does not resolve which action the interval describes: preserve it as CONTEXT, without inventing supply or outage polarity.

"""
    + LIT_WINDOWS_VS_FIRE_RULES
    + GATE_ORDINAL_AND_HEDGE_RULES
    + """
CHAT SARCASM, RUMORS, AND SCHEDULED OUTAGES:
- Informal chat banter, sarcasm, emotional reactions, and ungrounded rumors/predictions about future outages (e.g. "после 20-го всё вырубят", "зимой тепла не будет", "завтра опять отключат") MUST NEVER be classified as service_access or SCHEDULED.
- SCHEDULED with basis=scheduled_change requires an authoritative announced schedule, planned maintenance notification, or official utility/municipal notice (e.g. РЭС, Водоканал, Горгаз, Горсвет, администрация).
- If an informal chat message expresses personal speculation, jokes, or fears about future outages without reporting a current factual failure or an official schedule, classify it as DROP with exclusion_reason="obvious_noise".

SERVICE-STATE CONTRACT:
- Operational service truth exists only inside a PUBLISH evidence item with kind=service_access and a non-null service_state object.
- Do not output an operational_observations array.
- service_state describes the external resident-facing service outcome, not the resident workaround mechanism.
- A generator, battery, private well, neighbor collection, charging action, VPN choice, fuel burden, or other coping action is not itself a resident-facing service state.
- If a workaround causes an explicitly stated service outcome, keep the coping action as separate evidence and attach service_state only to the evidence sentence that states the water/internet/banking/transport/etc. outcome.
- For UNAVAILABLE, DEGRADED, or RESTRICTED, expected_now MUST be true and the excerpts must establish that the service is expected to operate now or explicitly describe a current failure/restriction.
- Do not infer expected_now from the calendar or general season knowledge.
- For SCHEDULED, basis must be scheduled_change, effective_from is required, and excerpts must cite an authoritative schedule, planned work, or utility announcement. NEVER assign SCHEDULED to chat gossip, jokes, or rumors.
- Valid basis values: normal_operation, direct_failure, degraded_access, explicit_restriction, scheduled_change.
- LOCAL or DIRECT_IMPACT MUST cite one or more exact scope_basis_fragment_ids from the Story excerpts that establish the local occurrence or concrete local consequence.

Respond ONLY with a valid JSON object containing a "results" array:
{
  "results": [
    {
      "story_id": 123,
      "scope": "LOCAL | DIRECT_IMPACT | OUT_OF_SCOPE | UNCERTAIN",
      "scope_basis_fragment_ids": [101],
      "scope_confidence": 0.98,
      "scope_reason": "Brief geographic explanation",
      "retention": "KEEP | DROP",
      "enrichment": "NONE | BRIEF | ANALYZE",
      "exclusion_reason": "commercial_classified | private_classified | directory_payload | obvious_noise | null",
      "confidence": 0.98,
      "reason": "Brief explanation",
      "same_incident_context_ids": ["C3"],
      "brief_payload": {
        "enrichment_level": "brief",
        "topic": "Concise topic label",
        "tags": ["3-8 short open tags in Russian"],
        "urgency": "critical | high | normal | low",
        "publishability": "news | brief | internal_only | noise",
        "headline": "Informative headline",
        "digest_summary": "1-2 concise sentences",
        "evidence_items": [
          {
            "text": "Fact or service access detail",
            "kind": "established_fact | community_report | service_access | official_statement | commercial_offer | resident_question",
            "publication_use": "PUBLISH | CONTEXT | EXCLUDE",
            "source_fragment_ids": [101],
            "service_state": {
              "subject_key": "water_supply",
              "subject_label": "Водоснабжение",
              "dimension": "availability",
              "state": "UNAVAILABLE",
              "location": "",
              "entity": "",
              "expected_now": true,
              "basis": "direct_failure",
              "effective_from": null,
              "effective_until": null
            }
          }
        ]
      }
    }
  ]
}
"""
)


def build_gate_response_format(story_count: int) -> dict[str, Any]:
    """Build the strict structured-output contract for one Gate V2 batch.

    ``json_object`` only asks the provider for syntactically valid JSON. Gate
    needs a stronger contract: one result per requested story and a stable
    nested brief shape. Semantic provenance checks still run after parsing.
    """
    if story_count < 1:
        raise ValueError("story_count must be positive")

    service_state_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject_key": {"type": "string"},
            "subject_label": {"type": "string"},
            "dimension": {"type": "string"},
            "state": {
                "type": "string",
                "enum": [
                    "AVAILABLE",
                    "UNAVAILABLE",
                    "DEGRADED",
                    "RESTRICTED",
                    "UNKNOWN",
                    "SCHEDULED",
                ],
            },
            "location": {"type": "string"},
            "entity": {"type": "string"},
            "expected_now": {"type": "boolean"},
            "basis": {
                "type": "string",
                "enum": [
                    "normal_operation",
                    "direct_failure",
                    "degraded_access",
                    "explicit_restriction",
                    "scheduled_change",
                ],
            },
            "effective_from": {"type": ["string", "null"]},
            "effective_until": {"type": ["string", "null"]},
        },
        "required": [
            "subject_key",
            "subject_label",
            "dimension",
            "state",
            "location",
            "entity",
            "expected_now",
            "basis",
            "effective_from",
            "effective_until",
        ],
    }

    evidence_item_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": [
                    "established_fact",
                    "community_report",
                    "service_access",
                    "official_statement",
                    "commercial_offer",
                    "resident_question",
                ],
            },
            "publication_use": {"type": "string", "enum": ["PUBLISH", "CONTEXT", "EXCLUDE"]},
            "source_fragment_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "service_state": {
                "type": ["object", "null"],
                "properties": service_state_schema["properties"],
                "required": service_state_schema["required"],
                "additionalProperties": False,
            },
        },
        "required": ["text", "kind", "publication_use", "source_fragment_ids", "service_state"],
    }

    brief_payload_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "enrichment_level": {"type": "string", "enum": ["brief", "analysis"]},
            "topic": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "urgency": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
            "publishability": {
                "type": "string",
                "enum": ["news", "brief", "internal_only", "noise"],
            },
            "headline": {"type": "string"},
            "digest_summary": {"type": "string"},
            "evidence_items": {
                "type": "array",
                "items": evidence_item_schema,
                "minItems": 1,
            },
        },
        "required": [
            "enrichment_level",
            "topic",
            "tags",
            "urgency",
            "publishability",
            "headline",
            "digest_summary",
            "evidence_items",
        ],
    }

    result_item_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "story_id": {"type": "integer"},
            "scope": {
                "type": "string",
                "enum": ["LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"],
            },
            "scope_basis_fragment_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "scope_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "scope_reason": {"type": "string"},
            "retention": {"type": "string", "enum": ["KEEP", "DROP"]},
            "enrichment": {"type": "string", "enum": ["NONE", "BRIEF", "ANALYZE"]},
            "exclusion_reason": {
                "type": ["string", "null"],
                "enum": [
                    "commercial_classified",
                    "private_classified",
                    "directory_payload",
                    "obvious_noise",
                    None,
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
            "same_incident_context_ids": {"type": "array", "items": {"type": "string"}},
            "brief_payload": {"anyOf": [brief_payload_schema, {"type": "null"}]},
        },
        "required": [
            "story_id",
            "scope",
            "scope_basis_fragment_ids",
            "scope_confidence",
            "scope_reason",
            "retention",
            "enrichment",
            "exclusion_reason",
            "confidence",
            "reason",
            "same_incident_context_ids",
            "brief_payload",
        ],
    }

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "event_triage",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "results": {
                        "type": "array",
                        "items": result_item_schema,
                        "minItems": story_count,
                        "maxItems": story_count,
                    }
                },
                "required": ["results"],
            },
        },
    }


@dataclass(frozen=True)
class StoryGateResult:
    story_id: int
    scope: EditionScopeClass
    scope_confidence: float
    scope_reason: str
    retention: Literal["KEEP", "DROP"]
    enrichment: Literal["NONE", "BRIEF", "ANALYZE"]
    exclusion_reason: str | None
    confidence: float
    reason: str
    brief_payload: EventPayload | None
    scope_basis_fragment_ids: tuple[int, ...] = ()
    same_incident_story_ids: tuple[int, ...] = ()

    @property
    def decision(self) -> str:
        """Compatibility projection for legacy audit code."""
        return "ANALYZE" if self.retention == "KEEP" else "IGNORE"


_TRIAGE_ERROR_MESSAGE_LIMIT = 300
_SECRET_TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+", re.IGNORECASE)


def _triage_error_detail(exc: BaseException) -> tuple[int | None, str, str]:
    """HTTP status, exception class and a short message for a failed Gate batch.

    The message is for diagnosis only: it is bounded and stripped of anything shaped like a key.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        status = None
    text = " ".join(_SECRET_TOKEN.sub("[redacted]", str(exc)).split())
    return status, type(exc).__name__, text[:_TRIAGE_ERROR_MESSAGE_LIMIT]


@dataclass(frozen=True)
class StoryGateBatchResult:
    results: tuple[StoryGateResult, ...]
    deferred_story_ids: tuple[int, ...]
    batch_error_kind: str | None = None
    prompt_hash: str | None = None
    fence_lost_story_ids: tuple[int, ...] = ()
    missing_story_ids: tuple[int, ...] = ()
    invalid_story_ids: tuple[int, ...] = ()
    # Per-story validation reason for invalid_story_ids (diagnostics only).
    invalid_reasons: dict[int, str] = field(default_factory=dict, compare=False)


DecisionFence = Callable[[psycopg.AsyncConnection, int, int], Awaitable[bool]]
DecisionPersistHook = Callable[[psycopg.AsyncConnection, StoryGateResult, int], Awaitable[None]]


# Backward compatibility aliases
StoryTriageResult = StoryGateResult
StoryTriageBatchResult = StoryGateBatchResult


class StoryTriageService:
    """Classifies candidate story clusters via Gate V2 (scope, retention, enrichment, brief)."""

    def __init__(
        self,
        ai_cascade: Any,
        cluster_repo: EventClusterRepository | None = None,
        model: str | None = None,
        logger_instance: logging.Logger | None = None,
        uow: Any | None = None,
        max_output_tokens: int = 12_288,
        reasoning_effort: str | None = "low",
        conversation_timeline: ConversationTimelineSettings | None = None,
    ) -> None:
        self.ai = ai_cascade
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.model = model or "default"
        self.logger = logger_instance or logger
        self.uow = uow
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.conversation_timeline = conversation_timeline

    async def triage_stories_batch(
        self,
        conn: psycopg.AsyncConnection | None = None,
        stories: list[StoryClusterState] | None = None,
        *,
        edition_id: int,
        scope_config: EditionScopeConfig,
        scope_hash: str,
        excerpt_chars: int = 320,
        min_ignore_confidence: float = 0.95,
        max_gate_fragments: int = 6,
        assignment_id_by_story: Mapping[int, int] | None = None,
        source_cutoff_at: dt.datetime | None = None,
        decision_fence: DecisionFence | None = None,
        before_decision_persist: DecisionPersistHook | None = None,
        max_output_tokens: int | None = None,
        max_input_chars: int | None = None,
    ) -> StoryGateBatchResult:
        """Run batch Gate V2 classification on story clusters."""
        if not stories:
            return StoryGateBatchResult(results=(), deferred_story_ids=())

        @asynccontextmanager
        async def _get_conn():
            if conn is not None:
                yield conn
            elif self.uow is not None:
                async with self.uow.transaction() as c:
                    yield c
            else:
                raise ValueError("Either conn or uow must be provided for triage_stories_batch")

        async with _get_conn() as read_conn:
            effective_stories: list[StoryClusterState] = []
            for story in stories:
                if assignment_id_by_story is None:
                    effective_stories.append(story)
                    continue
                target_assignment = assignment_id_by_story.get(
                    story.story_id, story.latest_assignment_id
                )
                (
                    fragment_count,
                    source_count,
                    last_seen_at,
                ) = await self.cluster_repo.get_assignment_snapshot_metrics(
                    read_conn,
                    story_id=story.story_id,
                    assignment_id=target_assignment,
                    source_cutoff_at=source_cutoff_at,
                )
                effective_stories.append(
                    replace(
                        story,
                        latest_assignment_id=target_assignment,
                        fragment_count=fragment_count,
                        unique_source_count=source_count,
                        last_seen_at=last_seen_at,
                    )
                )

            # 1. Lookup cached Gate V2 results
            # Background cache rows do not persist the full fragment set used
            # for scope reasoning. A publication cutoff therefore requires a
            # fresh exact Gate snapshot; otherwise an older cache could have
            # incorporated a post-cutoff source item assigned before the target.
            cached_results = (
                {}
                if source_cutoff_at is not None
                else await self._lookup_cached_decisions(read_conn, effective_stories, scope_hash)
            )
            uncached_stories = [s for s in effective_stories if s.story_id not in cached_results]

            valid_results: list[StoryGateResult] = [
                cached_results[s.story_id]
                for s in effective_stories
                if s.story_id in cached_results
            ]
            deferred_ids: list[int] = []
            fence_lost_ids: set[int] = set()

            if not uncached_stories:
                if before_decision_persist is not None:
                    async with _get_conn() as write_conn:
                        for story in effective_stories:
                            cached = cached_results[story.story_id]
                            if cached.retention != "KEEP":
                                continue
                            assignment_id = (
                                assignment_id_by_story.get(
                                    story.story_id, story.latest_assignment_id
                                )
                                if assignment_id_by_story is not None
                                else story.latest_assignment_id
                            )
                            if decision_fence is not None and not await decision_fence(
                                write_conn, story.story_id, assignment_id
                            ):
                                fence_lost_ids.add(story.story_id)
                                continue
                            await before_decision_persist(write_conn, cached, assignment_id)
                return StoryGateBatchResult(
                    results=tuple(
                        result for result in valid_results if result.story_id not in fence_lost_ids
                    ),
                    deferred_story_ids=(),
                    fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
                )

            # 2. Fetch fragment metadata for uncached stories
            story_ids = [s.story_id for s in uncached_stories]
            cursor = await read_conn.execute(
                """
                SELECT sf.story_id, f.id, f.text_content, s.id, s.name,
                       COALESCE(s.role, s.kind, 'unknown'),
                       COALESCE(si.published_at, si.first_collected_at, f.created_at),
                       si.parent_item_id,
                       si.id
                FROM story_fragments sf
                JOIN source_fragments f ON f.id = sf.fragment_id
                JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN sources s ON s.id = si.source_id
                WHERE sf.story_id = ANY(%s)
                ORDER BY sf.story_id, f.id DESC
                """,
                (story_ids,),
            )

            if assignment_id_by_story is not None:
                await cursor.close()
                target_story_ids = [story.story_id for story in uncached_stories]
                target_assignment_ids = [story.latest_assignment_id for story in uncached_stories]
                cursor = await read_conn.execute(
                    """
                    WITH target(story_id, assignment_id) AS (
                        SELECT * FROM unnest(%s::bigint[], %s::bigint[])
                    ), target_assignment AS (
                        SELECT t.story_id, t.assignment_id, sf.assigned_at
                        FROM target t
                        JOIN story_fragments sf
                          ON sf.id = t.assignment_id
                         AND sf.story_id = t.story_id
                    )
                    SELECT sf.story_id, f.id, f.text_content, s.id, s.name,
                           COALESCE(s.role, s.kind, 'unknown'),
                           COALESCE(si.published_at, si.first_collected_at, f.created_at),
                           si.parent_item_id,
                           si.id
                    FROM target_assignment ta
                    JOIN story_fragments sf ON sf.story_id = ta.story_id
                    JOIN source_fragments f ON f.id = sf.fragment_id
                    JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
                    JOIN source_items si ON si.id = sir.source_item_id
                    JOIN sources s ON s.id = si.source_id
                    WHERE (sf.assigned_at, sf.id) <= (ta.assigned_at, ta.assignment_id)
                      AND (
                          %s::timestamptz IS NULL
                          OR COALESCE(si.published_at, si.first_collected_at, f.created_at)
                             <= %s
                      )
                    ORDER BY sf.story_id, sf.id DESC
                    """,
                    (
                        target_story_ids,
                        target_assignment_ids,
                        source_cutoff_at,
                        source_cutoff_at,
                    ),
                )

            story_fragments_map: dict[int, list[dict[str, Any]]] = {sid: [] for sid in story_ids}
            all_story_frag_ids: dict[int, set[int]] = {sid: set() for sid in story_ids}
            all_parent_item_ids: set[int] = set()
            async for row in cursor:
                sid = int(row[0])
                fid = int(row[1])
                full_text = str(row[2])
                text = full_text[:excerpt_chars]
                source_id = int(row[3])
                source_name = str(row[4])
                source_role = str(row[5])
                obs_time = row[6]
                # Some unit-test/fake cursors and pre-parent_item_id query
                # adapters still provide the original seven-column row.
                parent_item_id = int(row[7]) if len(row) > 7 and row[7] is not None else None
                item_id = int(row[8]) if len(row) > 8 and row[8] is not None else None
                if parent_item_id is not None:
                    all_parent_item_ids.add(parent_item_id)
                story_fragments_map[sid].append(
                    {
                        "fragment_id": fid,
                        "text": text,
                        "full_text": full_text,
                        "source_id": source_id,
                        "source_name": source_name,
                        "source_role": source_role,
                        "observed_at": obs_time,
                        "parent_item_id": parent_item_id,
                        "item_id": item_id,
                    }
                )
                all_story_frag_ids[sid].add(fid)

            # Load reply parent texts if present (as-of source_cutoff_at)
            parent_texts: dict[int, str] = {}
            if all_parent_item_ids:
                cur_parents = await read_conn.execute(
                    """
                    SELECT sir.source_item_id, sir.text_content
                    FROM source_item_revisions sir
                    JOIN (
                        SELECT source_item_id, MAX(revision_no) as max_rev
                        FROM source_item_revisions
                        WHERE source_item_id = ANY(%s)
                          AND (%s::timestamptz IS NULL OR collected_at <= %s)
                        GROUP BY source_item_id
                    ) latest ON latest.source_item_id = sir.source_item_id AND latest.max_rev = sir.revision_no
                    """,
                    (list(all_parent_item_ids), source_cutoff_at, source_cutoff_at),
                )
                async for p_row in cur_parents:
                    parent_texts[int(p_row[0])] = str(p_row[1])[:200]

            # Other paragraphs of each fragment's own source message. They share
            # the fragment's revision, so a source cutoff applies to them equally.
            same_message_context = await self._load_same_message_context(
                read_conn,
                story_fragments_map,
            )

            timeline = await self._load_timeline(
                read_conn, story_fragments_map, edition_id, source_cutoff_at
            )

            # 3. Load dynamic recent subject hints
            recent_hints = await self._load_recent_subject_hints(read_conn, edition_id)

            # 4. Build prompt with geographic context
            cur_slug = await read_conn.execute(
                "SELECT slug FROM editions WHERE id = %s", (edition_id,)
            )
            row_slug = await cur_slug.fetchone()
            edition_slug = str(row_slug[0]) if row_slug else "unknown"

        from src.domain.edition_geography import (
            resolve_edition_geography,
            standardize_edition_area_slang,
        )

        geo_context = resolve_edition_geography(edition_slug, scope_config.name)

        # Select at most max_gate_fragments per story deterministically:
        # first newest fragment from each distinct source, then newest unused
        story_sampled_excerpts: dict[int, list[str]] = {}
        for sid, frags in story_fragments_map.items():
            selected_frags: list[dict[str, Any]] = []
            seen_sources: set[int] = set()
            # 1. Newest from distinct sources
            for frag in frags:
                src_id = frag["source_id"]
                if src_id not in seen_sources:
                    seen_sources.add(src_id)
                    selected_frags.append(frag)
                if len(selected_frags) >= max_gate_fragments:
                    break
            # 2. Fill remaining slots
            if len(selected_frags) < max_gate_fragments:
                selected_ids = {f["fragment_id"] for f in selected_frags}
                for frag in frags:
                    if frag["fragment_id"] not in selected_ids:
                        selected_frags.append(frag)
                    if len(selected_frags) >= max_gate_fragments:
                        break

            # Replies joined by their link would otherwise crowd out the
            # message they answer, which is the first report of the Story.
            if (
                self.conversation_timeline is not None
                and frags
                and frags[-1] not in selected_frags
                and selected_frags
            ):
                selected_frags[-1] = frags[-1]

            # Sort selected by fragment_id for stable reading
            selected_frags.sort(key=lambda x: x["fragment_id"])
            excerpt_lines: list[str] = []
            for sf in selected_frags:
                parent_id = sf.get("parent_item_id")
                p_text = (
                    parent_texts.get(int(parent_id))
                    if parent_id is not None and str(parent_id).isdigit()
                    else None
                )
                message_ctx = same_message_context.get(int(sf["fragment_id"]))
                message_text = (
                    same_message_geography_excerpt(
                        message_ctx, scope=scope_config, geo_context=geo_context
                    )
                    if message_ctx is not None
                    else None
                )
                excerpt_lines.append(format_gate_fragment_excerpt(sf, p_text, message_text))
            story_sampled_excerpts[sid] = excerpt_lines

        hint_text = ""
        if recent_hints:
            hint_lines = [f"- {k}: {lbl}" for k, lbl in recent_hints]
            hint_text = (
                "Recent open subject keys for consistency (reuse a key only when semantically exact; create a new key when none fits):\n"
                + "\n".join(hint_lines)
                + "\n\n"
            )

        contract_text = build_scope_contract(scope_config, geo_context)
        prompt_lines = [
            contract_text,
            "",
            f"Minimum confidence for a hard DROP: {min_ignore_confidence:g}. "
            "Below this threshold, supply a faithful brief; keep background-only evidence CONTEXT.",
            hint_text,
        ]
        rendered_timeline = timeline.render(s.story_id for s in uncached_stories)
        if rendered_timeline:
            prompt_lines.extend([rendered_timeline, ""])
        prompt_lines.append("Stories for Gate V2 triage and brief synthesis:")

        for s in uncached_stories:
            excerpts = story_sampled_excerpts.get(s.story_id, [])
            excerpts_str = "\n".join(excerpts) if excerpts else "(No text)"
            prompt_lines.append(
                f"Story #{s.story_id} (fragments={s.fragment_count}, sources={s.unique_source_count}):\n{excerpts_str}\n"
            )

        story_ids_str = ", ".join(str(s.story_id) for s in uncached_stories)
        prompt_lines.append(
            f"CRITICAL INSTRUCTION: Your JSON output must contain a 'results' array with EXACTLY {len(uncached_stories)} elements, one for EACH of the requested stories ({story_ids_str}). Do NOT omit any story, do NOT stop after the first story."
        )

        user_prompt = "\n".join(prompt_lines)
        prompt_hash = hashlib.sha256(
            f"{_GATE_V2_SYSTEM_PROMPT}\n{user_prompt}".encode("utf-8")
        ).hexdigest()

        # 5. Prepare audit run info (written to DB after LLM call to avoid holding locks)
        provider_name = getattr(self.ai, "primary_provider_name", None) or getattr(
            self.ai, "provider_name", "ai_cascade"
        )
        model_name = getattr(self.ai, "model_name", None) or self.model or "default"

        # The timeline has its own ceiling (max_lines), so it must not take room
        # from the Stories this batch was sized for.
        if (
            max_input_chars is not None
            and len(user_prompt) - len(rendered_timeline) > max_input_chars
        ):
            try:
                async with _get_conn() as err_conn:
                    await err_conn.execute(
                        """
                        INSERT INTO story_event_triage_runs (
                            triage_version, provider, model, prompt_hash, story_count, input_chars, status, error_kind, completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'failed', %s, now())
                        """,
                        (
                            TRIAGE_VERSION,
                            str(provider_name),
                            str(model_name),
                            prompt_hash,
                            len(uncached_stories),
                            len(user_prompt),
                            "input_too_large",
                        ),
                    )
            except Exception as log_exc:
                self.logger.warning("Failed to record failed triage run: %s", log_exc)
            self.logger.warning(
                "Story triage user prompt exceeded max_input_chars (%d > %d); failing batch as recoverable",
                len(user_prompt),
                max_input_chars,
            )
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=tuple(s.story_id for s in uncached_stories),
                batch_error_kind="input_too_large",
                prompt_hash=prompt_hash,
                fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
                invalid_story_ids=tuple(s.story_id for s in uncached_stories),
            )

        # 6. Call LLM
        try:
            with llm_call_context(
                stage="event_triage",
                prompt_hash=prompt_hash,
                story_count=len(uncached_stories),
            ):
                if hasattr(self.ai, "generate_text"):
                    raw_response = await self.ai.generate_text(
                        prompt=user_prompt,
                        system_prompt=_GATE_V2_SYSTEM_PROMPT,
                        temperature=0.0,
                        json_mode=True,
                        max_tokens=max_output_tokens or self.max_output_tokens,
                        reasoning_effort=self.reasoning_effort,
                    )
                elif hasattr(self.ai, "chat_completion"):
                    raw_response = await self.ai.chat_completion(
                        messages=[
                            {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        model=self.model,
                        temperature=0.0,
                        max_tokens=max_output_tokens or self.max_output_tokens,
                        reasoning_effort=self.reasoning_effort,
                        response_format=build_gate_response_format(len(uncached_stories)),
                    )
                else:
                    raise TypeError(f"Unsupported AI provider type: {type(self.ai)}")
        except Exception as exc:
            provider_kind = classify_provider_failure(exc)
            try:
                async with _get_conn() as err_conn:
                    await err_conn.execute(
                        """
                        INSERT INTO story_event_triage_runs (
                            triage_version, provider, model, prompt_hash, story_count, input_chars, status, error_kind, error_status, error_type, error_message, completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'failed', %s, %s, %s, %s, now())
                        """,
                        (
                            TRIAGE_VERSION,
                            str(provider_name),
                            str(model_name),
                            prompt_hash,
                            len(uncached_stories),
                            len(user_prompt),
                            provider_kind,
                            *_triage_error_detail(exc),
                        ),
                    )
            except Exception as log_exc:
                self.logger.warning("Failed to record failed triage run: %s", log_exc)
            self.logger.warning("Story triage AI call failed: %s; deferring stories", exc)
            recoverable_ids = (
                tuple(s.story_id for s in uncached_stories)
                if provider_kind in ("context_size", "token_budget")
                else ()
            )
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=tuple(s.story_id for s in uncached_stories),
                batch_error_kind=provider_kind,
                prompt_hash=prompt_hash,
                fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
                invalid_story_ids=recoverable_ids,
            )

        try:
            from src.utils import robust_extract_json

            payload = robust_extract_json(raw_response)
            if not isinstance(payload, dict):
                raise ValueError("gate response must be a JSON object")
            raw_items = payload.get("results")
            if not isinstance(raw_items, list):
                raise ValueError("gate response missing results list")
        except Exception as exc:
            try:
                async with _get_conn() as err_conn:
                    await err_conn.execute(
                        """
                        INSERT INTO story_event_triage_runs (
                            triage_version, provider, model, prompt_hash, story_count, input_chars, output_chars, status, error_kind, error_status, error_type, error_message, completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed', 'invalid_response', %s, %s, %s, now())
                        """,
                        (
                            TRIAGE_VERSION,
                            str(provider_name),
                            str(model_name),
                            prompt_hash,
                            len(uncached_stories),
                            len(user_prompt),
                            len(raw_response),
                            *_triage_error_detail(exc),
                        ),
                    )
            except Exception as log_exc:
                self.logger.warning("Failed to record failed triage run: %s", log_exc)
            self.logger.warning(
                "Story triage response parse failed: %s; deferring stories as invalid_response",
                exc,
            )
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=tuple(s.story_id for s in uncached_stories),
                batch_error_kind="invalid_response",
                prompt_hash=prompt_hash,
                fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
                invalid_story_ids=tuple(s.story_id for s in uncached_stories),
            )

        items_by_id: dict[int, dict[str, Any]] = {}
        for item in raw_items:
            if isinstance(item, dict) and isinstance(item.get("story_id"), int):
                items_by_id[item["story_id"]] = item

        expected_story_ids = {story.story_id for story in uncached_stories}
        returned_story_ids = set(items_by_id) & expected_story_ids
        missing_story_ids = tuple(
            story.story_id for story in uncached_stories if story.story_id not in returned_story_ids
        )

        invalid_ids: list[int] = []
        invalid_reasons: dict[int, str] = {}
        new_valid_results: list[StoryGateResult] = []

        def mark_invalid(story_id: int, reason: str) -> None:
            deferred_ids.append(story_id)
            invalid_ids.append(story_id)
            invalid_reasons.setdefault(story_id, reason)

        for s in uncached_stories:
            item = items_by_id.get(s.story_id)
            if item is None or not isinstance(item, dict):
                deferred_ids.append(s.story_id)
                if s.story_id in returned_story_ids:
                    invalid_reasons[s.story_id] = "result_not_object"
                    invalid_ids.append(s.story_id)
                continue

            scope_raw = str(item.get("scope", "")).strip()
            if scope_raw not in {"LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"}:
                mark_invalid(s.story_id, "invalid_scope")
                continue
            scope: EditionScopeClass = scope_raw  # type: ignore[assignment]

            scope_conf = item.get("scope_confidence")
            if (
                isinstance(scope_conf, bool)
                or not isinstance(scope_conf, (int, float))
                or not (0.0 <= float(scope_conf) <= 1.0)
            ):
                mark_invalid(s.story_id, "invalid_scope_confidence")
                continue
            scope_confidence = float(scope_conf)

            scope_reason = item.get("scope_reason")
            if not isinstance(scope_reason, str) or not scope_reason.strip():
                mark_invalid(s.story_id, "missing_scope_reason")
                continue

            conf = item.get("confidence")
            if (
                isinstance(conf, bool)
                or not isinstance(conf, (int, float))
                or not (0.0 <= float(conf) <= 1.0)
            ):
                mark_invalid(s.story_id, "invalid_confidence")
                continue
            confidence = float(conf)

            reason = str(item.get("reason", "")).strip()
            if (
                story_sampled_excerpts.get(s.story_id)
                and any(sf["text"].strip() for sf in story_fragments_map.get(s.story_id, []))
                and (
                    _MISSING_GATE_INPUT_RE.search(scope_reason)
                    or _MISSING_GATE_INPUT_RE.search(reason)
                )
            ):
                mark_invalid(s.story_id, "delivered_input_reported_missing")
                continue
            retention_raw = str(item.get("retention", "")).strip().upper()
            enrichment_raw = str(item.get("enrichment", "")).strip().upper()
            ex_reason_raw = item.get("exclusion_reason")
            ex_reason = (
                str(ex_reason_raw).strip() if ex_reason_raw in _ALLOWED_EXCLUSION_REASONS else None
            )

            allowed_fids = all_story_frag_ids.get(s.story_id, set())

            raw_scope_basis = item.get("scope_basis_fragment_ids", [])
            if isinstance(raw_scope_basis, (list, tuple)):
                scope_basis_ids = tuple(
                    int(x)
                    for x in raw_scope_basis
                    if isinstance(x, (int, str)) and str(x).isdigit()
                )
            else:
                scope_basis_ids = ()

            if set(scope_basis_ids) - allowed_fids:
                mark_invalid(s.story_id, "scope_basis_fragment_not_in_story")
                continue

            if scope in {"LOCAL", "DIRECT_IMPACT"} and not scope_basis_ids:
                mark_invalid(s.story_id, "missing_scope_basis_fragments")
                continue

            basis_texts = tuple(
                sf["text"]
                for sf in story_fragments_map.get(s.story_id, [])
                if sf["fragment_id"] in scope_basis_ids
            )
            story_frags = story_fragments_map.get(s.story_id, [])
            story_all_texts = tuple(
                str(sf.get("text") or sf.get("text_content", "")) for sf in story_frags
            )
            if scope in {"LOCAL", "DIRECT_IMPACT"}:
                if broad_region_without_focus_impact(
                    basis_texts=basis_texts,
                    scope=scope_config,
                    geo_context=geo_context,
                ):
                    scope = "OUT_OF_SCOPE"
                    scope_confidence = max(scope_confidence, 0.95)
                    scope_reason = (
                        "Broad regional summary without explicit configured focus-area consequence"
                    )
                elif external_city_without_focus_impact(
                    basis_texts=(*basis_texts, *story_all_texts),
                    scope=scope_config,
                    geo_context=geo_context,
                ):
                    scope = "OUT_OF_SCOPE"
                    scope_confidence = max(scope_confidence, 0.95)
                    scope_reason = (
                        "External city event without explicit configured focus-area consequence"
                    )
                elif external_relocated_idp_event(basis_texts=(*basis_texts, *story_all_texts)):
                    scope = "OUT_OF_SCOPE"
                    scope_confidence = max(scope_confidence, 0.95)
                    scope_reason = "Displaced persons (IDP) or relocated administration activity outside the focus area"
                elif story_placed_outside_by_message(
                    basis_fragments=[
                        _fragment_message_context(sf, same_message_context)
                        for sf in story_frags
                        if sf["fragment_id"] in scope_basis_ids
                    ],
                    story_fragments=[
                        _fragment_message_context(sf, same_message_context) for sf in story_frags
                    ],
                    scope=scope_config,
                    geo_context=geo_context,
                ):
                    scope = "OUT_OF_SCOPE"
                    scope_confidence = max(scope_confidence, 0.95)
                    scope_reason = (
                        "Same source message places the event outside the configured focus area"
                    )

            # Hard exclusion audit on story fragments
            story_frag_texts: dict[int, str] = {}
            story_reply_parent_contexts: dict[int, str] = {}
            conversation_service_families: dict[int, str] = {}
            for sf in story_frags:
                fid = int(sf.get("fragment_id") or sf.get("id", 0))
                txt = str(sf.get("text") or sf.get("text_content", ""))
                parent_id = sf.get("parent_item_id")
                p_id = (
                    int(parent_id) if parent_id is not None and str(parent_id).isdigit() else None
                )
                if p_id is not None and p_id in parent_texts:
                    story_reply_parent_contexts[fid] = parent_texts[p_id]
                story_frag_texts[fid] = txt
                omitted_service = timeline.omitted_service_family(
                    sf.get("item_id"), parent_text=story_reply_parent_contexts.get(fid, "")
                )
                if omitted_service is not None:
                    conversation_service_families[fid] = omitted_service
            hard_audit = evaluate_story_hard_exclusion(story_frags)
            timeline_texts = timeline.context_texts_for(s.story_id)
            message_contexts = [
                _fragment_message_context(sf, same_message_context) for sf in story_frags
            ]
            # Reply parents are non-citable context for a place they alone name.
            story_message_own_texts = [
                *(standardize_edition_area_slang(c.text, edition_slug) for c in message_contexts),
                *(
                    text
                    for context in message_contexts
                    for text in (*context.earlier_message_texts, *context.later_message_texts)
                ),
            ]
            # Gate sees each message's date, so the day it was posted counts as
            # stated. Official area names replace slang by contract (§3).
            story_own_texts = [
                *(standardize_edition_area_slang(c.text, edition_slug) for c in message_contexts),
                *(
                    standardize_edition_area_slang(text, edition_slug)
                    for text in story_reply_parent_contexts.values()
                ),
                *(
                    text
                    for context in message_contexts
                    for text in (*context.earlier_message_texts, *context.later_message_texts)
                ),
                *(str(sf["observed_at"].day) for sf in story_frags),
            ]

            # Parse brief_payload if present
            raw_brief = item.get("brief_payload")
            brief_payload: EventPayload | None = None
            rejected_publish_extraction = False
            if isinstance(raw_brief, dict):
                try:
                    parsed_payload = parse_event_payload(
                        raw_brief, allowed_fragment_ids=allowed_fids
                    )
                    decomposed = decompose_mixed_outage_evidence(parsed_payload, story_frag_texts)
                    brief_payload = normalize_question_evidence(decomposed)
                    pre_service_normalization = brief_payload
                    brief_payload, service_audit = normalize_service_state_evidence(
                        brief_payload,
                        story_frag_texts,
                        reply_parent_context_by_fragment_id=story_reply_parent_contexts,
                        edition_name=scope_config.name,
                        conversation_service_family_by_fragment_id=conversation_service_families,
                    )
                    rejected_publish_extraction = any(
                        pre_service_normalization.evidence_items[index].publication_use == "PUBLISH"
                        and brief_payload.evidence_items[index].publication_use != "PUBLISH"
                        for index in service_audit.rejected_evidence_indexes
                    )
                    brief_payload, withheld_context_detail = withhold_context_only_evidence(
                        brief_payload,
                        own_texts=story_own_texts,
                        context_texts=timeline_texts,
                        scope=scope_config,
                        geo_context=geo_context,
                    )
                    rejected_publish_extraction = (
                        rejected_publish_extraction or withheld_context_detail
                    )
                    # Before the headline check: a place only the timeline or a reply
                    # parent names is cleared; the item stays PUBLISH.
                    brief_payload, _ = clear_context_only_service_locations(
                        brief_payload,
                        own_texts=story_message_own_texts,
                        context_texts=(
                            *timeline_texts,
                            *(
                                standardize_edition_area_slang(text, edition_slug)
                                for text in story_reply_parent_contexts.values()
                            ),
                        ),
                        scope=scope_config,
                        geo_context=geo_context,
                    )
                    if service_audit.rejected_count > 0:
                        self.logger.debug(
                            "Gate normalized %s unsupported service claims/states for story %s: %s",
                            service_audit.rejected_count,
                            s.story_id,
                            service_audit.rejection_reasons,
                        )
                    if has_unstructured_publish_service_access(brief_payload):
                        self.logger.debug(
                            "Gate story %s has unstructured publish service_access evidence",
                            s.story_id,
                        )
                except Exception as e:
                    self.logger.debug("Brief payload parsing error for story %s: %s", s.story_id, e)
                    brief_payload = None

            # Normalization rules
            if scope in {"OUT_OF_SCOPE", "UNCERTAIN"}:
                retention: Literal["KEEP", "DROP"] = "DROP"
                enrichment: Literal["NONE", "BRIEF", "ANALYZE"] = "NONE"
                ex_reason = None
                brief_payload = None
            elif scope in {"LOCAL", "DIRECT_IMPACT"}:
                if hard_audit.drop_story:
                    # Deterministic hard-exclusion override: all substantive fragments are noise/commercial
                    retention = "DROP"
                    enrichment = "NONE"
                    ex_reason = hard_audit.story_exclusion_reason or "commercial_classified"
                    brief_payload = None
                elif retention_raw == "DROP":
                    # High-confidence LLM hard exclusions
                    if (
                        enrichment_raw == "NONE"
                        and ex_reason in _ALLOWED_EXCLUSION_REASONS
                        and confidence >= min_ignore_confidence
                    ):
                        retention = "DROP"
                        enrichment = "NONE"
                        brief_payload = None
                    elif brief_payload is not None:
                        # Unsafe drop normalized to KEEP+BRIEF
                        retention = "KEEP"
                        enrichment = "BRIEF"
                        ex_reason = None
                        decomposed = decompose_mixed_outage_evidence(
                            brief_payload, story_frag_texts
                        )
                        brief_payload = ensure_keep_publishability(
                            normalize_question_evidence(decomposed), default="brief"
                        )
                        brief_payload, _ = normalize_service_state_evidence(
                            brief_payload,
                            story_frag_texts,
                            reply_parent_context_by_fragment_id=story_reply_parent_contexts,
                            edition_name=scope_config.name,
                            conversation_service_family_by_fragment_id=conversation_service_families,
                        )
                    else:
                        # Unsafe drop without a valid brief must defer
                        mark_invalid(s.story_id, "drop_without_valid_brief")
                        continue
                elif retention_raw == "KEEP":
                    if enrichment_raw not in ("BRIEF", "ANALYZE") or brief_payload is None:
                        mark_invalid(s.story_id, "keep_without_valid_brief")
                        continue
                    retention = "KEEP"
                    enrichment = enrichment_raw  # type: ignore[assignment]
                    ex_reason = None
                    decomposed = decompose_mixed_outage_evidence(brief_payload, story_frag_texts)
                    brief_payload = ensure_keep_publishability(
                        normalize_question_evidence(decomposed), default="brief"
                    )
                    brief_payload, _ = normalize_service_state_evidence(
                        brief_payload,
                        story_frag_texts,
                        reply_parent_context_by_fragment_id=story_reply_parent_contexts,
                        edition_name=scope_config.name,
                        conversation_service_family_by_fragment_id=conversation_service_families,
                    )
                else:
                    mark_invalid(s.story_id, "invalid_retention_or_enrichment")
                    continue

                # If retention is KEEP and story is mixed (has excluded fragments),
                # normalize evidence items deriving from excluded fragments to EXCLUDE
                if (
                    retention == "KEEP"
                    and brief_payload is not None
                    and hard_audit.excluded_fragment_ids
                ):
                    excluded_set = set(hard_audit.excluded_fragment_ids)
                    cleaned_items = []
                    for evi in brief_payload.evidence_items:
                        if evi.source_fragment_ids and set(evi.source_fragment_ids).issubset(
                            excluded_set
                        ):
                            cleaned_items.append(
                                replace(evi, publication_use="EXCLUDE", service_state=None)
                            )
                        else:
                            cleaned_items.append(evi)
                    brief_payload = replace(brief_payload, evidence_items=tuple(cleaned_items))

                # Invariant: A KEEP story must contain at least 1 legitimate PUBLISH evidence item
                if retention == "KEEP" and brief_payload is not None:
                    has_publish = any(
                        evi.publication_use == "PUBLISH" for evi in brief_payload.evidence_items
                    )
                    if not has_publish:
                        # Failed extraction says nothing about the source's
                        # news value. Route it to existing bounded recovery,
                        # rather than caching it as a genuine noise decision.
                        if rejected_publish_extraction:
                            mark_invalid(s.story_id, "unsupported_publish_extraction")
                            continue
                        all_context_or_noise = all(
                            evi.kind in ("resident_question", "commercial_offer")
                            or evi.publication_use in ("CONTEXT", "EXCLUDE")
                            for evi in brief_payload.evidence_items
                        )
                        if all_context_or_noise:
                            self.logger.info(
                                "Story %s has 0 PUBLISH items (pure context/questions/noise); normalizing to DROP / obvious_noise",
                                s.story_id,
                            )
                            retention = "DROP"
                            enrichment = "NONE"
                            ex_reason = "obvious_noise"
                            brief_payload = None
                        else:
                            self.logger.warning(
                                "Story %s has 0 PUBLISH items unexpectedly; deferring for re-analysis",
                                s.story_id,
                            )
                            mark_invalid(s.story_id, "keep_without_publish_evidence")
                            continue
            else:
                mark_invalid(s.story_id, "invalid_scope_classification")
                continue

            # A summary that names a detail only the timeline states would carry
            # another message's place or number into the digest.
            if (
                retention == "KEEP"
                and brief_payload is not None
                and any(
                    context_only_details(
                        text,
                        own_texts=story_own_texts,
                        context_texts=timeline_texts,
                        scope=scope_config,
                        geo_context=geo_context,
                    )
                    for text in (brief_payload.headline, brief_payload.digest_summary)
                )
            ):
                mark_invalid(s.story_id, "context_detail_in_summary")
                continue

            # "Девятиэтажки все горят" reports lit windows. A fire reading whose
            # sources only say "горит", with no fire, strike, or burning object in
            # any source, parent, or same-message text, is a misread extraction:
            # route it to bounded recovery, never publish it.
            if retention == "KEEP" and brief_payload is not None:
                if unsupported_fire_reading(
                    brief_payload,
                    source_texts=[context.text for context in message_contexts],
                    context_texts=[
                        *story_reply_parent_contexts.values(),
                        *(
                            text
                            for context in message_contexts
                            for text in (
                                *context.earlier_message_texts,
                                *context.later_message_texts,
                            )
                        ),
                    ],
                ):
                    mark_invalid(s.story_id, "unsupported_fire_reading")
                    continue

            gate_res = StoryGateResult(
                story_id=s.story_id,
                scope=scope,
                scope_confidence=scope_confidence,
                scope_reason=scope_reason.strip(),
                retention=retention,
                enrichment=enrichment,
                exclusion_reason=ex_reason,
                confidence=confidence,
                reason=reason,
                brief_payload=brief_payload,
                scope_basis_fragment_ids=scope_basis_ids,
                same_incident_story_ids=timeline.same_incident_story_ids(
                    item.get("same_incident_context_ids"),
                    story_id=s.story_id,
                    own_texts=story_own_texts,
                    scope=scope_config,
                    geo_context=geo_context,
                ),
            )
            new_valid_results.append(gate_res)

        invalid_story_ids = tuple(invalid_ids)
        try:
            async with _get_conn() as write_conn:
                has_missing = bool(missing_story_ids)
                has_invalid = bool(invalid_story_ids)
                run_status = "failed" if (has_missing or has_invalid) else "succeeded"
                run_error_kind = (
                    "partial_response"
                    if (has_missing and not has_invalid)
                    else ("invalid_response" if has_invalid else None)
                )
                if has_missing or has_invalid:
                    log_event = (
                        "event_first_gate_partial_response"
                        if (has_missing and not has_invalid)
                        else "event_first_gate_invalid_response"
                    )
                    self.logger.warning(
                        log_event,
                        extra={
                            "requested_count": len(uncached_stories),
                            "valid_count": len(new_valid_results),
                            "deferred_count": len(deferred_ids),
                            "missing_count": len(missing_story_ids),
                            "missing_story_ids": missing_story_ids,
                            "invalid_count": len(invalid_story_ids),
                            "invalid_story_ids": invalid_story_ids,
                            "invalid_reasons": {
                                str(story_id): invalid_reasons.get(story_id, "validation_failed")
                                for story_id in invalid_story_ids
                            },
                            "prompt_hash": prompt_hash,
                            "error_kind": run_error_kind,
                        },
                    )
                cursor = await write_conn.execute(
                    """
                    INSERT INTO story_event_triage_runs (
                        triage_version, provider, model, prompt_hash, story_count, input_chars, output_chars, status, error_kind, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    RETURNING id
                    """,
                    (
                        TRIAGE_VERSION,
                        str(provider_name),
                        str(model_name),
                        prompt_hash,
                        len(uncached_stories),
                        len(user_prompt),
                        len(raw_response),
                        run_status,
                        run_error_kind,
                    ),
                )
                run_row = await cursor.fetchone()
                if run_row is None:
                    raise RuntimeError("Failed to insert story_event_triage_runs")
                run_id = int(run_row[0])

                # 7. Persist decisions
                s_map = {s.story_id: s for s in uncached_stories}
                for res in new_valid_results:
                    st = s_map[res.story_id]
                    assignment_id = (
                        assignment_id_by_story.get(res.story_id, st.latest_assignment_id)
                        if assignment_id_by_story is not None
                        else st.latest_assignment_id
                    )
                    if decision_fence is not None and not await decision_fence(
                        write_conn, res.story_id, assignment_id
                    ):
                        fence_lost_ids.add(res.story_id)
                        continue
                    if before_decision_persist is not None:
                        await before_decision_persist(write_conn, res, assignment_id)
                    # Scope decision
                    await write_conn.execute(
                        """
                        INSERT INTO story_edition_scope_decisions (
                            triage_run_id,
                            story_id,
                            edition_id,
                            latest_assignment_id,
                            scope_version,
                            scope_config_hash,
                            scope_class,
                            confidence,
                            reason
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (
                            story_id,
                            latest_assignment_id,
                            scope_version,
                            scope_config_hash
                        ) DO NOTHING
                        """,
                        (
                            run_id,
                            res.story_id,
                            edition_id,
                            assignment_id,
                            SCOPE_VERSION,
                            scope_hash,
                            res.scope,
                            res.scope_confidence,
                            res.scope_reason,
                        ),
                    )

                    # Gate V2 decision
                    brief_json = (
                        json.dumps(res.brief_payload.to_dict()) if res.brief_payload else None
                    )
                    await write_conn.execute(
                        """
                        INSERT INTO story_event_triage_decisions (
                            run_id, story_id, latest_assignment_id, triage_version,
                            scope_config_hash, decision, retention, enrichment,
                            exclusion_reason, confidence, reason, brief_payload
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (story_id, latest_assignment_id, triage_version, scope_config_hash)
                        DO UPDATE SET
                            decision = EXCLUDED.decision,
                            retention = EXCLUDED.retention,
                            enrichment = EXCLUDED.enrichment,
                            exclusion_reason = EXCLUDED.exclusion_reason,
                            confidence = EXCLUDED.confidence,
                            reason = EXCLUDED.reason,
                            brief_payload = EXCLUDED.brief_payload
                        """,
                        (
                            run_id,
                            res.story_id,
                            assignment_id,
                            TRIAGE_VERSION,
                            scope_hash,
                            res.decision,
                            res.retention,
                            res.enrichment,
                            res.exclusion_reason,
                            res.confidence,
                            res.reason,
                            brief_json,
                        ),
                    )
                    if res.retention == "KEEP":
                        await self._record_incident_links(write_conn, res)
        except Exception as exc:
            try:
                async with _get_conn() as err_conn:
                    await err_conn.execute(
                        """
                        INSERT INTO story_event_triage_runs (
                            triage_version, provider, model, prompt_hash, story_count, input_chars, status, error_kind, error_status, error_type, error_message, completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'failed', %s, %s, %s, %s, now())
                        """,
                        (
                            TRIAGE_VERSION,
                            str(provider_name),
                            str(model_name),
                            prompt_hash,
                            len(uncached_stories),
                            len(user_prompt),
                            type(exc).__name__,
                            *_triage_error_detail(exc),
                        ),
                    )
            except Exception as log_exc:
                self.logger.warning("Failed to record failed triage run: %s", log_exc)
            self.logger.warning("Story triage batch failed: %s; deferring stories", exc)
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=tuple(s.story_id for s in uncached_stories),
                batch_error_kind=classify_provider_failure(exc),
                prompt_hash=prompt_hash,
                fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
            )

        all_results_by_id = {
            r.story_id: r
            for r in valid_results + new_valid_results
            if r.story_id not in fence_lost_ids
        }
        final_results = [
            all_results_by_id[s.story_id]
            for s in effective_stories
            if s.story_id in all_results_by_id
        ]

        batch_error_kind = (
            "partial_response"
            if (missing_story_ids and not invalid_story_ids)
            else ("invalid_response" if invalid_story_ids else None)
        )

        return StoryGateBatchResult(
            results=tuple(final_results),
            deferred_story_ids=tuple(deferred_ids),
            batch_error_kind=batch_error_kind,
            prompt_hash=prompt_hash,
            fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
            missing_story_ids=missing_story_ids,
            invalid_story_ids=invalid_story_ids,
            invalid_reasons={
                sid: invalid_reasons[sid] for sid in invalid_story_ids if sid in invalid_reasons
            },
        )

    async def _load_timeline(
        self,
        conn: psycopg.AsyncConnection,
        story_fragments_map: Mapping[int, list[dict[str, Any]]],
        edition_id: int,
        source_cutoff_at: dt.datetime | None,
    ) -> ConversationTimeline:
        """Load the chat around the newest fragments of the judged Stories."""
        settings = self.conversation_timeline
        if settings is None:
            return ConversationTimeline()
        anchors = [
            TimelineAnchor(source_id=int(sf["source_id"]), observed_at=sf["observed_at"])
            for frags in story_fragments_map.values()
            for sf in frags[: settings.anchors_per_story]
        ]
        return await load_conversation_timeline(
            conn,
            anchors,
            edition_id=edition_id,
            settings=settings,
            source_cutoff_at=source_cutoff_at,
        )

    async def _record_incident_links(
        self, conn: psycopg.AsyncConnection, result: StoryGateResult
    ) -> None:
        """Remember which other Stories Gate read as the same incident."""
        for linked_story_id in result.same_incident_story_ids:
            await conn.execute(
                """
                INSERT INTO story_relations (from_story_id, to_story_id, relation_type)
                SELECT %(story)s, %(linked)s, %(relation)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM story_relations
                    WHERE from_story_id = %(story)s
                      AND to_story_id = %(linked)s
                      AND relation_type = %(relation)s
                )
                """,
                {
                    "story": result.story_id,
                    "linked": linked_story_id,
                    "relation": SAME_INCIDENT_RELATION,
                },
            )

    async def _load_same_message_context(
        self,
        conn: psycopg.AsyncConnection,
        story_fragments_map: Mapping[int, list[dict[str, Any]]],
    ) -> dict[int, FragmentMessageContext]:
        """Load the other paragraphs of each fragment's source message revision.

        A sibling may sit in another Story or be a dropped noise paragraph; it is
        geographic context only and never a citable fragment of this Story.
        """
        fragment_ids = sorted(
            {int(sf["fragment_id"]) for frags in story_fragments_map.values() for sf in frags}
        )
        if not fragment_ids:
            return {}
        cursor = await conn.execute(
            """
            SELECT f.id, sib.ordinal < f.ordinal, sib.text_content
            FROM source_fragments f
            JOIN source_fragments sib
              ON sib.source_item_revision_id = f.source_item_revision_id
             AND sib.fragmenter_version = f.fragmenter_version
             AND sib.id <> f.id
            WHERE f.id = ANY(%s)
            ORDER BY f.id, sib.ordinal
            """,
            (fragment_ids,),
        )
        earlier: dict[int, list[str]] = {}
        later: dict[int, list[str]] = {}
        async for row in cursor:
            target = earlier if row[1] else later
            target.setdefault(int(row[0]), []).append(str(row[2]))
        return {
            fid: FragmentMessageContext(
                text="",
                earlier_message_texts=tuple(earlier.get(fid, ())),
                later_message_texts=tuple(later.get(fid, ())),
            )
            for fid in set(earlier) | set(later)
        }

    async def _lookup_cached_decisions(
        self,
        conn: psycopg.AsyncConnection,
        stories: list[StoryClusterState],
        scope_hash: str,
    ) -> dict[int, StoryGateResult]:
        if not stories:
            return {}

        story_ids = [s.story_id for s in stories]
        assignment_ids = [s.latest_assignment_id for s in stories]

        cursor = await conn.execute(
            """
            WITH requested(story_id, assignment_id) AS (
                SELECT * FROM unnest(%s::bigint[], %s::bigint[])
            )
            SELECT setd.story_id, sesd.scope_class, sesd.confidence, sesd.reason,
                   setd.retention, setd.enrichment, setd.exclusion_reason,
                   setd.confidence, setd.reason, setd.brief_payload
            FROM story_event_triage_decisions setd
            JOIN story_edition_scope_decisions sesd
              ON sesd.story_id = setd.story_id
             AND sesd.latest_assignment_id = setd.latest_assignment_id
             AND sesd.scope_config_hash = setd.scope_config_hash
            JOIN requested r
              ON r.story_id = setd.story_id
             AND r.assignment_id = setd.latest_assignment_id
            WHERE setd.triage_version = %s
              AND setd.scope_config_hash = %s
            """,
            (
                story_ids,
                assignment_ids,
                TRIAGE_VERSION,
                scope_hash,
            ),
        )

        cached: dict[int, StoryGateResult] = {}
        async for row in cursor:
            sid = int(row[0])
            scope_cls = str(row[1])
            scope_conf = float(row[2])
            scope_reason = str(row[3])
            retention = str(row[4])
            enrichment = str(row[5])
            ex_reason = row[6]
            conf = float(row[7])
            reason = str(row[8])
            raw_brief = row[9]
            try:
                brief_payload = parse_event_payload(raw_brief) if raw_brief else None
            except (TypeError, ValueError):
                # A malformed legacy cache must be re-evaluated by Gate rather
                # than becoming an apparently satisfied authority target.
                continue

            if retention == "KEEP" and (
                brief_payload is None
                or brief_payload.publishability not in {"news", "brief"}
                or not any(
                    item.publication_use == "PUBLISH" for item in brief_payload.evidence_items
                )
            ):
                continue

            cached[sid] = StoryGateResult(
                story_id=sid,
                scope=scope_cls,  # type: ignore[arg-type]
                scope_confidence=scope_conf,
                scope_reason=scope_reason,
                retention=retention,  # type: ignore[arg-type]
                enrichment=enrichment,  # type: ignore[arg-type]
                exclusion_reason=ex_reason,
                confidence=conf,
                reason=reason,
                brief_payload=brief_payload,
                scope_basis_fragment_ids=(),
            )

        return cached

    async def _load_recent_subject_hints(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int,
        limit: int = 30,
    ) -> list[tuple[str, str]]:
        cursor = await conn.execute(
            """
            WITH extracted_hints AS (
                SELECT
                    item->'service_state'->>'subject_key' AS subject_key,
                    item->'service_state'->>'subject_label' AS subject_label
                FROM story_event_triage_decisions setd
                JOIN story_edition_scope_decisions sesd
                  ON sesd.story_id = setd.story_id
                 AND sesd.latest_assignment_id = setd.latest_assignment_id
                 AND sesd.scope_config_hash = setd.scope_config_hash
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(setd.brief_payload->'evidence_items', '[]'::jsonb)) AS item
                WHERE sesd.edition_id = %s
                  AND sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')
                  AND setd.retention = 'KEEP'
                  AND setd.triage_version = %s
                  AND item->'service_state'->>'subject_key' IS NOT NULL

                UNION

                SELECT
                    obs->>'subject_key' AS subject_key,
                    obs->>'subject_label' AS subject_label
                FROM story_event_triage_decisions setd
                JOIN story_edition_scope_decisions sesd
                  ON sesd.story_id = setd.story_id
                 AND sesd.latest_assignment_id = setd.latest_assignment_id
                 AND sesd.scope_config_hash = setd.scope_config_hash
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(setd.brief_payload->'operational_observations', '[]'::jsonb)) AS obs
                WHERE sesd.edition_id = %s
                  AND sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')
                  AND setd.retention = 'KEEP'
                  AND setd.triage_version = %s
                  AND obs->>'subject_key' IS NOT NULL
            )
            SELECT DISTINCT subject_key, subject_label
            FROM extracted_hints
            WHERE subject_key IS NOT NULL AND subject_key != ''
            ORDER BY subject_key
            LIMIT %s
            """,
            (edition_id, TRIAGE_VERSION, edition_id, TRIAGE_VERSION, limit),
        )
        hints: list[tuple[str, str]] = []
        async for row in cursor:
            k, label = str(row[0] or "").strip(), str(row[1] or "").strip()
            if k and label:
                hints.append((k, label))
        return hints
