"""Conservative fact composition and deterministic digest budget admission."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from src.publication.digest_presentation import (
    DigestPresentationPlan,
    RequiredDigestFact,
    validate_digest_fact_ids,
)

COMPOSITION_POLICY_VERSION = "digest_composition_v7"


class DigestFactRelationKind(str, Enum):
    SAME_FACT = "SAME_FACT"
    UPDATE_OF = "UPDATE_OF"
    LOCAL_CONTRAST = "LOCAL_CONTRAST"
    RELATED_ONLY = "RELATED_ONLY"


@dataclass(frozen=True)
class DigestFactRelation:
    left_fact_id: str
    right_fact_id: str
    kind: DigestFactRelationKind
    reason: str


@dataclass(frozen=True)
class DigestFactRecord:
    fact_id: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    rubric_id: str
    canonical_subject: str
    canonical_service: str
    canonical_area: str
    canonical_place: tuple[str, ...]
    original_location: str
    effective_time: dt.datetime | None
    observed_time: dt.datetime | None
    service_state: str
    epistemic_kind: str
    source_publication_time: dt.datetime | None
    text: str


@dataclass(frozen=True)
class DigestCompositionUnit:
    unit_id: str
    rubric_id: str
    fact_ids: tuple[str, ...]
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    canonical_area_key: str
    priority: int
    allowed_relations: tuple[DigestFactRelation, ...] = ()
    estimated_character_cost: int = 0


@dataclass(frozen=True)
class DigestCandidateDisposition:
    candidate_id: str
    disposition: str
    reason: str
    priority: int
    estimated_character_cost: int
    selector_exclusion_suggestion: Any = None


@dataclass(frozen=True)
class DigestCompositionResult:
    units: tuple[DigestCompositionUnit, ...]
    relations: tuple[DigestFactRelation, ...]
    dispositions: tuple[DigestCandidateDisposition, ...]
    admitted_story_ids: frozenset[str]
    admitted_fact_ids: frozenset[str]
    estimated_visible_character_count: int
    fact_records: tuple[DigestFactRecord, ...] = ()
    composition_policy_version: str = COMPOSITION_POLICY_VERSION
    failure_reason: str = ""

    @property
    def feasible(self) -> bool:
        return not self.failure_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition_policy_version": self.composition_policy_version,
            "units": [
                {
                    "unit_id": u.unit_id,
                    "rubric_id": u.rubric_id,
                    "fact_ids": list(u.fact_ids),
                    "story_ids": list(u.story_ids),
                    "support_ids": list(u.support_ids),
                    "canonical_area_key": u.canonical_area_key,
                    "priority": u.priority,
                    "estimated_character_cost": u.estimated_character_cost,
                    "allowed_relations": [r.kind.value for r in u.allowed_relations],
                }
                for u in self.units
            ],
            "relations": [
                {
                    "left_fact_id": r.left_fact_id,
                    "right_fact_id": r.right_fact_id,
                    "kind": r.kind.value,
                    "reason": r.reason,
                }
                for r in self.relations
            ],
            "fact_records": [
                {
                    "fact_id": f.fact_id,
                    "story_ids": list(f.story_ids),
                    "support_ids": list(f.support_ids),
                    "rubric_id": f.rubric_id,
                    "canonical_subject": f.canonical_subject,
                    "canonical_service": f.canonical_service,
                    "canonical_area": f.canonical_area,
                    "canonical_place": list(f.canonical_place),
                    "original_location": f.original_location,
                    "effective_time": f.effective_time.isoformat() if f.effective_time else None,
                    "observed_time": f.observed_time.isoformat() if f.observed_time else None,
                    "service_state": f.service_state,
                    "epistemic_kind": f.epistemic_kind,
                    "source_publication_time": f.source_publication_time.isoformat()
                    if f.source_publication_time
                    else None,
                    "text": f.text,
                }
                for f in self.fact_records
            ],
            "dispositions": [
                {
                    "candidate_id": d.candidate_id,
                    "disposition": d.disposition,
                    "reason": d.reason,
                    "priority": d.priority,
                    "estimated_character_cost": d.estimated_character_cost,
                    "selector_exclusion_suggestion": d.selector_exclusion_suggestion,
                }
                for d in self.dispositions
            ],
            "admitted_story_ids": sorted(self.admitted_story_ids),
            "admitted_fact_ids": sorted(self.admitted_fact_ids),
            "estimated_visible_character_count": self.estimated_visible_character_count,
            "failure_reason": self.failure_reason,
        }

    def to_metadata_dict(self) -> dict[str, Any]:
        """Serialize composition provenance without copying evidence text."""
        return {
            "composition_policy_version": self.composition_policy_version,
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "rubric_id": unit.rubric_id,
                    "fact_ids": list(unit.fact_ids),
                    "story_ids": list(unit.story_ids),
                    "support_ids": list(unit.support_ids),
                    "canonical_area_key": unit.canonical_area_key,
                    "priority": unit.priority,
                    "estimated_character_cost": unit.estimated_character_cost,
                    "allowed_relations": [
                        relation.kind.value for relation in unit.allowed_relations
                    ],
                }
                for unit in self.units
            ],
            "relations": [
                {
                    "left_fact_id": relation.left_fact_id,
                    "right_fact_id": relation.right_fact_id,
                    "kind": relation.kind.value,
                }
                for relation in self.relations
            ],
            "dispositions": [
                {
                    "candidate_id": disposition.candidate_id,
                    "disposition": disposition.disposition,
                    "reason": disposition.reason,
                    "priority": disposition.priority,
                    "estimated_character_cost": disposition.estimated_character_cost,
                    "selector_exclusion_suggestion": (
                        {
                            "exclusion_reason": str(
                                disposition.selector_exclusion_suggestion.get(
                                    "exclusion_reason", ""
                                )
                            ),
                            "hard_label": bool(
                                disposition.selector_exclusion_suggestion.get("hard_label", False)
                            ),
                            "confidence": disposition.selector_exclusion_suggestion.get(
                                "confidence"
                            ),
                            "binding": False,
                        }
                        if isinstance(disposition.selector_exclusion_suggestion, Mapping)
                        else {"status": "unavailable", "binding": False}
                    ),
                }
                for disposition in self.dispositions
            ],
            "admitted_story_ids": sorted(self.admitted_story_ids),
            "admitted_fact_ids": sorted(self.admitted_fact_ids),
            "eligible_story_count": len(self.dispositions),
            "admitted_story_count": len(self.admitted_story_ids),
            "deferred_story_count": sum(
                disposition.disposition == "budget_deferred" for disposition in self.dispositions
            ),
            "estimated_visible_character_count": self.estimated_visible_character_count,
            "failure_reason": self.failure_reason,
        }


_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_EVENT_EVIDENCE_ID_RE = re.compile(r"story:(\d+):evidence:(\d+):frag:(\d+)")
_SERVICE_DOMAIN_IDS = frozenset(
    {
        "utilities",
        "municipal_service",
        "municipal_infrastructure",
        "banking",
        "telecom",
        "communications",
        "connectivity",
        "transport",
        "mobility",
        "civic_services",
        "infrastructure",
        "sewage",
        "sewer",
        "refuse",
        "communal",
        "safety",
        "security",
        "health",
    }
)
_SERVICE_DOMAIN_TERMS = (
    "utility",
    "communal",
    "electric",
    "power",
    "water",
    "gas",
    "heating",
    "transport",
    "transit",
    "connect",
    "telecom",
    "internet",
    "интернет",
    "banking",
    "municipal_service",
    "sewage",
    "sewer",
    "refuse",
    "waste",
    "garbage",
    "канализац",
    "водоотвед",
    "сточн",
    "мусор",
    "отход",
    "вывоз мусора",
    "municipal_infrastructure",
    "civic_service",
    "safety",
    "security",
    "health",
    "infrastrukt",
    "коммун",
    "электр",
    "энерг",
    "вод",
    "тепл",
    "транспорт",
    "связ",
    "банк",
    "муницип",
    "безопас",
    "здоров",
)
_RUBRIC_BY_SERVICE_OR_CATEGORY = {
    "connectivity": "communications",
    "telecom": "communications",
    "communications": "communications",
    "transport": "mobility",
    "mobility": "mobility",
    "utilities": "infrastructure",
    "communal": "infrastructure",
    "electricity": "infrastructure",
    "power": "infrastructure",
    "water": "infrastructure",
    "gas": "infrastructure",
    "heating": "infrastructure",
    "municipal_infrastructure": "infrastructure",
    "infrastructure": "infrastructure",
    "sewage": "infrastructure",
    "sewer": "infrastructure",
    "refuse": "infrastructure",
    "waste": "infrastructure",
    "garbage": "infrastructure",
    "канализация": "infrastructure",
    "водоотведение": "infrastructure",
    "сточные_воды": "infrastructure",
    "мусор": "infrastructure",
    "отходы": "infrastructure",
    "вывоз_мусора": "infrastructure",
    "banking": "civic_services",
    "municipal_service": "civic_services",
    "municipal_services": "civic_services",
    "civic_service": "civic_services",
    "civic_services": "civic_services",
    "health": "health",
    "medical": "health",
    "safety": "safety",
    "security": "safety",
}


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").casefold()))


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _service_domain_signal(card: Any) -> bool:
    raw_values = [
        getattr(card, "rubric_id", ""),
        getattr(card, "category", ""),
        getattr(card, "story_kind", ""),
        getattr(card, "topic", ""),
        *(getattr(card, "tags", ()) or ()),
    ]
    normalized = [
        str(value).strip().casefold().replace("-", "_").replace(" ", "_") for value in raw_values
    ]
    explicit_service_category = any(value in _SERVICE_DOMAIN_IDS for value in normalized)
    commercial_categories = {
        "economy",
        "retail",
        "commerce",
        "commercial_offer",
        "classified",
        "advertisement",
        "marketplace",
    }
    commercial_terms = (
        "магазин",
        "магазины",
        "торговля",
        "продажа",
        "объявление",
        "объявления",
        "retail",
        "store",
        "shop",
        "sale",
        "classified",
        "advertisement",
        "marketplace",
    )
    is_commercial = any(value in commercial_categories for value in normalized) or any(
        term in " ".join(normalized) for term in commercial_terms
    )
    if is_commercial and not explicit_service_category:
        return False
    return (
        explicit_service_category
        or any(term in " ".join(normalized) for term in _SERVICE_DOMAIN_TERMS)
        or str(getattr(card, "story_kind", "")).casefold() == "operational_status"
    )


def _stable_id(prefix: str, members: tuple[str, ...]) -> str:
    digest = hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _fact_records(
    plan: DigestPresentationPlan,
    cards: tuple[Any, ...],
    evidence: Any,
    edition_slug: str,
    snapshot_at: dt.datetime | None,
    rubric_labels: Mapping[str, str] | None,
    rubric_fallback_id: str,
) -> tuple[DigestFactRecord, ...]:
    from src.publication.digest_presentation import (
        _canonical_service_family,
        _load_digest_geography_resolver,
    )

    resolver = _load_digest_geography_resolver(edition_slug)
    selected_story_ids = set(plan.story_ids)
    card_by_id = {
        str(getattr(c, "id", "")): c
        for c in cards
        if str(getattr(c, "id", "")) in selected_story_ids
    }
    evidence_map = evidence if isinstance(evidence, Mapping) else {}
    # A StoryCard ID is a synthetic coverage reference, not source evidence.
    # Canonical composition writer input accepts only exact PUBLISH evidence
    # aliases; strip card IDs from fact supports unless that exact string is
    # also a real PUBLISH evidence alias.
    publish_support_ids: set[str] = set()
    for evidence_id, item in evidence_map.items():
        if getattr(item, "publication_use", "") != "PUBLISH":
            continue
        text = str(getattr(item, "text", "") or getattr(item, "source_text", "") or "").strip()
        if not text:
            continue
        publish_support_ids.add(str(evidence_id))
        for name in ("evidence_id", "source_ref"):
            value = str(getattr(item, name, "") or "").strip()
            if value:
                publish_support_ids.add(value)
        fragment_id = str(getattr(item, "fragment_id", "") or "").strip()
        if fragment_id:
            publish_support_ids.add(f"fragment:{fragment_id}")
    output: list[DigestFactRecord] = []
    represented_by_story: dict[str, list[RequiredDigestFact]] = {}
    for fact in plan.required_facts:
        for sid in fact.story_ids:
            represented_by_story.setdefault(sid, []).append(fact)
        story_ids = tuple(str(sid) for sid in fact.story_ids)
        story_id_set = set(story_ids)
        fact_support_ids = tuple(
            str(support_id)
            for support_id in fact.support_ids
            if str(support_id) not in story_id_set or str(support_id) in publish_support_ids
        )
        location, state, effective, observed, kind, published = (
            fact.original_location,
            fact.service_state,
            _parse_time(fact.effective_at),
            _parse_time(fact.observed_at),
            fact.epistemic_kind,
            _parse_time(fact.source_published_at),
        )
        service = fact.subject_key.casefold()
        _append_fact_record(
            output,
            fact_id=fact.fact_id,
            story_ids=story_ids,
            support_ids=fact_support_ids,
            rubric_id=fact.rubric_id,
            subject=fact.subject_key,
            service=service,
            location=location,
            effective=effective,
            observed=observed,
            state=state,
            kind=kind,
            published=published,
            text=fact.text,
            resolver=resolver,
            snapshot_at=snapshot_at,
            resolve_text_location=True,
        )

    # Group per-fragment adapter records by their explicit payload evidence-item
    # ordinal. The ordinal is frozen in the evidence ID; shared fragment IDs
    # alone never merge separate payload items.
    for sid, card in card_by_id.items():
        story_num = sid.removeprefix("story:")
        story_facts = represented_by_story.get(sid, [])
        logical_evidence: dict[tuple[str, str, str], list[Any]] = {}
        for ev in evidence_map.values():
            if getattr(ev, "publication_use", "") != "PUBLISH":
                continue
            if str(getattr(ev, "story_id", "")) != story_num:
                continue
            ev_id = str(getattr(ev, "evidence_id", ""))
            ev_text = str(getattr(ev, "text", "") or "").strip()
            if not ev_id or not ev_text:
                continue
            id_match = _EVENT_EVIDENCE_ID_RE.fullmatch(ev_id)
            if id_match and id_match.group(1) == story_num:
                logical_key = f"payload-item:{id_match.group(2)}"
            else:
                logical_key = f"evidence-id:{ev_id}"
            # Exact text/kind partition protects against malformed frozen rows
            # that reuse an item ordinal with inconsistent payload values.
            group_key = (logical_key, ev_text, str(getattr(ev, "kind", "") or ""))
            logical_evidence.setdefault(group_key, []).append(ev)

        for (logical_key, ev_text, kind), items in logical_evidence.items():
            evidence_ids = tuple(
                dict.fromkeys(str(getattr(item, "evidence_id", "")) for item in items)
            )
            fragment_ids = tuple(
                dict.fromkeys(str(getattr(item, "fragment_id", "")) for item in items)
            )
            source_refs = tuple(
                dict.fromkeys(
                    str(getattr(item, "source_ref", ""))
                    for item in items
                    if getattr(item, "source_ref", "")
                )
            )
            support_ids = tuple(
                dict.fromkeys(
                    (
                        *evidence_ids,
                        *(f"fragment:{fid}" for fid in fragment_ids if fid),
                        *source_refs,
                    )
                )
            )
            matched_facts = [
                fact for fact in story_facts if set(evidence_ids) & set(fact.support_ids)
            ]
            if not matched_facts:
                # Some frozen RequiredDigestFacts predate the adapter's
                # generated evidence IDs and carry only `fragment:<id>`.
                # Link exact text (optionally with the plan's verbatim location
                # prefix) only with same-story provenance and one matching fact.
                item_provenance = {
                    *source_refs,
                    *(f"fragment:{fid}" for fid in fragment_ids if fid),
                }
                exact_claim_matches = [
                    fact
                    for fact in story_facts
                    if (
                        fact.text.strip() == ev_text
                        or (
                            fact.original_location.strip()
                            and fact.text.strip() == f"{fact.original_location.strip()}: {ev_text}"
                        )
                    )
                    and item_provenance.intersection(fact.support_ids)
                ]
                if len(exact_claim_matches) == 1:
                    matched_facts = exact_claim_matches
            if matched_facts:
                # Exact logical IDs or the conservative content/provenance
                # fallback bind this item to the required fact. Preserve each
                # per-fragment evidence, fragment, and source-ref support.
                extra_supports = set(support_ids)
                output = [
                    replace(
                        record,
                        support_ids=tuple(dict.fromkeys((*record.support_ids, *extra_supports))),
                    )
                    if record.fact_id in {fact.fact_id for fact in matched_facts}
                    else record
                    for record in output
                ]
                continue

            item_index_match = (
                _EVENT_EVIDENCE_ID_RE.fullmatch(evidence_ids[0]) if evidence_ids else None
            )
            logical_suffix = item_index_match.group(2) if item_index_match else logical_key
            payload_variant = hashlib.sha256(f"{ev_text}|{kind}".encode()).hexdigest()[:8]
            fact_suffix = f"{logical_suffix}:{payload_variant}"
            fact_id = f"evidence-item:{sid}:{fact_suffix}"
            item_times = {
                _parse_time(getattr(item, "observed_at", None))
                for item in items
                if _parse_time(getattr(item, "observed_at", None)) is not None
            }
            observed = next(iter(item_times)) if len(item_times) == 1 else None
            explicit_card_rubric = str(getattr(card, "rubric_id", "") or "").strip()
            required_fact_rubric = story_facts[0].rubric_id if story_facts else ""
            configured_rubric_ids = set(rubric_labels or {})
            card_rubric = explicit_card_rubric
            if card_rubric and configured_rubric_ids and card_rubric not in configured_rubric_ids:
                card_rubric = ""
            if not card_rubric:
                category_values = [
                    getattr(card, "category", ""),
                    getattr(card, "story_kind", ""),
                    getattr(card, "topic", ""),
                    *(getattr(card, "tags", ()) or ()),
                ]
                category_keys = {
                    str(value).strip().casefold().replace("-", "_").replace(" ", "_")
                    for value in category_values
                    if str(value).strip()
                }
                service_family = _canonical_service_family(card)
                mapping_keys = ([service_family] if service_family else []) + sorted(category_keys)
                mapped_rubrics = [
                    _RUBRIC_BY_SERVICE_OR_CATEGORY[key]
                    for key in mapping_keys
                    if key in _RUBRIC_BY_SERVICE_OR_CATEGORY
                ]
                if configured_rubric_ids:
                    card_rubric = next(
                        (rubric for rubric in mapped_rubrics if rubric in configured_rubric_ids),
                        "",
                    )
                if (
                    not card_rubric
                    and required_fact_rubric
                    and (not configured_rubric_ids or required_fact_rubric in configured_rubric_ids)
                ):
                    card_rubric = required_fact_rubric
                if not card_rubric:
                    card_rubric = (
                        rubric_fallback_id
                        if not configured_rubric_ids or rubric_fallback_id in configured_rubric_ids
                        else sorted(configured_rubric_ids)[0]
                    )
            # PublicationEvidence carries no structured location, service state,
            # effective time, or source publication time. Keep them unknown.
            _append_fact_record(
                output,
                fact_id=fact_id,
                story_ids=(sid,),
                support_ids=support_ids,
                rubric_id=card_rubric,
                subject="",
                service="",
                location="",
                effective=None,
                observed=observed,
                state="",
                kind=kind,
                published=None,
                text=ev_text,
                resolver=resolver,
                snapshot_at=snapshot_at,
                resolve_text_location=False,
            )
    return tuple(output)


def _append_fact_record(
    output: list[DigestFactRecord],
    *,
    fact_id: str,
    story_ids: tuple[str, ...],
    support_ids: tuple[str, ...],
    rubric_id: str,
    subject: str,
    service: str,
    location: str,
    effective: dt.datetime | None,
    observed: dt.datetime | None,
    state: str,
    kind: str,
    published: dt.datetime | None,
    text: str,
    resolver: Any,
    snapshot_at: dt.datetime | None,
    resolve_text_location: bool,
) -> None:
    from src.publication.digest_presentation import _resolved_geographic_scopes

    scopes = (
        _resolved_geographic_scopes(location, resolver) if resolver is not None and location else {}
    )
    if not scopes and resolver is not None and not location and resolve_text_location:
        scopes = _resolved_geographic_scopes(text, resolver)
    if len(scopes) == 1:
        area = next(iter(scopes))
    else:
        # Unresolved and ambiguous locations remain fact-specific. Matching raw
        # wording is not sufficient to assert canonical-area identity.
        area = f"unknown:{fact_id}"
    place_identity: tuple[str, ...] = ()
    if resolver is not None:
        place_text = " ".join(part for part in (location, text) if part).strip()
        if place_text:
            annotation = resolver.resolve(place_text)
            place_identity = tuple(
                sorted(
                    {
                        str(entity.entity_id)
                        for entity in getattr(annotation, "entities", ())
                        if getattr(entity, "kind", "") == "place"
                        and getattr(entity, "confidence", "") == "high"
                        and getattr(entity, "entity_id", "")
                    }
                )
            )
    observed = (
        observed if observed is None or snapshot_at is None or observed <= snapshot_at else None
    )
    published = (
        published if published is None or snapshot_at is None or published <= snapshot_at else None
    )
    output.append(
        DigestFactRecord(
            fact_id=fact_id,
            story_ids=story_ids,
            support_ids=support_ids,
            rubric_id=rubric_id,
            canonical_subject=subject.casefold(),
            canonical_service=service.casefold(),
            canonical_area=area,
            canonical_place=place_identity,
            original_location=location,
            effective_time=effective,
            observed_time=observed,
            service_state=state.upper(),
            epistemic_kind=kind,
            source_publication_time=published,
            text=text,
        )
    )


def _classify(left: DigestFactRecord, right: DigestFactRecord) -> DigestFactRelationKind:
    same_topic = (
        left.canonical_subject == right.canonical_subject
        and left.canonical_service == right.canonical_service
    )
    same_area = left.canonical_area == right.canonical_area and not left.canonical_area.startswith(
        "unknown:"
    )
    effective_time_equal = (
        left.effective_time is not None
        and right.effective_time is not None
        and left.effective_time == right.effective_time
    )
    observed_time_equal = (
        left.effective_time is None
        and right.effective_time is None
        and left.observed_time is not None
        and left.observed_time == right.observed_time
    )
    time_ordered = left.effective_time is not None and right.effective_time is not None
    left_words, right_words = _words(left.text), _words(right.text)
    match = (
        bool(left_words and right_words)
        and len(left_words & right_words) / max(len(left_words | right_words), 1) >= 0.88
    )
    # Parent-area identity and raw wording are insufficient deletion keys.
    # Require a profile-resolved physical entity on both facts; this also
    # blocks same-parent-area claims whose street names are unrecognized.
    same_physical_place = (
        bool(left.canonical_place) and left.canonical_place == right.canonical_place
    )
    if (
        same_topic
        and same_area
        and same_physical_place
        and left.service_state == right.service_state
        and effective_time_equal
        and match
    ):
        return DigestFactRelationKind.SAME_FACT
    if same_topic and same_area and left.service_state != right.service_state:
        if time_ordered and left.effective_time != right.effective_time:
            return DigestFactRelationKind.UPDATE_OF
        if effective_time_equal or observed_time_equal:
            return DigestFactRelationKind.LOCAL_CONTRAST
    return DigestFactRelationKind.RELATED_ONLY


_CORE_SERVICE_FACT_CATEGORIES = frozenset(
    {
        "banking",
        "civic_service",
        "civic_services",
        "communications",
        "connectivity",
        "electricity",
        "gas",
        "health",
        "heating",
        "mobility",
        "municipal_service",
        "municipal_services",
        "power",
        "safety",
        "security",
        "telecom",
        "transport",
        "water",
    }
)
_CORE_SERVICE_FACT_TERMS = (
    "electric",
    "electricity",
    "power",
    "water",
    "heat",
    "heating",
    "gas",
    "connectivity",
    "telecom",
    "internet",
    "wifi",
    "transport",
    "transit",
    "bus",
    "tram",
    "trolleybus",
    "bank",
    "pension",
    "municipal",
    "hospital",
    "clinic",
    "safety",
    "security",
    "strike",
    "свет",
    "света",
    "свете",
    "свету",
    "светом",
    "электричество",
    "электричества",
    "электричестве",
    "электричеству",
    "электричеством",
    "энергия",
    "энергии",
    "энергию",
    "энергией",
    "вода",
    "воды",
    "воде",
    "воду",
    "водой",
    "водою",
    "тепло",
    "тепла",
    "тепле",
    "теплом",
    "газ",
    "газа",
    "газу",
    "газом",
    "газе",
    "связь",
    "связи",
    "связью",
    "интернет",
    "интернета",
    "интернету",
    "интернетом",
    "интернете",
    "вайфай",
    "вайфая",
    "вайфаю",
    "вайфаем",
    "банк",
    "банка",
    "банке",
    "банку",
    "банком",
    "банки",
    "банков",
)
_CORE_SERVICE_FACT_PREFIXES = (
    "электроснабж",
    "электросет",
    "электроэнерг",
    "водоснабж",
    "водопровод",
    "теплоснабж",
    "отоплен",
    "интернет",
    "газоснабж",
    "газопровод",
    "автобус",
    "трамва",
    "троллейбус",
    "маршрут",
    "пенсион",
    "муниципаль",
    "больниц",
    "поликлиник",
)
_CORE_SAFETY_FACT_TERMS = (
    "safety",
    "security",
    "strike",
)
_CORE_SAFETY_FACT_PREFIXES = ("обстрел", "пожар", "взрыв", "эвакуац")


def _matches_fact_signal(text: str, words: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    tokens = _words(normalized)
    return (
        bool(tokens.intersection(words))
        or any(token.startswith(prefix) for token in tokens for prefix in prefixes)
        or ("wifi" in words and bool(re.search(r"\bwi[\s-]?fi\b", normalized)))
    )


def _fact_signal_text(fact: DigestFactRecord | None) -> str:
    if fact is None or fact.epistemic_kind.casefold() in {"question", "resident_question"}:
        return ""
    return " ".join((fact.canonical_service, fact.text)).casefold()


def _core_service_fact_signal(fact: DigestFactRecord | None) -> bool:
    if fact is None:
        return False
    signal = _fact_signal_text(fact)
    if not signal:
        return False
    category_ids = {
        value.strip().casefold().replace("-", "_").replace(" ", "_")
        for value in (fact.canonical_service,)
    }
    return bool(category_ids & _CORE_SERVICE_FACT_CATEGORIES) or _matches_fact_signal(
        signal, _CORE_SERVICE_FACT_TERMS, _CORE_SERVICE_FACT_PREFIXES
    )


def _core_service_domains(fact: DigestFactRecord) -> set[str]:
    """Presentation breadth within core services, based only on grounded facts."""
    if not _core_service_fact_signal(fact):
        return set()
    signal = _fact_signal_text(fact)
    domain_signals = {
        "power": (
            ("power", "electricity", "свет", "света", "свете", "свету", "светом"),
            ("electric", "электричество", "электроснабж", "электросет", "электроэнерг"),
        ),
        "water": (
            ("water", "вода", "воды", "воде", "воду", "водой", "водою"),
            ("water_", "водоснабж", "водопровод"),
        ),
        "gas": (
            ("gas", "газ", "газа", "газу", "газом", "газе"),
            ("gas_", "газоснабж", "газопровод"),
        ),
        "heating": (
            ("heat", "heating", "тепло", "тепла", "тепле", "теплом"),
            ("heating_", "теплоснабж", "отоплен"),
        ),
        "connectivity": (
            ("connectivity", "telecom", "wifi", "связь", "связи", "связью"),
            ("connectivity_", "telecom_", "internet", "интернет", "вайфай"),
        ),
        "transport": (
            ("transport", "mobility", "transit", "bus", "tram", "trolleybus", "такси"),
            ("transport_", "urban_transport", "автобус", "трамва", "троллейбус", "маршрут"),
        ),
        "safety": (_CORE_SAFETY_FACT_TERMS, _CORE_SAFETY_FACT_PREFIXES),
        "health": (("health", "hospital", "clinic"), ("больниц", "поликлиник")),
        "civic": (
            ("bank", "banking", "pension", "municipal", "civic_services"),
            ("банк", "пенсион", "муниципаль", "civic_service"),
        ),
    }
    # A hospital used as a landmark for a power outage does not establish a
    # health-service update. Prefer a known service; use claim text otherwise.
    service_domains = {
        domain
        for domain, (words, prefixes) in domain_signals.items()
        if _matches_fact_signal(fact.canonical_service, words, prefixes)
    }
    if service_domains:
        return service_domains
    domains = {
        domain
        for domain, (words, prefixes) in domain_signals.items()
        if _matches_fact_signal(signal, words, prefixes)
    }
    return domains or {"core_other"}


def _priority(card: Any, fact: DigestFactRecord | None) -> int:
    raw_importance = getattr(card, "importance", "medium")
    if isinstance(raw_importance, (int, float)) and not isinstance(raw_importance, bool):
        numeric_importance = float(raw_importance)
        importance = (
            int(numeric_importance * 50)
            if numeric_importance <= 1
            else int(numeric_importance * 10)
            if numeric_importance <= 10
            else int(numeric_importance)
        )
    else:
        raw = str(raw_importance).strip().casefold()
        importance = {
            "critical": 50,
            "urgent": 45,
            "high": 40,
            "medium": 25,
            "normal": 20,
            "low": 10,
        }.get(raw, 15)
    rubric = str(getattr(card, "rubric_id", "") or "").casefold()
    signals = " ".join(
        str(value).casefold()
        for value in (
            rubric,
            getattr(card, "story_kind", ""),
            getattr(card, "topic", ""),
            getattr(card, "category", ""),
            " ".join(str(t) for t in (getattr(card, "tags", ()) or ())),
        )
    )
    fact_signal = _fact_signal_text(fact)
    safety_signal = _matches_fact_signal(
        f"{signals} {fact_signal}", _CORE_SAFETY_FACT_TERMS, _CORE_SAFETY_FACT_PREFIXES
    )
    core_service_signal = _core_service_fact_signal(fact)
    service_signal = _service_domain_signal(card) or core_service_signal
    urgency = 32 if safety_signal else 24 if core_service_signal else 14 if service_signal else 0
    current = 8 if fact and (fact.effective_time or fact.observed_time) else 0
    return importance + urgency + current


def _is_essential(card: Any, unit_priority: int) -> bool:
    raw = getattr(card, "importance", "medium")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        importance = (
            float(raw) * 5
            if float(raw) <= 1
            else float(raw) / 10
            if float(raw) > 10
            else float(raw)
        )
    else:
        importance = {
            "critical": 5,
            "urgent": 4.5,
            "high": 4,
            "medium": 2.5,
            "normal": 2,
            "low": 1,
        }.get(str(raw).strip().casefold(), 2)
    if importance >= 4.5:
        return True
    # High importance and service-domain signals still rank early through
    # `_priority`, but they are not individually non-negotiable: several
    # independent utility reports can exceed a single post even when each is
    # useful. Only urgent/critical facts should make the entire digest fail
    # closed when they cannot fit.
    return False


def build_digest_composition(
    plan: DigestPresentationPlan,
    cards: Any,
    evidence: Any,
    *,
    edition_slug: str,
    snapshot_at: dt.datetime | None,
    max_chars: int,
    reserved_chars: int,
    include_statistics: bool,
    selector_suggestions: dict[str, Any] | None = None,
    rubric_labels: dict[str, str] | None = None,
    rubric_fallback_id: str = "other",
) -> DigestCompositionResult:
    """Build exact fact relations and choose a deterministic, feasible pre-write digest plan."""
    validate_digest_fact_ids(
        plan.required_facts,
        error_code="DIGEST_DUPLICATE_REQUIRED_FACT_ID",
    )
    card_list = tuple(cards or ())
    by_id = {str(getattr(c, "id", "")): c for c in card_list}
    records = _fact_records(
        plan,
        card_list,
        evidence,
        edition_slug,
        snapshot_at,
        rubric_labels=rubric_labels,
        rubric_fallback_id=rubric_fallback_id,
    )
    validate_digest_fact_ids(
        records,
        error_code="DIGEST_DUPLICATE_COMPOSITION_FACT_ID",
    )
    relations: list[DigestFactRelation] = []
    for i, left in enumerate(records):
        for right in records[i + 1 :]:
            kind = _classify(left, right)
            relations.append(
                DigestFactRelation(
                    left.fact_id,
                    right.fact_id,
                    kind,
                    "compatible fact metadata"
                    if kind == DigestFactRelationKind.SAME_FACT
                    else "preserve distinct claim",
                )
            )

    groups: dict[tuple[str, str, str], list[DigestFactRecord]] = {}
    for record in records:
        # Composition units keep distinct propositions as separate fact records
        # while placing same-area/service material together for coherent writing.
        key = (record.rubric_id, record.canonical_area, record.canonical_service)
        groups.setdefault(key, []).append(record)

    stories_with_records = {sid for rec in records for sid in rec.story_ids}
    story_placeholder_keys: list[tuple[str, str]] = []
    for sid in sorted(set(plan.story_ids) - stories_with_records):
        card = by_id.get(sid)
        if card is None:
            story_placeholder_keys.append((sid, ""))
        else:
            story_placeholder_keys.append((sid, str(getattr(card, "summary", "") or "")))

    all_units: list[DigestCompositionUnit] = []
    for (rubric, area, _service), members in sorted(groups.items()):
        fact_ids = tuple(dict.fromkeys(r.fact_id for r in members))
        story_ids = tuple(dict.fromkeys(sid for r in members for sid in r.story_ids))
        supports = tuple(dict.fromkeys(sid for r in members for sid in r.support_ids))
        card = next((by_id[sid] for sid in story_ids if sid in by_id), None)
        priority = max(
            (
                _priority(by_id[sid], next((r for r in members if sid in r.story_ids), None))
                for sid in story_ids
                if sid in by_id
            ),
            default=_priority(card, members[0] if members else None) if card else 10,
        )
        # Include full source-backed fact text plus a 54-character per-fact
        # allowance for attribution/labels/separators and a 24-character unit
        # allowance for item formatting. Never clip long facts in estimates.
        content_cost = sum(len(r.text) + 54 for r in members) + 24
        uid = _stable_id("composition", fact_ids or story_ids)
        rels = tuple(
            r for r in relations if r.left_fact_id in fact_ids and r.right_fact_id in fact_ids
        )
        all_units.append(
            DigestCompositionUnit(
                uid, rubric, fact_ids, story_ids, supports, area, priority, rels, content_cost
            )
        )

    for sid, summary in story_placeholder_keys:
        card = by_id.get(sid)
        if card is not None:
            all_refs = getattr(card, "all_source_refs", None)
            raw_refs = (
                all_refs()
                if callable(all_refs)
                else getattr(card, "representative_source_refs", ()) or ()
            )
            refs = tuple(dict.fromkeys(str(ref) for ref in raw_refs if str(ref).strip()))
        else:
            refs = ()
        rubric = str(getattr(card, "rubric_id", "") or "other") if card is not None else "other"
        priority = _priority(card, None) if card is not None else 15
        # This is a story-level budget unit, not a new factual record: it uses
        # only the existing summary/supports and does not fabricate a claim.
        summary_cost = len(summary) + 54 + 24 if summary else 0
        all_units.append(
            DigestCompositionUnit(
                _stable_id("story", (sid,)),
                rubric,
                (),
                (sid,),
                refs,
                f"unknown:{sid}",
                priority,
                (),
                summary_cost,
            )
        )

    # The writer targets 2500–3700 characters, while the single-post
    # composition budget leaves 196 characters below Telegram's hard ceiling
    # for estimation variance. `reserved_chars` covers the title only; rubric
    # headings are charged once when their rubric is first admitted.
    target_budget = min(max(0, max_chars), 3900, 4096)
    stat_cost = 0 if not include_statistics else 180
    available = max(0, target_budget - max(0, reserved_chars) - stat_cost)
    sorted_units = sorted(
        all_units, key=lambda u: (-u.priority, u.rubric_id, u.canonical_area_key, u.unit_id)
    )
    first_by_rubric: dict[str, DigestCompositionUnit] = {}
    remaining_units: list[DigestCompositionUnit] = []
    for unit in sorted_units:
        if unit.rubric_id not in first_by_rubric:
            first_by_rubric[unit.rubric_id] = unit
        else:
            remaining_units.append(unit)
    # Admit the best unit from each domain first where budget permits, then use
    # remaining space by priority. This rewards breadth without imposing a quota.
    ranked = sorted(first_by_rubric.values(), key=lambda u: (-u.priority, u.rubric_id, u.unit_id))
    ranked.extend(remaining_units)
    # Unit text/item allowance is already in unit cost; heading labels and
    # spacing are charged exactly once per admitted rubric here.
    label_map = rubric_labels or {}
    heading_cost = {
        rubric: (
            len(str(label_map[rubric]).strip()) + 2
            if rubric in label_map and str(label_map[rubric]).strip()
            else 96
        )
        for rubric in {u.rubric_id for u in ranked}
    }
    # Candidate-connected components make admission atomic for a Story. If a
    # same-fact unit is shared by Stories, they remain an atomic component too.
    parent = list(range(len(ranked)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    story_owner: dict[str, int] = {}
    for index, unit in enumerate(ranked):
        for sid in unit.story_ids:
            if sid in story_owner:
                a, b = root(index), root(story_owner[sid])
                if a != b:
                    parent[b] = a
            else:
                story_owner[sid] = index
    packages: dict[int, list[DigestCompositionUnit]] = {}
    for index, unit in enumerate(ranked):
        packages.setdefault(root(index), []).append(unit)
    package_list = list(packages.values())
    package_list.sort(
        key=lambda package: (
            -max(u.priority for u in package),
            min((u.rubric_id for u in package), default=""),
            min((u.unit_id for u in package), default=""),
        )
    )
    record_by_id = {record.fact_id: record for record in records}
    essential_packages: list[list[DigestCompositionUnit]] = []
    core_service_packages: list[list[DigestCompositionUnit]] = []
    other_packages: list[list[DigestCompositionUnit]] = []
    for package in package_list:
        if any(
            _is_essential(by_id.get(story_id), unit.priority)
            for unit in package
            for story_id in unit.story_ids
        ):
            essential_packages.append(package)
            continue
        has_core_service_fact = any(
            _core_service_fact_signal(record_by_id.get(fact_id))
            for unit in package
            for fact_id in unit.fact_ids
        )
        (core_service_packages if has_core_service_fact else other_packages).append(package)
    # Preserve breadth for remaining material, after grounded core-service
    # packages have had a chance to use the budget.
    seen_rubrics: set[str] = set()
    first_packages: list[list[DigestCompositionUnit]] = []
    rest_packages: list[list[DigestCompositionUnit]] = []
    for package in other_packages:
        rubric = min((u.rubric_id for u in package), default="")
        if rubric not in seen_rubrics:
            seen_rubrics.add(rubric)
            first_packages.append(package)
        else:
            rest_packages.append(package)
    admitted_packages: list[list[DigestCompositionUnit]] = []
    deferred_packages: list[list[DigestCompositionUnit]] = []
    used = 0
    charged_rubrics: set[str] = set()

    def admit(package: list[DigestCompositionUnit]) -> bool:
        nonlocal used
        package_rubrics = {u.rubric_id for u in package}
        marginal = sum(u.estimated_character_cost for u in package)
        marginal += sum(heading_cost[r] for r in package_rubrics - charged_rubrics)
        if used + marginal <= available:
            admitted_packages.append(package)
            charged_rubrics.update(package_rubrics)
            used += marginal
            return True
        return False

    # Offer distinct supported service domains space before repeating power (or
    # any other domain). An oversized package cannot reserve a domain or prevent
    # a later feasible package from representing it. Story admission stays atomic.
    covered_core_domains: set[str] = set()
    repeat_core_packages: list[list[DigestCompositionUnit]] = []
    for package in essential_packages + core_service_packages:
        domains = set().union(
            *(
                _core_service_domains(record_by_id[fact_id])
                for unit in package
                for fact_id in unit.fact_ids
            )
        )
        essential = package in essential_packages
        if (essential or domains - covered_core_domains) and admit(package):
            covered_core_domains.update(domains)
        elif essential:
            deferred_packages.append(package)
        else:
            repeat_core_packages.append(package)
    for package in repeat_core_packages + first_packages + rest_packages:
        if not admit(package):
            deferred_packages.append(package)

    admitted_units = [unit for package in admitted_packages for unit in package]
    deferred_units = [unit for package in deferred_packages for unit in package]

    dispositions: list[DigestCandidateDisposition] = []
    admitted_story_ids = {sid for u in admitted_units for sid in u.story_ids}
    admitted_fact_ids = {fid for u in admitted_units for fid in u.fact_ids}
    selector_suggestions = selector_suggestions or {}
    for card in card_list:
        sid = str(getattr(card, "id", ""))
        if sid not in plan.story_ids:
            continue
        candidate_units = [u for u in all_units if sid in u.story_ids]
        cost = sum(u.estimated_character_cost for u in candidate_units)
        priority = max((u.priority for u in candidate_units), default=_priority(card, None))
        admitted_unit_ids = {unit.unit_id for unit in admitted_units}
        selected = bool(candidate_units) and all(
            unit.unit_id in admitted_unit_ids for unit in candidate_units
        )
        dispositions.append(
            DigestCandidateDisposition(
                sid,
                "selected" if selected else "budget_deferred",
                "admitted within deterministic digest budget"
                if selected
                else "digest character budget; lower priority after reserving title, headings, separators and statistics",
                priority,
                cost,
                selector_suggestions.get(sid, {"status": "unavailable"}),
            )
        )

    # Keep the plan membership and disposition sets identical, including any
    # compatibility card missing from the supplied card sequence.
    disposed_ids = {d.candidate_id for d in dispositions}
    for sid in plan.story_ids:
        if sid in disposed_ids:
            continue
        matching = [u for u in all_units if sid in u.story_ids]
        selected = bool(matching) and all(u in admitted_units for u in matching)
        dispositions.append(
            DigestCandidateDisposition(
                sid,
                "selected" if selected else "budget_deferred",
                "admitted within deterministic digest budget"
                if selected
                else "digest budget or unavailable candidate metadata",
                max((u.priority for u in matching), default=15),
                sum(u.estimated_character_cost for u in matching),
                selector_suggestions.get(sid, {"status": "unavailable"}),
            )
        )
        if selected:
            admitted_story_ids.add(sid)

    essential_deferred = [
        u
        for u in deferred_units
        if any(_is_essential(by_id.get(sid), u.priority) for sid in u.story_ids)
    ]
    failure = "ESSENTIAL_DIGEST_FACTS_EXCEED_BUDGET" if essential_deferred else ""
    return DigestCompositionResult(
        units=tuple(admitted_units),
        relations=tuple(relations),
        dispositions=tuple(dispositions),
        admitted_story_ids=frozenset(admitted_story_ids),
        admitted_fact_ids=frozenset(admitted_fact_ids),
        estimated_visible_character_count=used + reserved_chars + stat_cost,
        fact_records=records,
        failure_reason=failure,
    )
