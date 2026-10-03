"""Digest presentation planning for thematic city-life short-read digests."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
)
from src.publication.errors import DigestCoverageInvariantError


def validate_digest_fact_ids(facts: Sequence[Any], *, error_code: str) -> None:
    """Fail before fact-ID keyed processing can silently discard duplicate records."""
    seen: set[str] = set()
    for fact in facts:
        raw_fact_id = (
            fact.get("fact_id", "") if isinstance(fact, Mapping) else getattr(fact, "fact_id", "")
        )
        fact_id = str(raw_fact_id or "").strip()
        if not fact_id:
            raise DigestCoverageInvariantError(f"{error_code}:empty_fact_id")
        if fact_id in seen:
            safe_id = repr(fact_id[:120])
            raise DigestCoverageInvariantError(f"{error_code}:duplicate_fact_id:{safe_id}")
        seen.add(fact_id)


DigestPresentationUnitKind = Literal["SYNTHESIS", "NORMAL", "BRIEF_ROLLUP"]

_DIGEST_CITYWIDE_SCOPE_RE = re.compile(
    r"(?:весь город|всего города|по всему городу|во вс[её]м городе|"
    r"город целиком|городские районы|по городу в целом)",
    re.IGNORECASE,
)


class DigestPresentationMode(str, Enum):
    DASHBOARD_ONLY = "DASHBOARD_ONLY"
    DETAIL_ONLY = "DETAIL_ONLY"
    DASHBOARD_AND_DRILLDOWN = "DASHBOARD_AND_DRILLDOWN"


def city_situation_group_reader_text(group: Any) -> str:
    return getattr(group, "reader_text", "") or ""


@dataclass(frozen=True)
class DigestPresentationUnit:
    """Deterministic presentation compression unit grouping related stories into scan-first items."""

    unit_id: str
    rubric_id: str
    kind: DigestPresentationUnitKind
    story_ids: tuple[str, ...]
    support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...]
    min_rank: int
    compression_key: str


@dataclass(frozen=True)
class TopicGeographicFact:
    """A source-backed fact tied to one canonical area inside a topic bundle."""

    story_id: str
    text: str
    support_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "text": self.text,
            "support_ids": list(self.support_ids),
        }


@dataclass(frozen=True)
class TopicGeographicGroup:
    """Stories resolved to the same edition-profile area for digest composition."""

    area_id: str
    area_name: str
    story_ids: tuple[str, ...]
    facts: tuple[TopicGeographicFact, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "area_name": self.area_name,
            "story_ids": list(self.story_ids),
            "facts": [fact.to_dict() for fact in self.facts],
        }


@dataclass(frozen=True)
class TopicBundle:
    """Thematic aggregate of related stories within a rubric."""

    bundle_id: str
    rubric_id: str
    topic_key: str
    topic_label: str
    emoji: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    fact_ledger: tuple[str, ...]
    locations: tuple[str, ...] = ()
    geographic_groups: tuple[TopicGeographicGroup, ...] = ()
    unresolved_geography_story_ids: tuple[str, ...] = ()
    unresolved_geography_facts: tuple[TopicGeographicFact, ...] = ()
    required_facts: tuple[RequiredDigestFact, ...] = ()
    status_summary: str = ""
    states: tuple[str, ...] = ()
    epistemic_status: str = "сообщения жителей"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "rubric_id": self.rubric_id,
            "topic_key": self.topic_key,
            "topic_label": self.topic_label,
            "emoji": self.emoji,
            "story_ids": list(self.story_ids),
            "support_ids": list(self.support_ids),
            "fact_ledger": list(self.fact_ledger),
            "locations": list(self.locations),
            "geographic_groups": [group.to_dict() for group in self.geographic_groups],
            "unresolved_geography_story_ids": list(self.unresolved_geography_story_ids),
            "unresolved_geography_facts": [
                fact.to_dict() for fact in self.unresolved_geography_facts
            ],
            "required_facts": [rf.to_dict() for rf in self.required_facts],
            "status_summary": self.status_summary,
            "states": list(self.states),
            "epistemic_status": self.epistemic_status,
        }


@dataclass(frozen=True)
class RequiredDigestFact:
    """A discrete material operational proposition required for lossless digest coverage."""

    fact_id: str
    rubric_id: str
    subject_key: str
    subject_label: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    text: str
    original_location: str = ""
    canonical_area_key: str = ""
    observed_at: Any = None
    effective_at: Any = None
    service_state: str = ""
    epistemic_kind: str = ""
    source_published_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "rubric_id": self.rubric_id,
            "subject_key": self.subject_key,
            "subject_label": self.subject_label,
            "story_ids": list(self.story_ids),
            "support_ids": list(self.support_ids),
            "text": self.text,
            "original_location": self.original_location,
            "canonical_area_key": self.canonical_area_key,
            "observed_at": self.observed_at.isoformat()
            if hasattr(self.observed_at, "isoformat")
            else None,
            "effective_at": self.effective_at.isoformat()
            if hasattr(self.effective_at, "isoformat")
            else None,
            "service_state": self.service_state,
            "epistemic_kind": self.epistemic_kind,
            "source_published_at": self.source_published_at.isoformat()
            if hasattr(self.source_published_at, "isoformat")
            else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequiredDigestFact:
        def _optional_datetime(value: Any) -> dt.datetime | None:
            if isinstance(value, dt.datetime):
                return value
            if isinstance(value, str) and value.strip():
                try:
                    return dt.datetime.fromisoformat(value.strip())
                except ValueError:
                    return None
            return None

        return cls(
            fact_id=str(data.get("fact_id", "")),
            rubric_id=str(data.get("rubric_id", "")),
            subject_key=str(data.get("subject_key", "")),
            subject_label=str(data.get("subject_label", "")),
            story_ids=tuple(str(s) for s in data.get("story_ids", [])),
            support_ids=tuple(str(s) for s in data.get("support_ids", [])),
            text=str(data.get("text", "")),
            original_location=str(data.get("original_location", "")),
            canonical_area_key=str(data.get("canonical_area_key", "")),
            observed_at=_optional_datetime(data.get("observed_at")),
            effective_at=_optional_datetime(data.get("effective_at")),
            service_state=str(data.get("service_state", "")),
            epistemic_kind=str(data.get("epistemic_kind", "")),
            source_published_at=_optional_datetime(data.get("source_published_at")),
        )


# Backward compatibility alias
RequiredSituationFact = RequiredDigestFact


@dataclass(frozen=True)
class _CompatibilitySituationPlan:
    groups: tuple[Any, ...] = ()
    covered_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, init=False)
class DigestPresentationPlan:
    story_ids: tuple[str, ...]
    required_facts: tuple[RequiredDigestFact, ...]
    _city_situation: Any
    _story_presentations: tuple[Any, ...]
    composition: Any

    def __init__(
        self,
        story_ids: Sequence[str] = (),
        required_facts: Sequence[RequiredDigestFact] = (),
        city_situation: Any = None,
        story_presentations: Sequence[Any] = (),
        composition: Any = None,
        **kwargs: Any,
    ) -> None:
        # Accept story_hints as alias for story_presentations (backward compat)
        story_hints = kwargs.pop("story_hints", None)
        if story_hints and not story_presentations:
            story_presentations = story_hints
        # Accept detail_story_ids as alias for story_ids (backward compat)
        detail_story_ids = kwargs.pop("detail_story_ids", None)

        s_ids = tuple(story_ids)
        if not s_ids and detail_story_ids:
            s_ids = tuple(detail_story_ids)
        if not s_ids and story_presentations:
            s_ids = tuple(p.story_id for p in story_presentations if getattr(p, "story_id", None))
        object.__setattr__(self, "story_ids", s_ids)
        object.__setattr__(self, "required_facts", tuple(required_facts))
        object.__setattr__(
            self,
            "_city_situation",
            city_situation if city_situation is not None else _CompatibilitySituationPlan(),
        )
        if not story_presentations and s_ids:
            story_presentations = tuple(
                DigestStoryPresentation(story_id=sid, mode="DETAIL_ONLY") for sid in s_ids
            )
        object.__setattr__(self, "_story_presentations", tuple(story_presentations))
        object.__setattr__(self, "composition", composition)

    @property
    def detail_story_ids(self) -> tuple[str, ...]:
        """Story IDs eligible for thematic (detail) blocks — excludes DASHBOARD_ONLY stories."""
        if not self._story_presentations:
            return self.story_ids
        return tuple(
            p.story_id
            for p in self._story_presentations
            if getattr(p, "mode", "DETAIL_ONLY") != "DASHBOARD_ONLY"
        )

    @property
    def city_situation(self) -> Any:
        return self._city_situation

    @property
    def story_presentations(self) -> tuple[Any, ...]:
        return self._story_presentations

    @property
    def story_hints(self) -> tuple[Any, ...]:
        return self._story_presentations

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "story_ids": list(self.story_ids),
            "required_facts": [fact.to_dict() for fact in self.required_facts],
            "composition": self.composition.to_dict()
            if self.composition and hasattr(self.composition, "to_dict")
            else None,
        }

    def to_metadata_dict(self) -> dict[str, Any]:
        """Persist only frozen provenance IDs and semantic versions, not evidence text."""
        return {
            "story_ids": list(self.story_ids),
            "required_facts": [
                {
                    "fact_id": fact.fact_id,
                    "story_ids": list(fact.story_ids),
                    "support_ids": list(fact.support_ids),
                    "rubric_id": fact.rubric_id,
                }
                for fact in self.required_facts
            ],
            "composition": (
                self.composition.to_metadata_dict()
                if self.composition and hasattr(self.composition, "to_metadata_dict")
                else None
            ),
        }

    def with_composition(self, composition: Any) -> DigestPresentationPlan:
        """Freeze the admitted Story/fact membership on this existing presentation plan."""
        composition_records = tuple(getattr(composition, "fact_records", ()))
        validate_digest_fact_ids(
            self.required_facts,
            error_code="DIGEST_DUPLICATE_REQUIRED_FACT_ID",
        )
        validate_digest_fact_ids(
            composition_records,
            error_code="DIGEST_DUPLICATE_COMPOSITION_FACT_ID",
        )
        admitted_story_ids = set(getattr(composition, "admitted_story_ids", ()))
        admitted_fact_ids = set(getattr(composition, "admitted_fact_ids", ()))
        records = {
            str(getattr(record, "fact_id", "") or "").strip(): record
            for record in composition_records
        }
        admitted_support_ids = {
            support_id
            for fact_id, record in records.items()
            if fact_id in admitted_fact_ids
            for support_id in getattr(record, "support_ids", ())
        }
        deferred_support_ids = {
            support_id
            for fact_id, record in records.items()
            if fact_id not in admitted_fact_ids
            for support_id in getattr(record, "support_ids", ())
        }
        enriched_facts = []
        for fact in self.required_facts:
            fact_id = str(fact.fact_id or "").strip()
            if fact_id not in admitted_fact_ids:
                continue
            record = records.get(fact_id)
            if record is None:
                enriched_facts.append(fact)
                continue
            enriched_facts.append(
                replace(
                    fact,
                    support_ids=tuple(record.support_ids),
                    original_location=record.original_location,
                    canonical_area_key=record.canonical_area,
                    observed_at=record.observed_time,
                    effective_at=record.effective_time,
                    service_state=record.service_state,
                    epistemic_kind=record.epistemic_kind,
                    source_published_at=record.source_publication_time,
                )
            )
        known_ids = {str(fact.fact_id or "").strip() for fact in self.required_facts}
        for record in composition_records:
            record_fact_id = str(record.fact_id or "").strip()
            if record_fact_id in known_ids or record_fact_id not in admitted_fact_ids:
                continue
            enriched_facts.append(
                RequiredDigestFact(
                    fact_id=record_fact_id,
                    rubric_id=record.rubric_id,
                    subject_key=record.canonical_subject or "local_report",
                    subject_label=record.canonical_subject or "Местное сообщение",
                    story_ids=tuple(record.story_ids),
                    support_ids=tuple(record.support_ids),
                    text=record.text,
                    original_location=record.original_location,
                    canonical_area_key=record.canonical_area,
                    observed_at=record.observed_time,
                    effective_at=record.effective_time,
                    service_state=record.service_state,
                    epistemic_kind=record.epistemic_kind,
                    source_published_at=record.source_publication_time,
                )
            )
        city_situation = self._city_situation
        if city_situation is not None:
            situation_groups = getattr(city_situation, "groups", None)
            if situation_groups is not None:
                filtered_groups = []
                for group in situation_groups:
                    group_facts = tuple(getattr(group, "required_facts", ()) or ())
                    kept_facts = tuple(
                        fact
                        for fact in group_facts
                        if (
                            getattr(fact, "fact_id", "") in admitted_fact_ids
                            if getattr(fact, "fact_id", "")
                            else bool(set(getattr(fact, "story_ids", ())) & admitted_story_ids)
                        )
                    )
                    group_story_ids = set(getattr(group, "covered_story_ids", ()) or ())
                    if not group_story_ids and group_facts:
                        group_story_ids = {
                            story_id
                            for fact in group_facts
                            for story_id in getattr(fact, "story_ids", ())
                        }
                    # A pre-composed group can contain both admitted and
                    # deferred Stories. Its combined detail text cannot be
                    # safely sliced, so drop the whole group in that case.
                    if group_story_ids and not group_story_ids.issubset(admitted_story_ids):
                        continue
                    if group_facts and len(kept_facts) != len(group_facts):
                        continue
                    group_supports = set(getattr(group, "cited_support_ids", ()) or ())
                    group_supports.update(getattr(group, "source_refs", ()) or ())
                    if group_supports & deferred_support_ids:
                        continue
                    if group_supports and not (group_supports & admitted_support_ids):
                        continue
                    if (
                        not group_story_ids
                        and not kept_facts
                        and not (group_supports & admitted_support_ids)
                    ):
                        continue
                    changes: dict[str, Any] = {}
                    if hasattr(group, "covered_story_ids"):
                        changes["covered_story_ids"] = tuple(
                            sorted(group_story_ids & admitted_story_ids)
                        )
                    if hasattr(group, "required_facts"):
                        changes["required_facts"] = kept_facts
                    for field_name in ("source_refs", "cited_support_ids"):
                        if hasattr(group, field_name):
                            original = tuple(getattr(group, field_name, ()) or ())
                            changes[field_name] = tuple(
                                value for value in original if value in admitted_support_ids
                            )
                    filtered_groups.append(replace(group, **changes) if changes else group)
                if hasattr(city_situation, "groups"):
                    city_situation = replace(city_situation, groups=tuple(filtered_groups))
            elif getattr(city_situation, "items", None) is not None:
                # Rollup items have exact fact IDs when available; older rows
                # can be matched only through their exact source refs.
                kept_items = []
                for item in getattr(city_situation, "items", ()) or ():
                    item_fact_id = str(getattr(item, "fact_id", "") or "").strip()
                    item_supports = set(getattr(item, "source_refs", ()) or ())
                    item_supports.update(getattr(item, "current_source_refs", ()) or ())
                    if item_fact_id:
                        keep = item_fact_id in admitted_fact_ids
                    else:
                        keep = bool(item_supports & admitted_support_ids) and not bool(
                            item_supports & deferred_support_ids
                        )
                    if keep:
                        kept_items.append(item)
                city_situation = replace(city_situation, items=tuple(kept_items))

        return DigestPresentationPlan(
            story_ids=tuple(s for s in self.story_ids if s in admitted_story_ids),
            required_facts=tuple(enriched_facts),
            city_situation=city_situation,
            story_presentations=tuple(
                p
                for p in self._story_presentations
                if getattr(p, "story_id", None) in admitted_story_ids
            ),
            composition=composition,
        )


# ---------------------------------------------------------------------------
# Backward-compatible stub types for legacy tests / code that still imports
# dashboard-era presentation classes.  These are intentionally thin so that
# the consuming code can call getattr() on them safely.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CitySituationPresentationGroup:
    """Legacy city-situation group stub (kept for backward compatibility)."""

    group_id: str = ""
    group_kind: str = ""
    subject_key: str = ""
    subject_label: str = ""
    state: str = ""
    source_refs: tuple[str, ...] = ()
    detail_lines: tuple[str, ...] = ()
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()

    @property
    def reader_text(self) -> str:
        parts = list(self.detail_lines)
        return "; ".join(parts) if parts else self.subject_label

    @property
    def all_detail_lines(self) -> tuple[str, ...]:
        return self.detail_lines


@dataclass(frozen=True)
class CitySituationPresentationPlan:
    """Legacy city-situation plan stub (kept for backward compatibility)."""

    groups: tuple[CitySituationPresentationGroup, ...] = ()
    covered_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestStoryPresentation:
    """Legacy per-story presentation descriptor (kept for backward compatibility)."""

    story_id: str = ""
    mode: str = "DETAIL_ONLY"
    detail_support_ids: tuple[str, ...] = ()
    merge_group_id: str = ""
    city_situation_group_ids: tuple[str, ...] = ()

    # detail_role is derived from mode for backward compatibility
    @property
    def detail_role(self) -> str:
        return "DRILL_DOWN" if self.mode == "DASHBOARD_AND_DRILLDOWN" else "NORMAL"


@dataclass(frozen=True)
class DigestStoryPresentationHint:
    """Hint variant of DigestStoryPresentation with explicit detail_role."""

    story_id: str = ""
    detail_support_ids: tuple[str, ...] = ()
    merge_group_id: str = ""
    detail_role: str = "NORMAL"


def _norm_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _detail_line(item: CitySituationItem) -> str:
    from src.processing.operational_semantics import sanitize_operational_detail

    clean_detail = sanitize_operational_detail(item.detail)
    if not clean_detail:
        return ""
    detail = clean_detail

    location = item.location.strip()
    if location and location.casefold() not in detail.casefold():
        return f"{location}: {detail}"
    return detail or item.subject_label or item.subject_key


_CITY_SITUATION_SUBJECT_ALIASES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "water",
        frozenset(
            {
                "water",
                "water_supply",
                "water_network",
                "vodosnabzhenie",
                "voda",
                "водоснабжение",
                "вода",
            }
        ),
    ),
    (
        "electricity",
        frozenset(
            {
                "electricity",
                "power",
                "power_supply",
                "electricity_supply",
                "electrosupply",
                "электроснабжение",
                "электричество",
                "свет",
            }
        ),
    ),
    (
        "gas",
        frozenset(
            {
                "gas",
                "gas_supply",
                "газоснабжение",
                "газ",
            }
        ),
    ),
    (
        "heating",
        frozenset(
            {
                "heating",
                "heat_supply",
                "district_heating",
                "отопление",
                "теплоснабжение",
            }
        ),
    ),
    (
        "connectivity",
        frozenset(
            {
                "connectivity",
                "mobile_connection",
                "mobile_network",
                "telecom",
                "mobile_internet",
                "связь",
                "интернет",
                "мобильная_связь",
            }
        ),
    ),
    (
        "urban_transport",
        frozenset(
            {
                "urban_transport",
                "city_transport",
                "городской_транспорт",
                "городской_автобус",
                "городские_автобусы",
                "трамвай",
                "трамваи",
                "троллейбус",
                "троллейбусы",
                "маршрутка",
                "городская_маршрутка",
            }
        ),
    ),
)


def _canonical_city_situation_subject(item: CitySituationItem) -> str | None:
    combined_text = f"{item.subject_label} {item.detail}"
    # Reject retail product sales (e.g. bottled water, 3 rub/l) from operational facts
    if re.search(r"\b(?:розлив|розничн|руб/л|₽/л|3\s*₽/литр)\b", combined_text, re.IGNORECASE):
        return None
    # Reject long-distance/intercity transport from operational facts
    if re.search(
        r"\b(?:междугородн|межгород|ростов|тбилиси|москва|симферополь|донецк|луганск|таганрог)\b",
        combined_text,
        re.IGNORECASE,
    ):
        return None

    key = _norm_key(item.subject_key).replace(" ", "_")
    label = _norm_key(item.subject_label).replace(" ", "_")
    for root, aliases in _CITY_SITUATION_SUBJECT_ALIASES:
        if key in aliases or label in aliases:
            return root
    return None


def _fact_id_time(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _generated_digest_fact_id(
    topic_namespace: str,
    *,
    text: str,
    location: str,
    service_state: str,
    support_ids: Sequence[str],
    observed_at: Any = None,
    effective_at: Any = None,
    source_published_at: Any = None,
) -> str:
    """Build a stable ID from canonical topic and the complete fact provenance."""
    canonical_namespace = _norm_key(topic_namespace)
    namespace_slug = re.sub(r"[^\w]+", "_", canonical_namespace).strip("_") or "digest_fact"
    payload = {
        "effective_at": _fact_id_time(effective_at),
        "location": (location or "").strip(),
        "observed_at": _fact_id_time(observed_at),
        "service_state": (service_state or "").strip().casefold(),
        "source_published_at": _fact_id_time(source_published_at),
        "support_ids": sorted(
            {str(support_id).strip() for support_id in support_ids if str(support_id).strip()}
        ),
        "text": (text or "").strip(),
        "topic_namespace": canonical_namespace,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    discriminator = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]
    return f"{namespace_slug}_{discriminator}"


def _assign_unique_generated_fact_ids(
    facts: Sequence[RequiredDigestFact], generated: Sequence[bool]
) -> tuple[RequiredDigestFact, ...]:
    if len(facts) != len(generated):
        raise DigestCoverageInvariantError("DIGEST_FACT_ID_ASSIGNMENT_MISMATCH")
    explicit_facts = tuple(fact for fact, is_generated in zip(facts, generated) if not is_generated)
    validate_digest_fact_ids(
        explicit_facts,
        error_code="DIGEST_DUPLICATE_REQUIRED_FACT_ID",
    )
    used_ids = {fact.fact_id.strip() for fact in explicit_facts}
    assigned: list[RequiredDigestFact] = []
    for fact, is_generated in zip(facts, generated):
        if not is_generated:
            assigned.append(fact)
            continue
        base_id = fact.fact_id.strip()
        candidate_id = base_id
        suffix = 2
        while candidate_id in used_ids:
            candidate_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(candidate_id)
        assigned.append(replace(fact, fact_id=candidate_id))
    validate_digest_fact_ids(
        assigned,
        error_code="DIGEST_DUPLICATE_REQUIRED_FACT_ID",
    )
    return tuple(assigned)


def _matches_card(card_id: str, evi: Any, eid: str) -> bool:
    evi_sid = getattr(evi, "story_id", None)
    if str(evi_sid) == card_id or f"story:{evi_sid}" == card_id:
        return True
    if eid.startswith(f"{card_id}:"):
        return True
    num_part = card_id.split(":", 1)[1] if card_id.startswith("story:") else None
    if num_part and num_part.isdigit() and evi_sid is not None:
        try:
            if int(evi_sid) == int(num_part):
                return True
        except (ValueError, TypeError):
            pass
    return False


def _resolve_fact_supports(
    direct_refs: Sequence[str],
    card: Any,
    evidence_map: Mapping[str, Any],
    fact_text: str = "",
) -> tuple[str, ...]:
    supports: list[str] = [r for r in direct_refs if r]
    ref_set = set(supports)
    for evi in evidence_map.values():
        if getattr(evi, "publication_use", "PUBLISH") != "PUBLISH":
            continue
        e_ref = getattr(evi, "source_ref", None)
        eid = getattr(evi, "evidence_id", "")
        # A fragment can support several distinct claims. Promote an evidence
        # item ID into this fact's support only when its exact text matches the
        # fact and the source reference also matches.
        if (
            eid
            and eid in ref_set
            or (
                e_ref
                and e_ref in ref_set
                and str(getattr(evi, "text", "")).strip() == fact_text.strip()
            )
        ):
            if eid and eid not in supports:
                supports.append(eid)
    if not supports:
        for r in getattr(card, "representative_source_refs", []) or []:
            if r and r not in supports:
                supports.append(r)
    return tuple(dict.fromkeys(supports))


def _exact_evidence_metadata(
    refs: Sequence[str], evidence_map: Mapping[str, Any], fact_text: str = ""
) -> tuple[str, Any, Any]:
    """Return epistemic/time metadata only when exact support refs identify it unambiguously."""
    ref_set = {str(ref) for ref in refs if str(ref)}
    matches = []
    for item in evidence_map.values():
        if getattr(item, "publication_use", "PUBLISH") != "PUBLISH":
            continue
        evidence_id = str(getattr(item, "evidence_id", ""))
        source_ref = str(getattr(item, "source_ref", ""))
        if evidence_id in ref_set or (
            source_ref in ref_set and str(getattr(item, "text", "")).strip() == fact_text.strip()
        ):
            matches.append(item)
    kinds = {str(getattr(item, "kind", "")) for item in matches if getattr(item, "kind", "")}
    times = {
        getattr(item, "observed_at", None) for item in matches if getattr(item, "observed_at", None)
    }
    return (
        next(iter(kinds)) if len(kinds) == 1 else "",
        next(iter(times)) if len(times) == 1 else None,
        None,
    )


def build_required_digest_facts(
    *,
    cards: Sequence[Any],
    evidence: Mapping[str, Any] | None = None,
    city_situation: CitySituationRollup | None = None,
) -> tuple[RequiredDigestFact, ...]:
    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    # If legacy city_situation with items is provided, use legacy extraction
    if city_situation and city_situation.items:
        card_by_id = {c.id: c for c in cards}

        # Precompute card references and lineage
        card_refs_map: dict[str, set[str]] = {}
        for card in cards:
            refs: set[str] = set()
            all_refs_fn = getattr(card, "all_source_refs", None)
            if callable(all_refs_fn):
                refs.update(r for r in all_refs_fn() if r)
            else:
                refs.update(r for r in getattr(card, "representative_source_refs", []) or [] if r)
            for elem_list in (
                getattr(card, "hard_facts", []) or [],
                getattr(card, "community_observations", []) or [],
                getattr(card, "useful_details", []) or [],
                getattr(card, "operational_observations", []) or [],
            ):
                for elem in elem_list:
                    refs.update(r for r in getattr(elem, "source_refs", []) or [] if r)
            card_refs_map[card.id] = refs

        required_facts: list[RequiredDigestFact] = []
        generated_fact_ids: list[bool] = []

        for item in city_situation.items:
            canonical_subj = _canonical_city_situation_subject(item)
            if canonical_subj is None:
                continue

            orig_detail = (getattr(item, "detail", "") or "").casefold()
            if "in_reply_to" in orig_detail or "in reply to" in orig_detail:
                continue

            # Preserve subject_key, subject_label, and sanitized reader fact text
            fact_text = _detail_line(item)
            if not fact_text or not _is_usable_fact_line(fact_text):
                continue

            group_id = f"situation:{canonical_subj}"
            explicit_fact_id = str(getattr(item, "fact_id", "") or "").strip()

            # Resolve allowed support IDs from current_source_refs / source_refs
            item_refs = tuple(
                dict.fromkeys(
                    r for r in (getattr(item, "current_source_refs", ()) or item.source_refs) if r
                )
            )
            fact_supports: list[str] = list(item_refs)
            fact_stories: list[str] = []

            # Match via direct card source refs
            item_ref_set = set(item_refs)
            for card in cards:
                if bool(item_ref_set & card_refs_map.get(card.id, set())):
                    if card.id not in fact_stories:
                        fact_stories.append(card.id)

            # Match via PublicationEvidence
            for evi in evidence_map.values():
                if getattr(evi, "publication_use", "PUBLISH") != "PUBLISH":
                    continue
                e_ref = getattr(evi, "source_ref", None)
                eid = getattr(evi, "evidence_id", "")
                if (e_ref and e_ref in item_ref_set) or (eid and eid in item_ref_set):
                    if (
                        (
                            (eid and eid in item_ref_set)
                            or (
                                e_ref
                                and e_ref in item_ref_set
                                and str(getattr(evi, "text", "")).strip() == fact_text.strip()
                            )
                        )
                        and eid
                        and eid not in fact_supports
                    ):
                        fact_supports.append(eid)
                    # match card via evidence
                    for card in cards:
                        if _matches_card(card.id, evi, eid):
                            if card.id not in fact_stories:
                                fact_stories.append(card.id)
                    # match card via story_id on evidence
                    evi_sid = getattr(evi, "story_id", None)
                    if evi_sid is not None:
                        st_str = (
                            f"story:{evi_sid}"
                            if not str(evi_sid).startswith("story:")
                            else str(evi_sid)
                        )
                        if st_str in card_by_id and st_str not in fact_stories:
                            fact_stories.append(st_str)

            # Fail closed if unmapped to any selected story
            if not fact_stories:
                diagnostic_fact_id = explicit_fact_id or group_id
                raise DigestCoverageInvariantError(f"UNMAPPED_REQUIRED_FACT:{diagnostic_fact_id}")

            # Derive rubric_id from the first owning StoryCard
            first_owning_card = card_by_id.get(fact_stories[0])
            rubric_id = getattr(first_owning_card, "rubric_id", "") or "infrastructure"
            item_kind, item_observed, item_published = _exact_evidence_metadata(
                item_refs, evidence_map, fact_text
            )
            observed_at = item_observed or getattr(item, "last_observed_at", None)
            fact_support_ids = tuple(dict.fromkeys(fact_supports))
            topic_rubrics = sorted(
                {
                    getattr(card_by_id.get(story_id), "rubric_id", "") or "infrastructure"
                    for story_id in fact_stories
                }
            )
            fact_id = explicit_fact_id or _generated_digest_fact_id(
                f"{'|'.join(topic_rubrics)}:{canonical_subj}",
                text=fact_text,
                location=str(getattr(item, "location", "") or ""),
                service_state=str(getattr(item, "state", "") or ""),
                support_ids=fact_support_ids,
                observed_at=observed_at,
                source_published_at=item_published,
            )

            required_facts.append(
                RequiredDigestFact(
                    fact_id=fact_id,
                    rubric_id=rubric_id,
                    subject_key=item.subject_key or canonical_subj,
                    subject_label=item.subject_label or canonical_subj.title(),
                    story_ids=tuple(fact_stories),
                    support_ids=fact_support_ids,
                    text=fact_text,
                    original_location=str(getattr(item, "location", "") or ""),
                    observed_at=observed_at,
                    service_state=str(getattr(item, "state", "") or ""),
                    epistemic_kind=item_kind,
                    source_published_at=item_published,
                )
            )
            generated_fact_ids.append(not bool(explicit_fact_id))

        return _assign_unique_generated_fact_ids(required_facts, generated_fact_ids)

    # Event-First canonical extraction directly from StoryCards without city_situation
    card_by_id = {c.id: c for c in cards}
    card_required_facts: list[RequiredDigestFact] = []
    card_generated_fact_ids: list[bool] = []

    for card in cards:
        service_family = _canonical_service_family(card)
        is_operational = (
            service_family is not None
            or getattr(card, "story_kind", "") == "operational_status"
            or getattr(card, "rubric_id", "") in ("infrastructure", "utilities", "communal")
        )
        if not is_operational:
            continue

        subj_key = service_family or "infrastructure"
        subj_label = getattr(card, "topic", "") or getattr(card, "category", "") or subj_key.title()
        rubric_id = getattr(card, "rubric_id", "") or "infrastructure"

        obs_list = getattr(card, "operational_observations", []) or []
        if obs_list:
            for obs in obs_list:
                o_loc = getattr(obs, "location", "") or ""
                o_detail = getattr(obs, "detail", "") or ""
                o_text = f"{o_loc}: {o_detail}".strip(": ") if o_loc else o_detail
                if not o_text:
                    continue
                if not _is_usable_fact_line(o_text) and not _is_usable_fact_line(o_detail):
                    continue
                o_loc_clean = o_loc.strip().casefold()
                if not o_loc_clean or o_loc_clean in (
                    "бердянск",
                    "город",
                    "г. бердянск",
                    "г.бердянск",
                    "г бердянск",
                    "city",
                ):
                    if not any(
                        kw in o_detail.casefold()
                        for kw in (
                            "улиц",
                            "ул.",
                            "район",
                            "проспект",
                            "гора",
                            "колони",
                            "слободк",
                            "акз",
                            "кос",
                            "центр",
                            "порт",
                            "нагорн",
                            "володарск",
                            "димитров",
                            "морозов",
                            "свердлов",
                            "орджоникидзе",
                        )
                    ):
                        continue

                obs_refs = list(getattr(obs, "source_refs", []) or [])
                for fid in getattr(obs, "source_fragment_ids", []) or []:
                    ref_fid = f"fragment:{fid}"
                    if ref_fid not in obs_refs:
                        obs_refs.append(ref_fid)

                obs_supports = _resolve_fact_supports(obs_refs, card, evidence_map, o_text)
                obs_sups_list = list(obs_supports)
                if card.id not in obs_sups_list:
                    obs_sups_list.append(card.id)
                obs_supports = tuple(obs_sups_list)
                obs_kind, obs_time, obs_published = _exact_evidence_metadata(
                    obs_refs, evidence_map, o_text
                )
                obs_effective_at = getattr(obs, "effective_from", None)
                obs_subject_key = getattr(obs, "subject_key", "") or subj_key
                fact_id = _generated_digest_fact_id(
                    f"{rubric_id}:{obs_subject_key}",
                    text=o_text,
                    location=o_loc,
                    service_state=str(getattr(obs, "state", "") or ""),
                    support_ids=obs_supports,
                    observed_at=obs_time,
                    effective_at=obs_effective_at,
                    source_published_at=obs_published,
                )

                card_required_facts.append(
                    RequiredDigestFact(
                        fact_id=fact_id,
                        rubric_id=rubric_id,
                        subject_key=obs_subject_key,
                        subject_label=getattr(obs, "subject_label", "") or subj_label,
                        story_ids=(card.id,),
                        support_ids=obs_supports,
                        text=o_text,
                        original_location=o_loc,
                        effective_at=obs_effective_at,
                        observed_at=obs_time,
                        service_state=str(getattr(obs, "state", "") or ""),
                        epistemic_kind=obs_kind,
                        source_published_at=obs_published,
                    )
                )
                card_generated_fact_ids.append(True)
        else:
            hf_list = getattr(card, "hard_facts", []) or []
            for hf in hf_list:
                hf_text = getattr(hf, "text", "").strip()
                if not hf_text:
                    continue
                if not _is_usable_fact_line(hf_text):
                    continue
                if is_operational and not _OPERATIONAL_SERVICE_KW_RE.search(hf_text):
                    continue
                hf_refs = list(getattr(hf, "source_refs", []) or [])
                hf_supports = _resolve_fact_supports(hf_refs, card, evidence_map, hf_text)
                hf_sups_list = list(hf_supports)
                if card.id not in hf_sups_list:
                    hf_sups_list.append(card.id)
                hf_supports = tuple(hf_sups_list)
                hf_kind, hf_time, hf_published = _exact_evidence_metadata(
                    hf_refs, evidence_map, hf_text
                )
                original_location = "; ".join(
                    dict.fromkeys(
                        str(a).strip() for a in (getattr(hf, "areas", ()) or ()) if str(a).strip()
                    )
                )
                hf_effective_at = getattr(hf, "effective_from", None)
                hf_state = str(getattr(hf, "state", "") or "")
                fact_id = _generated_digest_fact_id(
                    f"{rubric_id}:{subj_key}",
                    text=hf_text,
                    location=original_location,
                    service_state=hf_state,
                    support_ids=hf_supports,
                    observed_at=hf_time,
                    effective_at=hf_effective_at,
                    source_published_at=hf_published,
                )

                card_required_facts.append(
                    RequiredDigestFact(
                        fact_id=fact_id,
                        rubric_id=rubric_id,
                        subject_key=subj_key,
                        subject_label=subj_label,
                        story_ids=(card.id,),
                        support_ids=hf_supports,
                        text=hf_text,
                        original_location=original_location,
                        observed_at=hf_time,
                        epistemic_kind=hf_kind,
                        source_published_at=hf_published,
                    )
                )
                card_generated_fact_ids.append(True)

    return _assign_unique_generated_fact_ids(card_required_facts, card_generated_fact_ids)


_PRIORITY_RUBRIC_WEIGHTS = {
    "focus": 110,
    "infrastructure": 100,
    "security": 90,
    "safety": 90,
    "civic_services": 80,
    "mobility": 75,
    "transport": 75,
    "communications": 70,
    "health": 65,
    "economy": 50,
    "society": 45,
    "social": 45,
    "education": 40,
    "education_culture": 40,
    "culture": 40,
    "weather": 25,
    "environment": 25,
    "other": 15,
}

_DEFAULT_RUBRIC_CARD_CAPS = {
    "focus": 4,
    "infrastructure": 20,
    "security": 6,
    "safety": 6,
    "civic_services": 6,
    "mobility": 6,
    "transport": 6,
    "communications": 6,
    "health": 6,
    "economy": 6,
    "society": 6,
    "social": 6,
    "education": 4,
    "education_culture": 4,
    "culture": 4,
    "weather": 3,
    "environment": 3,
    "other": 6,
}

_STORY_IMPORTANCE_WEIGHTS = {
    "high": 1.0,
    "medium": 0.5,
    "low": 0.2,
}


def build_digest_presentation_plan(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None = None,
    evidence: Mapping[str, Any] | None = None,
    include_all_candidates: bool = False,
    **kwargs: Any,
) -> DigestPresentationPlan:
    """Build the presentation plan, optionally retaining every input candidate for composition."""
    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    # Pre-identify cards associated with usable operational city situation items
    cards_with_city_situation: set[str] = set()
    if city_situation and city_situation.items:
        card_refs_map: dict[str, set[str]] = {}
        for c in cards:
            refs: set[str] = set()
            all_refs_fn = getattr(c, "all_source_refs", None)
            if callable(all_refs_fn):
                refs.update(r for r in all_refs_fn() if r)
            else:
                refs.update(r for r in getattr(c, "representative_source_refs", []) or [] if r)
            for elem_list in (
                getattr(c, "hard_facts", []) or [],
                getattr(c, "community_observations", []) or [],
                getattr(c, "useful_details", []) or [],
                getattr(c, "operational_observations", []) or [],
            ):
                for elem in elem_list:
                    refs.update(r for r in getattr(elem, "source_refs", []) or [] if r)
            card_refs_map[c.id] = refs

        for item in city_situation.items:
            if _canonical_city_situation_subject(item) is None:
                continue
            orig_d = (getattr(item, "detail", "") or "").casefold()
            if "in_reply_to" in orig_d or "in reply to" in orig_d:
                continue
            f_text = _detail_line(item)
            if not f_text or not _is_usable_fact_line(f_text):
                continue
            item_refs = {
                r for r in (getattr(item, "current_source_refs", ()) or item.source_refs) if r
            }
            for c in cards:
                if bool(item_refs & card_refs_map.get(c.id, set())):
                    cards_with_city_situation.add(c.id)

    user_max_cards = kwargs.get("max_presentation_cards")

    # Group cards by rubric to ensure broad, balanced city coverage across multiple domains
    cards_by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        r_id = getattr(c, "rubric_id", "other") or "other"
        cards_by_rubric.setdefault(r_id, []).append(c)

    # Sort and cap cards within each rubric so one high-volume rubric cannot starve the rest
    balanced_cards: list[Any] = []
    for r_id, r_cards in cards_by_rubric.items():
        cap = _DEFAULT_RUBRIC_CARD_CAPS.get(r_id, 6)

        def _card_key(c: Any) -> tuple[int, int, float]:
            owns_sit = 1 if c.id in cards_with_city_situation else 0
            has_ops = 1 if getattr(c, "operational_observations", None) else 0
            raw_imp = getattr(c, "importance", "medium")
            if isinstance(raw_imp, (int, float)):
                imp = float(raw_imp)
            else:
                imp = _STORY_IMPORTANCE_WEIGHTS.get(str(raw_imp).strip().lower(), 0.5)
            return (owns_sit, has_ops, imp)

        r_cards.sort(key=_card_key, reverse=True)
        # Always retain cards backing operational city situation, plus top cards up to cap
        owns_sit_count = sum(1 for c in r_cards if c.id in cards_with_city_situation)
        effective_cap = len(r_cards) if include_all_candidates else max(cap, owns_sit_count)
        balanced_cards.extend(r_cards[:effective_cap])

    # If caller explicitly requested a strict card limit (e.g. in test fixtures):
    if (
        not include_all_candidates
        and user_max_cards is not None
        and len(balanced_cards) > user_max_cards
    ):
        effective_max = max(user_max_cards, len(cards_with_city_situation))

        def _global_priority(c: Any) -> tuple[int, int, int, float]:
            owns_sit = 1 if c.id in cards_with_city_situation else 0
            r_id = getattr(c, "rubric_id", "other") or "other"
            w = _PRIORITY_RUBRIC_WEIGHTS.get(r_id, 10)
            has_ops = 1 if getattr(c, "operational_observations", None) else 0
            raw_imp = getattr(c, "importance", "medium")
            if isinstance(raw_imp, (int, float)):
                imp = float(raw_imp)
            else:
                imp = _STORY_IMPORTANCE_WEIGHTS.get(str(raw_imp).strip().lower(), 0.5)
            return (owns_sit, has_ops, w, imp)

        balanced_cards.sort(key=_global_priority, reverse=True)
        balanced_cards = balanced_cards[:effective_max]

    selected_cards = balanced_cards

    return DigestPresentationPlan(
        story_ids=tuple(card.id for card in selected_cards),
        required_facts=build_required_digest_facts(
            cards=selected_cards,
            evidence=evidence_map,
            city_situation=city_situation,
        ),
    )


def render_city_situation_presentation(*args: Any, **kwargs: Any) -> str:
    """Deprecated: no reader-facing dashboard rendering exists in thematic digest synthesis."""
    return ""


_GENERIC_STOP_TAGS = frozenset(
    {
        "город",
        "города",
        "городской",
        "городские",
        "житель",
        "жители",
        "жителей",
        "новость",
        "новости",
        "информация",
        "информации",
        "местный",
        "местные",
        "общество",
        "происшествия",
        "события",
        "сообщество",
        "news",
        "city",
        "resident",
        "local",
        "info",
        "жкх",
        "коммуналка",
        "коммунальные_услуги",
        "коммунальные",
        "коммунальный",
        "услуги",
        "служба",
        "службы",
        "благоустройство",
        "отключение",
        "отключения",
        "авария",
        "перебои",
        "график",
        "жалоба",
        "жалобы",
        "жалоба_жителей",
        "жалобы_жителей",
        "обращение",
        "заявка",
        "заявки",
        "сервис",
        "телефон",
        "номер",
        "адрес",
        "ремонт",
        "работы",
        "ситуация",
        "состояние",
        "бердянск",
        "бердянске",
        "бердянска",
        "бердянский",
        "водоснабжение",
        "электроснабжение",
        "электричество",
        "свет",
        "вода",
        "газ",
        "отопление",
        "связь",
        "интернет",
        "транспорт",
        "инфраструктура",
    }
)

_EDITION_LEVEL_AREAS = frozenset(
    {
        "бердянск",
        "бердянский",
        "бердянске",
        "бердянска",
        "город",
        "городской",
        "в городе",
        "центр города",
        "район города",
    }
)


def _compute_batch_frequent_tags(cards: Sequence[Any], threshold: float = 0.30) -> set[str]:
    from collections import Counter

    if len(cards) < 8:
        return set()
    tag_rubrics: dict[str, set[str]] = {}
    tag_counts: Counter[str] = Counter()
    for c in cards:
        rid = getattr(c, "rubric_id", "") or ""
        seen_in_card: set[str] = set()
        for t in getattr(c, "tags", []) or []:
            norm = " ".join(str(t).casefold().split())
            if norm and norm not in seen_in_card:
                seen_in_card.add(norm)
                tag_counts[norm] += 1
                tag_rubrics.setdefault(norm, set()).add(rid)

    cutoff = max(5, int(len(cards) * threshold))
    return {
        tag
        for tag, count in tag_counts.items()
        if count >= cutoff and len(tag_rubrics.get(tag, set())) >= 3
    }


def _card_specific_tags(card: Any, batch_stop_tags: set[str] | None = None) -> set[str]:
    tags: set[str] = set()
    raw_tags = getattr(card, "tags", []) or []
    for t in raw_tags:
        norm = " ".join(str(t).casefold().split())
        if (
            norm
            and norm not in _GENERIC_STOP_TAGS
            and (batch_stop_tags is None or norm not in batch_stop_tags)
            and len(norm) > 2
        ):
            tags.add(norm)
    return tags


def _card_areas(card: Any) -> set[str]:
    areas: set[str] = set()
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            for a in getattr(elem, "areas", []) or []:
                norm = " ".join(str(a).casefold().split())
                if norm and norm not in _EDITION_LEVEL_AREAS:
                    areas.add(norm)
    return areas


@lru_cache(maxsize=16)
def _load_digest_geography_resolver(edition_slug: str) -> Any | None:
    """Load only the checked-in geography profile for the current edition."""
    slug = (edition_slug or "").strip()
    if not slug or not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return None

    path = Path("data/city_profiles") / f"{slug}.yaml"
    if not path.is_file():
        return None

    from src.city_context import CityContextResolver, CityProfileError

    try:
        resolver = CityContextResolver.from_yaml(path)
    except (CityProfileError, FileNotFoundError, OSError):
        return None

    if resolver.profile_id.casefold() != slug.casefold():
        return None
    return resolver


def _resolved_geographic_scopes(text: str, resolver: Any) -> dict[str, str]:
    """Resolve source wording to stable profile area IDs, preferring local labels."""
    if not text.strip():
        return {}

    annotation = resolver.resolve(text)
    area_scopes: dict[str, str] = {}
    place_entities = []
    for entity in annotation.entities:
        if entity.confidence != "high":
            continue
        if entity.kind == "area":
            colloquial_ids = tuple(getattr(entity, "colloquial_area_ids", ()) or ())
            if colloquial_ids:
                for area_id in colloquial_ids:
                    area_scopes[f"colloquial:{area_id}"] = (
                        entity.matched_text or entity.canonical_name or area_id
                    )
                continue
            for area in getattr(entity, "municipal_areas", ()) or ():
                if area.confidence == "high":
                    area_scopes[f"municipal:{area.area_id}"] = entity.matched_text or area.area_name
            if not getattr(entity, "municipal_areas", ()):
                area_scopes[f"area:{entity.entity_id}"] = (
                    entity.matched_text or entity.canonical_name or entity.entity_id
                )
        elif entity.kind == "place":
            place_entities.append(entity)

    if area_scopes:
        return area_scopes

    place_scopes: dict[str, str] = {}
    for entity in place_entities:
        colloquial_ids = tuple(getattr(entity, "colloquial_area_ids", ()) or ())
        if colloquial_ids:
            for area_id in colloquial_ids:
                place_scopes[f"colloquial:{area_id}"] = (
                    entity.matched_text or entity.canonical_name or area_id
                )
            continue
        municipal_areas = tuple(
            area
            for area in (getattr(entity, "municipal_areas", ()) or ())
            if area.confidence == "high"
        )
        if municipal_areas:
            for area in municipal_areas:
                place_scopes[f"municipal:{area.area_id}"] = entity.matched_text or area.area_name
        elif entity.confidence == "high":
            place_scopes[f"place:{entity.entity_id}"] = (
                entity.matched_text or entity.canonical_name or entity.entity_id
            )
    return place_scopes


def _card_geographic_scopes(card: Any, resolver: Any | None) -> dict[str, str]:
    """Resolve the card's own evidence locations without inferring nearby districts."""
    texts: list[str] = []
    raw_locations: list[str] = []
    resolved: dict[str, str] = {}

    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            if not (getattr(elem, "source_refs", ()) or ()):
                continue
            elem_text = str(getattr(elem, "text", "") or "").strip()
            elem_areas = [
                str(area).strip()
                for area in (getattr(elem, "areas", ()) or ())
                if str(area).strip()
            ]
            if elem_text:
                texts.append(elem_text)
            raw_locations.extend(elem_areas)
            if resolver is not None:
                for location_text in (*elem_areas, elem_text):
                    resolved.update(_resolved_geographic_scopes(location_text, resolver))

    has_citywide_scope = any(_DIGEST_CITYWIDE_SCOPE_RE.search(text) for text in texts)
    if has_citywide_scope:
        resolved["citywide:explicit"] = "весь город"
    if resolved:
        return resolved

    # Keep the fallback conservative: exact profile-known places or raw source
    # locators remain their own scopes instead of being promoted to a district.
    raw_candidates = raw_locations + list(_extract_bundle_locations(texts))
    fallback: dict[str, str] = {}
    for location in raw_candidates:
        normalized = " ".join(location.casefold().split())
        if normalized:
            fallback[f"raw:{normalized}"] = location
    return fallback


def _build_topic_geographic_groups(
    cards: Sequence[Any], resolver: Any | None
) -> tuple[
    tuple[TopicGeographicGroup, ...],
    tuple[str, ...],
    tuple[TopicGeographicFact, ...],
]:
    groups: dict[str, tuple[str, list[str], list[TopicGeographicFact]]] = {}
    unresolved_story_ids: list[str] = []
    unresolved_facts: list[TopicGeographicFact] = []

    def add_resolved_fact(
        area_id: str,
        area_name: str,
        card_id: str,
        text: str,
        support_ids: Sequence[str],
    ) -> None:
        if area_id not in groups:
            groups[area_id] = (area_name, [], [])
        story_ids = groups[area_id][1]
        if card_id not in story_ids:
            story_ids.append(card_id)
        fact = TopicGeographicFact(
            story_id=card_id,
            text=text,
            support_ids=tuple(dict.fromkeys(str(ref) for ref in support_ids if str(ref).strip())),
        )
        if fact not in groups[area_id][2]:
            groups[area_id][2].append(fact)

    for card in cards:
        card_id = str(card.id)
        found_elements = False
        for elem_list in (
            getattr(card, "hard_facts", []) or [],
            getattr(card, "community_observations", []) or [],
            getattr(card, "useful_details", []) or [],
        ):
            for elem in elem_list:
                refs = tuple(
                    dict.fromkeys(
                        str(ref).strip()
                        for ref in (getattr(elem, "source_refs", ()) or ())
                        if str(ref).strip()
                    )
                )
                if not refs:
                    continue
                found_elements = True
                text = str(getattr(elem, "text", "") or "").strip()
                areas = tuple(
                    str(area).strip()
                    for area in (getattr(elem, "areas", ()) or ())
                    if str(area).strip()
                )
                scopes: dict[str, str] = {}
                if resolver is not None:
                    for locator in (*areas, text):
                        scopes.update(_resolved_geographic_scopes(locator, resolver))
                if _DIGEST_CITYWIDE_SCOPE_RE.search(text):
                    scopes["citywide:explicit"] = "весь город"
                if not scopes:
                    for locator in (*areas, *_extract_bundle_locations((text,))):
                        normalized = " ".join(locator.casefold().split())
                        if normalized:
                            scopes[f"raw:{normalized}"] = locator

                fact = TopicGeographicFact(
                    story_id=card_id,
                    text=text,
                    support_ids=refs,
                )
                if len(scopes) == 1:
                    area_id, area_name = next(iter(scopes.items()))
                    add_resolved_fact(area_id, area_name, card_id, text, refs)
                else:
                    unresolved_facts.append(fact)
                    if card_id not in unresolved_story_ids:
                        unresolved_story_ids.append(card_id)

        if not found_elements:
            scopes = _card_geographic_scopes(card, resolver)
            if len(scopes) == 1:
                area_id, area_name = next(iter(scopes.items()))
                add_resolved_fact(area_id, area_name, card_id, "", ())
            elif card_id not in unresolved_story_ids:
                unresolved_story_ids.append(card_id)

    geographic_groups = tuple(
        TopicGeographicGroup(
            area_id=area_id,
            area_name=area_name,
            story_ids=tuple(story_ids),
            facts=tuple(facts),
        )
        for area_id, (area_name, story_ids, facts) in groups.items()
    )
    return geographic_groups, tuple(unresolved_story_ids), tuple(unresolved_facts)


def _card_source_lineage(card: Any) -> set[str]:
    refs: set[str] = set()
    all_refs_fn = getattr(card, "all_source_refs", None)
    raw_refs: list[Any] = []
    if callable(all_refs_fn):
        raw_refs.extend(all_refs_fn())
    else:
        raw_refs.extend(getattr(card, "representative_source_refs", []) or [])
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            raw_refs.extend(getattr(elem, "source_refs", []) or [])

    for r in raw_refs:
        s = str(r).strip()
        if s and any(c.isdigit() for c in s):
            refs.add(s)
    return refs


def _card_service_families(card: Any) -> frozenset[str]:
    from src.domain.service_taxonomy import detect_service_families

    text_parts = [
        getattr(card, "topic", "") or "",
        getattr(card, "summary", "") or "",
        " ".join(getattr(card, "tags", []) or []),
    ]
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            text_parts.append(getattr(elem, "text", "") or "")
    return detect_service_families(" ".join(text_parts))


def _detect_presentation_kind(card: Any) -> str:
    text_parts = [
        getattr(card, "topic", "") or "",
        getattr(card, "summary", "") or "",
        " ".join(getattr(card, "tags", []) or []),
    ]
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            text_parts.append(getattr(elem, "text", "") or "")
    text = " ".join(text_parts).casefold()

    if re.search(
        r"\b(?:накопительн\w* бак|генератор|аккумулятор|павербанк|своими силами|частник|установка бак|альтернативн)\b",
        text,
    ):
        return "workaround"
    if re.search(r"\b(?:график|расписани|режим работы|по часам|веерн)\b", text):
        return "schedule"
    if re.search(
        r"\b(?:ремонт|восстановлен|бригад|аварийн\w* работ|водоканал проводит|чинят|устраняют)\b",
        text,
    ):
        return "repair"
    if re.search(
        r"\b(?:прилет|обстрел|поврежден|разрушен|взрыв|осколк|порыв|прорыв трубы|обрыв)\b",
        text,
    ):
        return "damage"
    if re.search(
        r"\b(?:заявил|сообщил|пообещал|администрация|власти|мэр|глава|официальн)\b",
        text,
    ):
        return "official_position"
    if re.search(r"\b(?:дтп|пожар|чп|несчастный случай)\b", text):
        return "incident"
    if re.search(
        r"\b(?:нет света|нет воды|света нет|воды нет|дали свет|дали воду|включили|отключили|появился|пропал|отсутствует|давление|есть вода|вода есть|свет есть)\b",
        text,
    ):
        return "status"
    return "other"


def _are_cards_merge_compatible(
    card_a: Any,
    card_b: Any,
    batch_stop_tags: set[str] | None = None,
    geographic_scopes_by_card: Mapping[str, set[str]] | None = None,
) -> bool:
    fams_a = _card_service_families(card_a)
    fams_b = _card_service_families(card_b)
    kind_a = _detect_presentation_kind(card_a)
    kind_b = _detect_presentation_kind(card_b)

    is_op_a = bool(fams_a) or getattr(card_a, "story_kind", "") == "operational_status"
    is_op_b = bool(fams_b) or getattr(card_b, "story_kind", "") == "operational_status"

    shared_lineage = bool(_card_source_lineage(card_a) & _card_source_lineage(card_b))
    areas_a = _card_areas(card_a)
    areas_b = _card_areas(card_b)
    shared_areas = bool(areas_a & areas_b)
    tags_a = _card_specific_tags(card_a, batch_stop_tags)
    tags_b = _card_specific_tags(card_b, batch_stop_tags)
    shared_tags = bool(tags_a & tags_b)

    geographic_scopes_by_card = geographic_scopes_by_card or {}
    geo_a = geographic_scopes_by_card.get(card_a.id, set())
    geo_b = geographic_scopes_by_card.get(card_b.id, set())
    if (geo_a or geo_b) and (len(geo_a) != 1 or geo_a != geo_b):
        return False

    if (kind_a == "workaround" or kind_b == "workaround") and kind_a != kind_b:
        return False

    if is_op_a or is_op_b:
        if not (is_op_a and is_op_b):
            return False

        if len(fams_a) > 1 or len(fams_b) > 1:
            if fams_a != fams_b:
                return False
            return (kind_a == kind_b) and (shared_areas or shared_lineage or shared_tags)

        if fams_a != fams_b:
            return False

        if kind_a == kind_b:
            return True

        if {kind_a, kind_b} <= {"status", "repair", "schedule", "other"}:
            return shared_areas or shared_lineage or shared_tags

        return False

    shared_specific_count = len(tags_a & tags_b)
    return (
        shared_specific_count >= 2
        or shared_lineage
        or (shared_specific_count >= 1 and shared_areas)
    )


def _compute_merge_groups(cards: Sequence[Any], *, edition_slug: str = "") -> dict[str, str]:
    if not cards:
        return {}

    batch_stop_tags = _compute_batch_frequent_tags(cards)
    resolver = _load_digest_geography_resolver(edition_slug)
    geographic_scopes_by_card = {
        card.id: set(_card_geographic_scopes(card, resolver)) for card in cards
    }

    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or ""
        by_rubric.setdefault(rid, []).append(c)

    merge_group_by_id: dict[str, str] = {}
    for _rid, r_cards in by_rubric.items():
        groups: list[list[Any]] = []
        for card in r_cards:
            placed = False
            for g in groups:
                if len(g) < 6 and all(
                    _are_cards_merge_compatible(
                        card, member, batch_stop_tags, geographic_scopes_by_card
                    )
                    for member in g
                ):
                    g.append(card)
                    placed = True
                    break
            if not placed:
                groups.append([card])

        for g in groups:
            if len(g) > 1:
                gid = f"merge:{min(c.id for c in g)}"
                for c in g:
                    merge_group_by_id[c.id] = gid
            else:
                c = g[0]
                merge_group_by_id[c.id] = c.id

    return merge_group_by_id


def _card_allowed_supports(card: Any) -> tuple[str, ...]:
    sups: list[str] = []
    if getattr(card, "summary", ""):
        sups.append(f"{card.id}:summary")
    for hf in getattr(card, "hard_facts", []) or []:
        for r in getattr(hf, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for co in getattr(card, "community_observations", []) or []:
        for r in getattr(co, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for ud in getattr(card, "useful_details", []) or []:
        for r in getattr(ud, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for obs in getattr(card, "operational_observations", []) or []:
        for r in getattr(obs, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    return tuple(sups)


def _canonical_service_family(card: Any) -> str | None:
    cat = (getattr(card, "category", "") or "").casefold()
    topic = (getattr(card, "topic", "") or "").casefold()
    tags = {str(t).casefold() for t in getattr(card, "tags", []) or []}
    tokens = set(re.findall(r"[a-zа-яё0-9]+", f"{cat} {topic} {' '.join(tags)}"))

    if {
        "electricity",
        "power",
        "blackout",
        "свет",
        "электроснабжение",
        "электроэнергия",
        "подстанция",
        "рэс",
    } & tokens:
        if not any(w in topic for w in ("услуги электрика", "электрик на дом", "частный электрик")):
            return "electricity"
    if {"water", "водоснабжение", "вода", "водоканал", "порыв"} & tokens:
        if not any(w in topic for w in ("услуги сантехника", "сантехник", "баки")):
            return "water"
    if {"gas", "газ", "газоснабжение", "горгаз"} & tokens:
        return "gas"
    if {"heating", "отопление", "теплосеть"} & tokens:
        return "heating"
    if {"telecom", "connectivity", "связь", "интернет", "провайдер", "мобильная связь"} & tokens:
        return "connectivity"
    if {"transport", "транспорт", "автобус", "маршрутка", "перевозки"} & tokens:
        return "transport"
    return None


def _canonical_topic_family(card: Any, rubric_id: str = "") -> tuple[str, str, str]:
    """Return (topic_key, topic_label, emoji) for a card based on its content and rubric."""
    cat = (getattr(card, "category", "") or "").casefold()
    topic = (getattr(card, "topic", "") or "").casefold()
    summary = (getattr(card, "summary", "") or "").casefold()
    tags = {str(t).casefold() for t in getattr(card, "tags", []) or []}
    text_corpus = f"{cat} {topic} {summary} {' '.join(tags)}"
    tokens = set(re.findall(r"[a-zа-яё0-9]+", text_corpus))

    # 1. Electricity / Электроснабжение
    if {
        "electricity",
        "power",
        "blackout",
        "свет",
        "электроснабжение",
        "электроэнергия",
        "электричество",
        "подстанция",
        "подстанции",
        "рэс",
        "горсвет",
        "напряжение",
        "киловольт",
    } & tokens:
        if not any(w in topic for w in ("услуги электрика", "электрик на дом", "частный электрик")):
            return ("electricity", "Электроснабжение", "⚡️")

    # 2. Water / Водоснабжение
    if {"water", "водоснабжение", "вода", "водоканал", "водопровод", "порыв", "водоводе"} & tokens:
        if not any(w in topic for w in ("услуги сантехника", "сантехник", "баки")):
            return ("water", "Водоснабжение", "💧")

    # 3. Gas / Газоснабжение
    if {"gas", "газ", "газоснабжение", "горгаз", "газопровод"} & tokens:
        return ("gas", "Газоснабжение", "💨")

    # 4. Heating / Отопление
    if {"heating", "отопление", "теплосеть", "котельная", "теплоснабжение"} & tokens:
        return ("heating", "Отопление", "♨️")

    # 5. Telecom / Connectivity / Internet
    if {
        "telecom",
        "connectivity",
        "связь",
        "интернет",
        "провайдер",
        "мобильная",
        "сотовая",
        "вышка",
        "оптика",
        "миртелеком",
        "онэт",
    } & tokens:
        return ("connectivity", "Связь и интернет", "🌐")

    # 6. Safety / Strikes / Air Defense
    if {
        "взрыв",
        "взрывы",
        "обстрел",
        "обстрелы",
        "пво",
        "бпла",
        "дрон",
        "дроны",
        "прилет",
        "прилеты",
        "сирена",
        "тревога",
        "хлопок",
        "хлопки",
        "атеш",
        "фаб",
        "каб",
        "бомба",
        "авиабомба",
        "ракета",
        "мина",
        "снаряд",
        "атака",
        "атаковал",
        "атаковали",
        "пострадали",
        "ранены",
        "погибли",
        "авиаудар",
        "удар",
        "осколочн",
        "контузи",
    } & tokens:
        return ("strikes", "Безопасность и происшествия", "💥")

    # 7. Fires / Emergency
    if {"пожар", "пожары", "возгорание", "мчс", "спасатели"} & tokens:
        return ("fire", "Пожары и происшествия", "🔥")

    # 8. Transport & Roads
    if {
        "transport",
        "транспорт",
        "автобус",
        "автобусы",
        "маршрутка",
        "маршрут",
        "перевозки",
        "дорога",
        "дороги",
        "чонгар",
        "перекрытие",
        "трасса",
    } & tokens:
        return ("transport", "Транспорт и дороги", "🚌")

    # 9. Civic services & Documents
    if {
        "паспорт",
        "паспортный",
        "мфц",
        "гибдд",
        "гаи",
        "пенсионный",
        "госуслуги",
        "нотариус",
        "доверенность",
        "прописка",
        "документы",
        "заявление",
        "вытрезвитель",
    } & tokens:
        return ("civic_services", "Городские службы и документы", "🏛")

    # 10. Banks & Cash
    if {
        "банк",
        "банки",
        "банкомат",
        "банкоматы",
        "наличные",
        "сбер",
        "сбербанк",
        "сбол",
        "псб",
        "мера",
        "дельмар",
        "пнкб",
        "карта",
        "перерасчет",
    } & tokens or bool(
        re.search(r"\b(?:банк\w*|сбер\w*|псб\b|пнкб\b|сбол\b|наличн\w*)\b", text_corpus)
    ):
        return ("banking", "Банки и наличные", "🏧")

    # 11. Medicine & Health
    if {
        "больница",
        "больницы",
        "поликлиника",
        "аптека",
        "аптеки",
        "врач",
        "врачи",
        "медицина",
        "медицинский",
        "флюорография",
        "визант",
        "донор",
        "доноры",
        "кровь",
        "донорство",
    } & tokens:
        return ("health", "Медицина и здоровье", "🏥")

    # 12. Education & Culture
    if {
        "школа",
        "школы",
        "детсад",
        "детсады",
        "училище",
        "вуз",
        "образование",
        "спорт",
        "культура",
        "музей",
    } & tokens:
        return ("education", "Образование и культура", "🎓")

    # 13. Social help & benefits
    if {
        "выплата",
        "выплаты",
        "пособие",
        "пособия",
        "гуманитарная",
        "гумпомощь",
        "помощь",
        "льготы",
        "социальная",
    } & tokens:
        return ("social", "Социальная помощь", "🏢")

    # 14. Logistics / Marketplaces & Deliveries
    if {
        "ozon",
        "озон",
        "wildberries",
        "вайлдберриз",
        "доставка",
        "посылка",
        "посылки",
        "сдэк",
        "маркетплейс",
        "пвз",
    } & tokens:
        return ("logistics", "Доставка и маркетплейсы", "📦")

    # 15. Economy & Business
    if {
        "магазин",
        "магазины",
        "рынок",
        "цена",
        "цены",
        "торговля",
        "предприятие",
        "бизнес",
    } & tokens:
        return ("economy", "Торговля и экономика", "💼")

    # Fallback to rubric ID
    r = (getattr(card, "rubric_id", "") or rubric_id or cat).casefold()
    if "safety" in r or "безопасн" in r:
        return ("safety_general", "Безопасность", "🛡")
    if "infrastructure" in r or "utilities" in r or "коммун" in r or "жкх" in r:
        return ("infrastructure_general", "Коммунальная сфера", "⚡️")
    if "comm" in r or "связ" in r:
        return ("connectivity_general", "Связь и интернет", "🌐")
    if "mobil" in r or "transport" in r or "трансп" in r:
        return ("mobility_general", "Транспорт и дороги", "🚌")
    if "health" in r or "медиц" in r:
        return ("health_general", "Медицина и здоровье", "🏥")
    if "civic" in r or "служб" in r:
        return ("civic_general", "Городские службы", "🏛")
    if "educ" in r or "образ" in r:
        return ("education_general", "Образование и культура", "🎓")
    if "soc" in r or "соц" in r:
        return ("social_general", "Социальная помощь", "🏢")
    if "econ" in r or "эконом" in r:
        return ("economy_general", "Экономика и бизнес", "💼")
    if "focus" in r or "фокус" in r:
        return ("focus_general", "В фокусе внимания", "🎯")

    return ("other_general", "Городские события", "📌")


_KNOWN_LOCATIONS = (
    # Районы и микрорайоны
    "Центр",
    "Нагорная часть",
    "Нагорный район",
    "Колония",
    "Слободка",
    "Лиски",
    "Азмол",
    "АКЗ",
    "Пески",
    "Дальняя Коса",
    "Ближняя Коса",
    "Коса",
    "РТС",
    "Военный городок",
    "8 Марта",
    "Черёмушки",
    "Стекловолокно",
    # Улицы и проспекты
    "проспект Победы",
    "проспект Труда",
    "улица Гайдара",
    "улица Кирово",
    "улица Орджоникидзе",
    "улица Руденко",
    "улица Правды",
    "улица Свободы",
    "улица Шевченко",
    "улица Дюмина",
    "улица Крупской",
    "улица Морозова",
    "улица Нагорная",
    "улица Пионерская",
    "улица Тверская",
    "улица Карла Маркса",
    "улица Франко",
    "улица Ростовская",
    "улица Тищенко",
    "Мелитопольское шоссе",
    "Восточный проспект",
    # Ориентиры
    "район 19 училища",
    "19 училище",
    "район Пакета",
    "район Меры",
    "район Сбера",
    "самолёт",
    "вокзал",
    "водоканал",
    "ПНС Димитрова",
    "ТЦ «Дель Мар»",
)


def _extract_bundle_locations(texts: Sequence[str]) -> tuple[str, ...]:
    """Extract and deduplicate known locations from a collection of texts."""
    found: list[str] = []
    combined = " ".join(texts)
    combined_l = combined.casefold()
    for loc in _KNOWN_LOCATIONS:
        if loc.casefold() in combined_l and loc not in found:
            found.append(loc)
    for match in re.finditer(r"\b(?:ул\.|улице|улицы|на|по)\s+([А-Я][а-я]+(?:\s+\d+)?)", combined):
        val = match.group(1).strip()
        if len(val) >= 4 and val not in found and not val.startswith(("Бердян", "Город", "Район")):
            found.append(val)
    return tuple(found)


_FACT_INTERNAL_METADATA_RE = re.compile(
    r"\s*\[[^\]\n]+\]\s*(?:" r"(?:AVAILABLE|UNAVAILABLE|DEGRADED|CONFLICTING)\s*[—-]\s*" r")?",
    re.IGNORECASE,
)
_FACT_QUESTION_RE = re.compile(
    r"(?:\?|\b(?:"
    r"интересу(?:ется|ются)|спрашива(?:ет|ют)|"
    r"выясня(?:ет|ют)|узна(?:ет|ют)|подскажите|кто\s+знает|"
    r"где\s+(?:купить|найти|приобрести|заказать)|"
    r"как\s+(?:доехать|проехать|попасть|добраться)|"
    r"посоветуйте|ищу\s+(?:где|магазин|мастера|работу|квартиру)"
    r")\b)",
    re.IGNORECASE,
)
_FACT_META_OR_ADVICE_RE = re.compile(
    r"\b(?:подробности\s+(?:не\s+)?уточняются|детали\s+не\s+раскрыты|"
    r"уточняйте\s+в\s+официальных\s+источниках|"
    r"совет(?:ую|ует|уют)|рекоменду(?:ется|ют|ет)|"
    r"не\s+(?:обстреливайте|появляйтесь|ходите|звоните)|"
    r"сообщается\s+о\s+(?:событии|ситуации)|"
    r"сообщени[ея]\s+(?:сообщества\s+о\s+выполнении\s+работ|о\s+(?:событии|ситуации))|"
    r"жител(?:и|ь)\s+(?:интересу(?:ются|ется)|спрашива(?:ют|ет)|зада(?:ют|ёт)\s+вопрос)|"
    r"перекличк[а-я]*|в\s+перекличк[а-я]*|"
    r"что[- ]то\s+(?:произошло|отключилось|случилось))\b",
    re.IGNORECASE,
)

_FACT_DIRECTORY_OR_PROMO_RE = re.compile(
    r"\b(?:"
    r"ежедневн\w*\s+(?:автобусн\w*\s+)?(?:рейс\w*|пассажирск\w*\s+перевоз\w*)|"
    r"атмосфер\w*\s+красот\w*|маленьк\w*\s+леди|"
    r"при[её]м\s+автомобил\w*\s+в\s+разбор|"
    r"задава(?:ть|йте)\s+вопрос\w*\s+в\s+личн\w*\s+сообщени\w*|"
    r"сообщени[ея]\s+о\s+контактн\w*\s+телефон\w*|"
    r"вступа(?:йте|ть)\s+в\b|подписыва(?:йтесь|ться)|переходи(?:те|ть)\s+по\s+ссылке|"
    r"канал\s+max\b|чат\s+max\b|групп[а-я]\s+max\b"
    r")\b",
    re.IGNORECASE,
)

_OPERATIONAL_SERVICE_KW_RE = re.compile(
    r"\b(?:"
    r"свет\w*|электр\w*|напряжен\w*|вольт\w*|обесточ\w*|отключ\w*|генератор\w*|"
    r"вод\w*|водоснаб\w*|водоканал\w*|напор\w*|порыв\w*|скважин\w*|труб\w*|"
    r"газ\w*|отоплен\w*|котельн\w*|тепл\w*|"
    r"связ\w*|интернет\w*|провайдер\w*|вышк\w*|сигнал\w*|"
    r"маршрут\w*|автобус\w*|транспорт\w*|рейс\w*|проезд\w*|дорог\w*|"
    r"банк\w*|банкомат\w*|почт\w*|пенсион\w*|мфц|больниц\w*|поликлиник\w*|аптек\w*"
    r")\b",
    re.IGNORECASE,
)


def _is_fact_noise_sentence(text: str) -> bool:
    """Return whether a sentence is chat metadata, a question, or unsolicited advice."""
    t_l = text.casefold().strip()
    if not t_l:
        return True
    if _FACT_QUESTION_RE.search(t_l):
        return True
    if _FACT_META_OR_ADVICE_RE.search(t_l):
        return True
    if _FACT_DIRECTORY_OR_PROMO_RE.search(t_l):
        return True
    from src.publication.story_quality import _NON_EDITORIAL_PAYLOAD_RE

    if _NON_EDITORIAL_PAYLOAD_RE.search(t_l):
        return True
    if any(
        marker in t_l
        for marker in (
            "сообщения с эмодзи",
            "сообщение сообщества о выполнении работ",
            "настраиваются на позитив",
            "эмоциональное сообщение",
            "уровень пройти не может",
            "не может пройти через кпп",
            "радиусе",
            "точнее не работает вообще",
            "всем мира",
            "город спит и пусть",
            "ледян",
            "пальцы рук онемели",
            "издевательств",
            "ваши улицы не касается",
            "вечность будут обрезать",
            "плохиши пилили",
            "тишина полнейшая",
            "болгаркой и дрелью",
            "вилка горит",
            "слабый для бойлера",
            "кто то в курсе",
            "кто-то в курсе",
            "непонятно как они распределяют",
            "суток не прошло",
        )
    ):
        return True
    if re.match(r"^(?:один|кто-то|кто то)\s+(?:отвечает|говорит|пишет)\b", t_l):
        return True
    return False


def _clean_fact_sentence(text: str) -> str:
    """Sanitize a raw sentence from chat noise, questions, and internal metadata."""
    t = _FACT_INTERNAL_METADATA_RE.sub(" ", text or "").strip()
    if not t:
        return ""
    t = re.sub(r"^(?:да|ну\s+да|а)\s*,\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"^(?:ах\s+да\s*,?\s*)?(?:вроде|кажется)\s*,?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Strip conversational and chat-source prefixes
    t = re.sub(
        r"^(?:(?:в\s+)?(?:городском\s+|местном\s+|районном\s+)?чате(?:\s+[а-яёA-Za-z-]+)?\s+сообща(?:ют|ется)|"
        r"сообщение\s+от\s+(?:местного\s+)?жителя|по\s+сообщениям\s+жителей|"
        r"по\s+сообщению\s+(?:местных\s+)?жител(?:ей|я)|"
        r"(?:местн(?:ый|ая|ые)\s+)?(?:жител(?:и|ь)|жительниц(?:а|ы))"
        r"(?:\s+[а-яё-]+){0,2}\s+сообща(?:ют|ет)|"
        r"по\s+словам\s+горожан)[\s,:]*(?:что\s+)?",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    t = re.sub(r"^(?:что|а)\s+", "", t, flags=re.IGNORECASE).strip()

    # Never reveal internal collection mechanics or chat sources to readers
    t = re.sub(
        r"\b(?:в\s+)?(?:городском\s+|местном\s+|районном\s+)?чате(?:\s+[а-яёA-Za-z-]+)?\s+сообща(?:ют|ется)[\s,:]*(?:что\s+)?",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\bв\s+(?:городском\s+|местном\s+|районном\s+)?чате(?:\s+[а-яёA-Za-z-]+)?\b",
        "в городе",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\bучастники?\s+чата\b", "жители", t, flags=re.IGNORECASE)
    t = re.sub(r"\bсообщени[ея]\s+в\s+чате\b", "сообщения", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(\s*со\s+слов\s+[^)]+\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r",\s*по\s+словам\s+жителя\b", "", t, flags=re.IGNORECASE)

    # Keep concrete sentences from a mixed summary while dropping appended
    # questions, chat reactions, and advice boilerplate.
    from src.publication.digest_quality_diagnostics import _PROFANITY_AND_ABUSE_RE

    sentences = re.split(r"(?<=[.!?])\s+", t)
    kept = []
    for sentence in sentences:
        s_clean = sentence.strip()
        if not s_clean:
            continue
        if _is_fact_noise_sentence(s_clean):
            continue
        if _PROFANITY_AND_ABUSE_RE.search(s_clean):
            continue
        # Strip reply syntax and machine tokens
        s_clean = re.sub(
            r"\s*\((?:в\s+ответ\s+на|в\s+ответ)\b.*$", "", s_clean, flags=re.IGNORECASE
        ).strip()
        s_clean = re.sub(
            r"\b(?:AVAILABLE|UNAVAILABLE|STATUS_\w+|service_access)\b\s*(?:—|-)?\s*",
            "",
            s_clean,
            flags=re.IGNORECASE,
        ).strip()
        if s_clean:
            kept.append(s_clean)
    t = " ".join(kept).strip()
    if not t:
        return ""
    t = re.sub(
        r"\s*(?:,|;)?\s*(?:подробности|детали)\s+(?:не\s+)?уточняются\.?$",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    # Clean repetitive temporal replay chains that cause TEMPORAL_REPLAY_CHAIN diagnostics
    t = re.sub(
        r"\bтакже\s+сообщается,\s+что\s+ранее\b", "ранее сообщалось, что", t, flags=re.IGNORECASE
    )
    t = re.sub(r"\bранее\s+также\b", "ранее", t, flags=re.IGNORECASE)
    t = re.sub(r"\bтакже\s+ранее\b", "ранее", t, flags=re.IGNORECASE)
    # Clean leading punctuation and trailing quote/parenthesis artifacts
    t = t.lstrip(" ,.-:;—")
    t = re.sub(r"[\"')\]]+\.?$", "", t).strip()
    # Clean unclosed parenthesis if any
    if "(" in t and ")" not in t:
        t = re.sub(r"\s*\([^\)]*$", "", t).strip()
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t).strip()
    if t:
        t = t[:1].upper() + t[1:]
    return t.rstrip(". ") + "."


def _is_usable_fact_line(text: str) -> bool:
    """Filter out chat noise, resident questions, classified ads, lost & found, and meta comments."""
    raw_l = text.casefold()
    from src.publication.story_quality import is_non_editorial_fact

    if is_non_editorial_fact(raw_l):
        return False
    if any(
        k in raw_l
        for k in (
            "in_reply_to",
            "in reply to",
            "обсуждение в чате",
            "обсуждения в чате",
            "перекличка в чате",
        )
    ):
        return False
    cleaned = _clean_fact_sentence(text)
    if not cleaned or len(cleaned) < 12:
        return False
    t = cleaned
    words = t.split()
    if len(words) < 3:
        return False
    t_l = t.casefold()

    substantive_text = re.sub(r"\(in_reply_to:[^)]*\)", "", t_l).strip()
    if not re.search(r"[a-zа-яё0-9]{4,}", substantive_text):
        return False

    if re.search(
        r"\b(?:"
        r"срамот\w*|"
        r"боюсь\s+сглаз\w*|"
        r"како\w*\s+круглосуточ\w*|"
        r"не\s+балу\w*|"
        r"в\s+итоге\s+вся\s+гора\s+с\s+водой|"
        r"плохо\s+голосовал\w*|"
        r"снова\s+цивилизаци\w*\s+покинул\w*|"
        r"не\s+хваста\w*\s+удач\w*|"
        r"каменн\w*\s+пещер\w*|"
        r"не\s*долго\s+музыка\s+играла|"
        r"музыка\s+играла|"
        r"видимо\s+нет|"
        r"где[- ]то\s+есть\b.*?в\s+ответ|"
        r"кому\s+включали\b|"
        r"света?\s+ушла|"
        r"нет\s+света\s+нету|"
        r"нету\s+\d+\s+дн\w*|"
        r"нашим\s+животн\w*|"
        r"ветклиник\w*|"
        r"ветеринар\w*|"
        r"конкурирующ\w*|"
        r"второй\s+день\s+решают|"
        r"ремонтировать\s+или\s+новый|"
        r"может\s+где[- ]?то\s+и\s+есть|"
        r"с\s+первыми\s+петух\w*|"
        r"подстроит\w*"
        r")\b",
        t_l,
    ):
        return False

    from src.publication.story_quality import is_non_editorial_fact

    if is_non_editorial_fact(t_l):
        return False

    # Short chat reactions can acquire a predicate during Event-First
    # analysis, but they are not useful city-life facts on their own.
    if re.search(
        r"\b(?:не\s+тихо|движух\w*|завтра\s*[-—]?\s*день\s+город\w*)\b",
        t_l,
    ):
        return False
    if re.search(r"\bгромк\w*\s+зву\w*\b", t_l) and not re.search(
        r"\b(?:взрыв\w*|стрел\w*|обстрел\w*|дрон\w*|пво|сирен\w*|пожар\w*|авари\w*)\b",
        t_l,
    ):
        return False
    if re.search(
        r"\b(?:источник|причин\w*)\b[^.!?]{0,50}\b(?:уточн\w*|неизвестн\w*)\b",
        t_l,
    ) and not re.search(
        r"\b(?:взрыв\w*|стрел\w*|обстрел\w*|дрон\w*|пво|сирен\w*|пожар\w*|авари\w*)\b",
        t_l,
    ):
        return False

    # Contact-card lines are directory payload, not scan-first digest facts.
    if re.search(
        r"(?:контактн\w*\s+телефон|телефон\s+для\s+вызов\w*|номер\s+телефон|\+?\d[\d\s().-]{7,})",
        t_l,
    ):
        return False

    # A reply such as "У меня на улице X не работает" has no identified
    # service or event.  A bare predicate must not make it publishable.
    if re.search(r"\bне\s+работа\w*\b", t_l) and not re.search(
        r"\b(?:свет\w*|электр\w*|вод\w*|газ\w*|отоплен\w*|интернет\w*|связ\w*|светофор\w*|автобус\w*|маршрут\w*|аптек\w*|врач\w*)\b",
        t_l,
    ):
        return False

    # 1. Questions
    if (
        _FACT_QUESTION_RE.search(t_l)
        or _FACT_META_OR_ADVICE_RE.search(t_l)
        or _FACT_DIRECTORY_OR_PROMO_RE.search(t_l)
    ):
        return False

    # 2. Lost & found animals / personal items / lost belongings
    if any(
        k in t_l
        for k in (
            "пропал кот",
            "пропала кошка",
            "пропала собака",
            "потерялась собака",
            "потерялся пес",
            "потерялся пёс",
            "нашли собачку",
            "нашли собаку",
            "нашлась собака",
            "найдена собака",
            "найденная собака",
            "найденной собаке",
            "найденную собаку",
            "найденного кота",
            "найденной кошке",
            "найденный кот",
            "нашли щенка",
            "помогите найти хозяина",
            "ищут хозяина",
            "ищет хозяина",
            "ищем хозяина",
            "поиск хозяев",
            "поиски хозяина",
            "оставил рюкзак",
            "оставили рюкзак",
            "потерял рюкзак",
            "потеряли рюкзак",
            "нашел рюкзак",
            "нашёл рюкзак",
            "найден рюкзак",
            "потерян рюкзак",
            "потерял ключи",
            "потерялись документы",
            "найдены ключи",
            "найден кошелек",
            "найден кошелёк",
            "забыли в автобусе",
            "забыл в автобусе",
            "нашли в автобусе",
            "кто-то нашёл",
            "кто-то нашел",
            "кто то нашел",
            "кто то нашёл",
            "найден паспорт",
            "утерян паспорт",
            "утеряны документы",
            "найдены документы",
        )
    ):
        return False

    # Personal dialogue about arranging something is not a city-life update
    # unless it names a concrete service or event.  These lines commonly leak
    # from a chat thread into an otherwise valid connectivity bundle.
    if re.search(r"\bпоехал\w*\b[^.!?]{0,80}\bоформил\w*\b", t_l) and not re.search(
        r"\b(?:интернет|связь|провайдер|заявк\w*|водоканал|электр\w*|вода|свет)\b",
        t_l,
    ):
        return False
    if re.search(r"\bс\s+приятел\w*\b|\bмы\s+не\s+звонил\w*\b", t_l) and not re.search(
        r"\b(?:интернет|связь|провайдер|заявк\w*|водоканал|электр\w*|вода|свет)\b",
        t_l,
    ):
        return False

    # 3. Unconditional chat dialogue fragments & non-factual community openers
    if any(
        k in t_l
        for k in (
            "приглашает посетить",
            "приглашают посетить",
            "дозванивается",
            "дозваниваюсь",
        )
    ):
        return False

    if any(
        t_l.startswith(prefix)
        for prefix in (
            "внизу, район",
            "внизу район",
            "житель приглашает",
            "жители приглашают",
            "сообщение от местного жителя",
            "сообщение от жителя",
            "посетите, а то",
            "только особо не рассчитывайте",
            "самостоятельно будет много быстрее",
            "похоже, забыли",
            "раза с 15",
            "раза с 10",
            "с праздником",
            "доброе утро",
            "добрый вечер",
            "спокойной ночи",
            "всем привет",
        )
    ):
        return False

    # 4. Conversational interjections & colloquial dialogue starts. A colloquial
    # lead-in is not enough to discard a report: short local observations often
    # begin with "у нас" or "да, с ...". Keep them when the line still carries
    # a concrete service/event signal or a date/number.
    concrete_signal = bool(re.search(r"\d", t_l)) or any(
        marker in t_l
        for marker in (
            "свет",
            "электр",
            "напряж",
            "вода",
            "водопровод",
            "газ",
            "интернет",
            "связь",
            "отоплен",
            "автобус",
            "маршрут",
            "дорог",
            "аптек",
            "больниц",
            "врач",
            "выплат",
            "ремонт",
            "авар",
            "пожар",
            "взрыв",
            "дрон",
            "пво",
            "перерасч",
        )
    )
    if (
        any(
            t_l.startswith(prefix)
            for prefix in (
                "та уже",
                "уже тихо",
                "да брат",
                "та не",
                "та вроде",
                "ну да",
                "вот именно",
                "короче",
                "кстати",
                "да, с ",
                "нет, с ",
                "а у нас",
                "у нас тоже",
                "и у меня",
                "вчера будет",
                "а завтра день",
                "а завтра — день",
            )
        )
        and not concrete_signal
    ):
        return False

    # 5. Commercial classifieds, private sales, job postings
    if any(
        k in t_l
        for k in (
            "куплю",
            "продам",
            "цена от",
            "позвонить по номеру",
            "купить стекло",
            "продается",
            "сдам",
            "сниму",
            "требуются",
            "вакансия",
            "в личные сообщения",
            "писать в лс",
            "писать в личку",
            "стоимости перекопки",
            "за сотку",
            "напишите в лс",
            "написать в лс",
            "вступайте",
            "подписывайтесь",
            "переходите в канал",
            "канал max",
            "чат max",
        )
    ):
        return False

    # 6. Emojis / technical metadata / chat profanity / abuse
    if "эмодзи" in t_l or "смайлик" in t_l or "стикер" in t_l:
        return False
    from src.publication.digest_quality_diagnostics import (
        _MALFORMED_CHAT_SYNTAX_RE,
        _PROFANITY_AND_ABUSE_RE,
    )

    if _PROFANITY_AND_ABUSE_RE.search(t_l):
        return False
    if _MALFORMED_CHAT_SYNTAX_RE.search(t_l):
        return False
    if "available" in t_l or "unavailable" in t_l or "service_access" in t_l:
        return False
    if any(
        k in t_l
        for k in (
            "чо за фигня",
            "идите нах",
            "кинули не только вас",
            "жесть",
            "херня",
        )
    ):
        return False

    # 7. Meta-commentary without concrete facts
    if "подробности уточняются" in t_l and len(words) <= 8:
        return False
    if "детали не раскрыты" in t_l:
        return False
    if "без конкретных деталей" in t_l:
        return False
    if "эмоциональное сообщение" in t_l:
        return False
    if "обсуждение в чате" in t_l or "обсуждения в чате" in t_l:
        return False
    if "реклама" in t_l or "рекламы" in t_l:
        return False

    return True


def _extract_distinctive_entities(text: str) -> set[str]:
    """Extract distinctive anchor entities, quoted names, Latin brands, and specific facilities."""
    entities: set[str] = set()
    if not text:
        return entities
    t_l = text.casefold()
    # 1. Quoted names: «...» or "..."
    for match in re.findall(r'[«"“]([^»"”]{2,30})[»"”]', text):
        clean_q = re.sub(r"[^\w\s-]", "", match).strip().casefold()
        if clean_q in {"семья", "улей", "экватор", "екватор"}:
            entities.add("экватор")
        elif len(clean_q) >= 3 and clean_q not in {
            "город",
            "бердянск",
            "новости",
            "внимание",
            "справка",
            "информация",
            "официально",
        }:
            entities.add(clean_q)
    # 2. Latin brands/words (e.g. ozon, wildberries, etc.)
    for latin in re.findall(r"\b[a-z]{3,}\b", t_l):
        if latin not in {"the", "and", "for", "com", "ru", "html", "http", "https"}:
            entities.add(latin)
    # 3. Specific Cyrillic facility / brand anchor stems
    _ANCHOR_PATTERNS = {
        r"\b[еэ]кватор\w*\b": "экватор",
        r'\b(?:магазин|супермаркет|склад)\w*\s+[«"“]?семь[яеи]': "экватор",
        r'[«"“]?семь[яеи][»"”]?\b[^.!?]{0,40}\b(?:склад|магазин|маркет|супермаркет|помещени\w*)': "экватор",
        r'\bпомещени[ея]\s+[«"“]?улей': "экватор",
        r"\b(?:прил[её]т|пожар|удар|дрон\w*)\b[^.!?]{0,40}\b(?:супермаркет\w*|маркет\w*|тц|[еэ]кватор\w*)": "экватор",
        r"\b(?:супермаркет\w*|маркет\w*|тц|[еэ]кватор\w*)\b[^.!?]{0,40}\b(?:прил[её]т|пожар|удар|сгорел\w*|разруш\w*|дрон\w*)": "экватор",
        r"\bозон\w*\b": "ozon",
        r"\bвайлдберриз\w*\b": "wildberries",
        r"\bсбер\w*\b": "сбер",
        r"\bпсб\b": "псб",
        r"\bпенсионн\w*\s+фонд\w*\b": "пенсионный_фонд",
        r"\bшереметьев\w*\b": "шереметьево",
        r"\bчонгар\w*\b": "чонгар",
        r"\bводоканал\w*\b": "водоканал",
        r"\bгоргаз\w*\b": "горгаз",
        r"\bгорсвет\w*\b": "горсвет",
        r"\bтеплосет\w*\b": "теплосеть",
    }
    for pat, canonical in _ANCHOR_PATTERNS.items():
        if re.search(pat, t_l):
            entities.add(canonical)
    return entities


def build_thematic_topic_bundles(
    cards: Sequence[Any],
    *,
    evidence: Mapping[str, Any] | None = None,
    required_facts: Sequence[RequiredDigestFact] = (),
    rubric_id: str | None = None,
    edition_slug: str = "",
) -> tuple[TopicBundle, ...]:
    """Group story cards within rubrics into cohesive, scan-first Thematic Topic Bundles."""
    if not cards:
        return ()

    geography_resolver = _load_digest_geography_resolver(edition_slug)

    if rubric_id is not None:
        by_rubric: dict[str, list[Any]] = {rubric_id: list(cards)}
    else:
        by_rubric = {}
        for c in cards:
            c_full_text = f"{getattr(c, 'topic', '')} {getattr(c, 'summary', '')}"
            if "экватор" in _extract_distinctive_entities(c_full_text):
                rid = "safety"
            else:
                rid = getattr(c, "rubric_id", "") or "other"
            by_rubric.setdefault(rid, []).append(c)

    bundles: list[TopicBundle] = []
    bundle_counter = 0

    for rid, r_cards in by_rubric.items():
        groups_by_key: dict[str, list[Any]] = {}
        # Count cards per topic family within this rubric
        family_counts: dict[str, int] = {}
        for c in r_cards:
            tk, _, _ = _canonical_topic_family(c, rid)
            family_counts[tk] = family_counts.get(tk, 0) + 1

        _PRIORITY_ANCHORS: tuple[str, ...] = ("экватор",)
        if rid == "economy":
            _PRIORITY_ANCHORS = ("ozon", "wildberries")
        _UNIFIED_THEMATIC_TOPICS = frozenset(
            {
                "electricity",
                "water",
                "gas",
                "heating",
                "connectivity",
                "strikes",
                "fire",
                "safety",
                "logistics",
                "banking",
                "transport",
                "health",
                "civic_services",
                "social",
                "education",
                "economy",
            }
        )
        entities_by_group: dict[str, set[str]] = {}
        geographic_scopes_by_card = {
            card.id: _card_geographic_scopes(card, geography_resolver) for card in r_cards
        }
        for c in r_cards:
            t_key, _, _ = _canonical_topic_family(c, rid)
            # Rubric affinity: align cross-rubric card contamination with the rubric's purpose
            if rid == "communications" and t_key in ("electricity", "connectivity_general"):
                t_key = "connectivity"
            elif rid == "mobility" and t_key in ("connectivity", "mobility_general"):
                t_key = "transport"
            elif rid == "health" and t_key in ("health_general",):
                t_key = "health"
            elif rid == "society" and t_key in ("civic_services", "social_general"):
                t_key = "social"
            elif rid == "infrastructure" and t_key in ("infrastructure_general",):
                t_key = "electricity"
            elif rid == "safety" and t_key in ("electricity", "water", "safety_general"):
                t_key = "strikes"
            elif rid == "economy" and t_key in ("economy_general", "logistics_general", "trade"):
                t_key = "economy"
            elif rid == "civic_services" and t_key in ("civic_general",):
                t_key = "civic_services"
            elif rid == "education" and t_key in ("education_general",):
                t_key = "education"

            c_full_text = f"{getattr(c, 'topic', '')} {getattr(c, 'summary', '')}"
            c_entities = _extract_distinctive_entities(c_full_text)
            p_anchors = [a for a in _PRIORITY_ANCHORS if a in c_entities] if c_entities else []

            if p_anchors:
                main_entity = p_anchors[0]
                if main_entity == "экватор":
                    group_key = "strikes:entity:экватор"
                else:
                    group_key = f"{t_key}:entity:{main_entity}"
                entities_by_group.setdefault(group_key, set()).update(c_entities)
            elif t_key in _UNIFIED_THEMATIC_TOPICS:
                group_key = t_key
            elif rid == "other" and len(r_cards) > 3:
                group_key = "other:city_life"
            else:
                raw_fingerprint = (
                    _clean_fact_sentence(getattr(c, "summary", "") or "")
                    or _clean_fact_sentence(getattr(c, "topic", "") or "")
                ).casefold()
                fingerprint = re.sub(r"\W+", "_", raw_fingerprint).strip("_")[:96]
                group_key = f"{t_key}:fact:{fingerprint}" if fingerprint else f"{t_key}:{c.id}"

            # Geography is a hard composition boundary. A writer bundle must
            # never combine otherwise-related service reports from distinct
            # canonical areas under one district label. Unknown and ambiguous
            # locations also stay apart from confidently resolved areas.
            geographic_scopes = geographic_scopes_by_card.get(c.id, {})
            if len(geographic_scopes) == 1:
                geography_key = next(iter(geographic_scopes))
            elif geographic_scopes:
                geography_key = f"ambiguous:{c.id}"
            else:
                geography_key = "unlocated"
            group_key = f"{group_key}:geo:{geography_key}"

            groups_by_key.setdefault(group_key, []).append(c)

        rubric_bundles: list[TopicBundle] = []
        for group_key, g_cards in groups_by_key.items():
            bundle_counter += 1
            sample_card = g_cards[0]
            t_key, t_label, t_emoji = _canonical_topic_family(sample_card, rid)
            # If group has multiple cards with different families, prioritize strikes/fire
            for c in g_cards:
                cand_k, cand_l, cand_e = _canonical_topic_family(c, rid)
                if cand_k in ("strikes", "fire") and t_key not in ("strikes", "fire"):
                    t_key, t_label, t_emoji = cand_k, cand_l, cand_e
                    break
            if "экватор" in group_key or any(
                "экватор" in _extract_distinctive_entities(f"{c.topic} {c.summary}")
                for c in g_cards
            ):
                t_key = "strikes"
                t_label = "Инцидент в ТРЦ «Экватор»"
                t_emoji = "💥"
            story_ids = tuple(c.id for c in g_cards)

            # Collect allowed supports
            all_sups: list[str] = []
            for c in g_cards:
                for s in _card_allowed_supports(c):
                    if s not in all_sups:
                        all_sups.append(s)
            if evidence:
                for eid, evi in evidence.items():
                    evi_sid = getattr(evi, "story_id", None)
                    if (
                        evi_sid and (str(evi_sid) in story_ids or f"story:{evi_sid}" in story_ids)
                    ) or eid.startswith(tuple(f"{sid}:" for sid in story_ids)):
                        if (
                            getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                            and eid not in all_sups
                        ):
                            all_sups.append(eid)

            # Collect texts for location extraction and fact ledger
            raw_texts: list[str] = []
            fact_candidates: list[str] = []
            for c in g_cards:
                if c.topic:
                    raw_texts.append(c.topic)
                if c.summary:
                    raw_texts.append(c.summary)
                    cleaned_summary = _clean_fact_sentence(c.summary)
                    for atom in re.split(r"(?<=[.!?])\s+", cleaned_summary):
                        if _is_usable_fact_line(atom):
                            fact_candidates.append(_clean_fact_sentence(atom))
                for hf in getattr(c, "hard_facts", []) or []:
                    if hf.text:
                        raw_texts.append(hf.text)
                        cleaned_fact = _clean_fact_sentence(hf.text)
                        for atom in re.split(r"(?<=[.!?])\s+", cleaned_fact):
                            if _is_usable_fact_line(atom):
                                fact_candidates.append(_clean_fact_sentence(atom))
                for co in getattr(c, "community_observations", []) or []:
                    if co.text:
                        raw_texts.append(co.text)
                        cleaned_observation = _clean_fact_sentence(co.text)
                        for atom in re.split(r"(?<=[.!?])\s+", cleaned_observation):
                            if _is_usable_fact_line(atom):
                                fact_candidates.append(_clean_fact_sentence(atom))

            locations = _extract_bundle_locations(raw_texts)

            # Deduplicate fact ledger propositions
            dedup_facts: list[str] = []
            seen_cf: set[str] = set()
            for fc in fact_candidates:
                key = fc.casefold()
                if key not in seen_cf and not any(key in s or s in key for s in seen_cf):
                    seen_cf.add(key)
                    dedup_facts.append(fc)

            # Persisted summaries often begin with a generic attribution while
            # the concrete street, duration, number, or service state appears
            # later.  Rank the ledger deterministically so both the compact
            # writer and the fallback lead with useful local detail.
            def fact_score(fact: str) -> tuple[int, int]:
                fact_l = fact.casefold()
                score = 0
                if re.search(r"\d", fact_l):
                    score += 3
                if re.search(
                    r"\b(?:улиц\w*|проспект\w*|район\w*|дом\w*|микрорайон\w*|на\s+гор\w*|в\s+центре)\b",
                    fact_l,
                ):
                    score += 2
                if re.search(
                    r"\b(?:нет\w*|есть|отключ\w*|включ\w*|восстанов\w*|ремонт\w*|работа\w*|перебо\w*|напряж\w*)\b",
                    fact_l,
                ):
                    score += 1
                if re.search(r"\b(?:жител\w*|горожан\w*)\s+(?:сообщ\w*|отмеч\w*)", fact_l):
                    score -= 1
                return score, min(len(fact), 180)

            dedup_facts.sort(key=fact_score, reverse=True)

            # If all raw lines were filtered as chatter, preserve cleaned summary/topic
            if not dedup_facts:
                for candidate in (sample_card.summary, sample_card.topic):
                    if not candidate or not _is_usable_fact_line(candidate):
                        continue
                    cleaned = _clean_fact_sentence(candidate)
                    if _is_usable_fact_line(cleaned):
                        dedup_facts.append(cleaned)
                        break
            # Determine epistemic kind
            is_official = False
            if evidence:
                for s in all_sups:
                    evi = evidence.get(s)
                    if evi and (
                        getattr(evi, "kind", "") == "official_statement"
                        or getattr(evi, "source_role", "") in {"official", "authority"}
                    ):
                        is_official = True
                        break
            epistemic_status = "официальная информация" if is_official else "сообщения жителей"

            # Extract distinct operational states
            bundle_states: list[str] = []
            for c in g_cards:
                for co in getattr(c, "community_observations", []) or []:
                    if co.text and _is_usable_fact_line(co.text):
                        st = _clean_fact_sentence(co.text)
                        if st not in bundle_states:
                            bundle_states.append(st)
                for hf in getattr(c, "hard_facts", []) or []:
                    if hf.text and _is_usable_fact_line(hf.text):
                        st = _clean_fact_sentence(hf.text)
                        if st not in bundle_states:
                            bundle_states.append(st)
            if not bundle_states and dedup_facts:
                bundle_states = list(dedup_facts[:4])

            # Match required operational facts
            story_id_set = set(story_ids)
            bundle_req_facts = tuple(
                rf for rf in required_facts if bool(set(rf.story_ids) & story_id_set)
            )

            if not dedup_facts and not bundle_req_facts:
                continue

            (
                geographic_groups,
                unresolved_geography_story_ids,
                unresolved_geography_facts,
            ) = _build_topic_geographic_groups(g_cards, geography_resolver)

            rubric_bundles.append(
                TopicBundle(
                    bundle_id=f"bundle:{rid}:{group_key}",
                    rubric_id=rid,
                    topic_key=t_key,
                    topic_label=t_label,
                    emoji=t_emoji,
                    story_ids=story_ids,
                    support_ids=tuple(all_sups),
                    fact_ledger=tuple(dedup_facts[:16]),
                    locations=locations,
                    geographic_groups=geographic_groups,
                    unresolved_geography_story_ids=unresolved_geography_story_ids,
                    unresolved_geography_facts=unresolved_geography_facts,
                    required_facts=bundle_req_facts,
                    status_summary="",
                    states=tuple(bundle_states),
                    epistemic_status=epistemic_status,
                )
            )

        bundles.extend(rubric_bundles)

    return tuple(bundles)


def build_digest_presentation_units(
    cards: Sequence[Any],
    presentation_plan: Any = None,
    *,
    max_synthesis_size: int = 100,
    max_normal_size: int = 8,
    max_brief_size: int = 6,
    edition_slug: str = "",
) -> tuple[DigestPresentationUnit, ...]:
    """Partition all detail story cards into deterministic presentation compression units."""
    if not cards:
        return ()

    resolver = _load_digest_geography_resolver(edition_slug)
    geographic_scopes_by_card = {c.id: set(_card_geographic_scopes(c, resolver)) for c in cards}
    fallback_merge_groups = _compute_merge_groups(cards, edition_slug=edition_slug)

    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or "other"
        by_rubric.setdefault(rid, []).append(c)

    units: list[DigestPresentationUnit] = []
    unit_counter = 0

    for rid, r_cards in by_rubric.items():
        groups_by_key: dict[str, list[Any]] = {}
        for c in r_cards:
            geo_scopes = geographic_scopes_by_card.get(c.id, set())
            if len(geo_scopes) == 1:
                geo_key = f"area:{next(iter(geo_scopes))}"
            elif geo_scopes:
                geo_key = f"ambiguous:{c.id}"
            else:
                geo_key = "unlocated"
            fam = _canonical_service_family(c)
            if fam:
                gid = f"service:{fam}:{geo_key}"
            else:
                t_key, _, _ = _canonical_topic_family(c, rid)
                if t_key and not t_key.endswith("_general"):
                    gid = f"topic:{t_key}:{geo_key}"
                else:
                    gid = fallback_merge_groups.get(c.id, c.id)
            groups_by_key.setdefault(gid, []).append(c)

        for gid, g_cards in groups_by_key.items():
            is_service = gid.startswith("service:") or gid.startswith("topic:")
            max_size = max_synthesis_size if is_service else 6
            kind: DigestPresentationUnitKind = "SYNTHESIS" if is_service else "NORMAL"

            for i in range(0, len(g_cards), max_size):
                chunk = g_cards[i : i + max_size]
                unit_counter += 1
                sups_by_story = tuple((c.id, _card_allowed_supports(c)) for c in chunk)
                units.append(
                    DigestPresentationUnit(
                        unit_id=f"unit:{rid}:{unit_counter}",
                        rubric_id=rid,
                        kind=kind,
                        story_ids=tuple(c.id for c in chunk),
                        support_ids_by_story=sups_by_story,
                        min_rank=1,
                        compression_key=f"{rid}:{gid}",
                    )
                )

    return tuple(units)
