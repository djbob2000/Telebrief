"""Canonical event payloads and operational observation domain models."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, cast

OPERATIONAL_STATES: frozenset[str] = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "DEGRADED", "RESTRICTED", "UNKNOWN", "SCHEDULED"}
)

EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "established_fact",
        "community_report",
        "service_access",
        "official_statement",
        "commercial_offer",
        "resident_question",
    }
)

PUBLICATION_USES: frozenset[str] = frozenset({"PUBLISH", "CONTEXT", "EXCLUDE"})


def _normalize_open_tags(value: Any, legacy_category: Any = None) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple)) else []
    if not raw and isinstance(legacy_category, str) and legacy_category.strip():
        raw = [legacy_category]

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item).strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag[:80])
        if len(result) == 12:
            break
    return tuple(result)


def _parse_iso_timestamp(ts_str: str | None) -> dt.datetime | None:
    if not ts_str or not isinstance(ts_str, str) or not ts_str.strip():
        return None
    try:
        return dt.datetime.fromisoformat(ts_str.strip())
    except ValueError as e:
        msg = f"Invalid ISO-8601 timestamp '{ts_str}'"
        raise ValueError(msg) from e


@dataclass(frozen=True)
class EvidenceItemPayload:
    """A single structured piece of factual evidence with exact fragment provenance."""

    text: str
    kind: Literal[
        "established_fact",
        "community_report",
        "service_access",
        "official_statement",
        "commercial_offer",
        "resident_question",
    ]
    publication_use: Literal["PUBLISH", "CONTEXT", "EXCLUDE"]
    source_fragment_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            msg = "Evidence item text cannot be empty"
            raise ValueError(msg)
        if self.kind not in EVIDENCE_KINDS:
            msg = f"Invalid evidence kind '{self.kind}'; expected one of {sorted(EVIDENCE_KINDS)}"
            raise ValueError(msg)
        if self.publication_use not in PUBLICATION_USES:
            msg = (
                f"Invalid publication_use '{self.publication_use}'; "
                f"expected one of {sorted(PUBLICATION_USES)}"
            )
            raise ValueError(msg)
        if not self.source_fragment_ids:
            msg = "source_fragment_ids must contain at least one cited fragment ID"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "publication_use": self.publication_use,
            "source_fragment_ids": list(self.source_fragment_ids),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        allowed_fragment_ids: set[int] | None = None,
    ) -> EvidenceItemPayload:
        raw_text = str(data.get("text", "")).strip()
        if not raw_text:
            msg = "Evidence item text cannot be empty"
            raise ValueError(msg)

        raw_kind = str(data.get("kind", "")).strip().lower()
        if raw_kind not in EVIDENCE_KINDS:
            msg = (
                f"Invalid evidence kind '{data.get('kind')}'; "
                f"expected one of {sorted(EVIDENCE_KINDS)}"
            )
            raise ValueError(msg)

        raw_pub_use = str(data.get("publication_use", "")).strip().upper()
        if raw_kind == "resident_question":
            raw_pub_use = "CONTEXT"
        elif not raw_pub_use:
            raw_pub_use = "EXCLUDE" if raw_kind == "commercial_offer" else "PUBLISH"
        if raw_pub_use not in PUBLICATION_USES:
            msg = (
                f"Invalid publication_use '{data.get('publication_use')}'; "
                f"expected one of {sorted(PUBLICATION_USES)}"
            )
            raise ValueError(msg)

        raw_ids = data.get("source_fragment_ids", [])
        if isinstance(raw_ids, (list, tuple)):
            clean_ids = tuple(
                int(x) for x in raw_ids if isinstance(x, (int, str)) and str(x).isdigit()
            )
        else:
            clean_ids = ()

        if not clean_ids:
            msg = "source_fragment_ids cannot be empty"
            raise ValueError(msg)

        if allowed_fragment_ids is not None:
            invalid_ids = set(clean_ids) - allowed_fragment_ids
            if invalid_ids:
                msg = (
                    f"Evidence item references fragment IDs {invalid_ids} "
                    f"not in allowed_fragment_ids {allowed_fragment_ids}"
                )
                raise ValueError(msg)

        return cls(
            text=raw_text,
            kind=cast(
                Literal[
                    "established_fact",
                    "community_report",
                    "service_access",
                    "official_statement",
                    "commercial_offer",
                    "resident_question",
                ],
                raw_kind,
            ),
            publication_use=cast(Literal["PUBLISH", "CONTEXT", "EXCLUDE"], raw_pub_use),
            source_fragment_ids=clean_ids,
        )


@dataclass(frozen=True)
class OperationalObservationPayload:
    """A single structured factual observation about utility or service operations."""

    subject_key: str
    subject_label: str
    dimension: str
    location: str
    entity: str
    state: str
    detail: str
    source_fragment_ids: tuple[int, ...]
    effective_from: str | None = None
    effective_until: str | None = None

    def __post_init__(self) -> None:
        state_upper = self.state.strip().upper()
        if state_upper not in OPERATIONAL_STATES:
            msg = (
                f"Invalid operational observation state '{self.state}'; "
                f"expected one of {sorted(OPERATIONAL_STATES)}"
            )
            raise ValueError(msg)
        if not self.source_fragment_ids:
            msg = "source_fragment_ids must contain at least one cited fragment ID"
            raise ValueError(msg)

        t_from = _parse_iso_timestamp(self.effective_from)
        t_until = _parse_iso_timestamp(self.effective_until)
        if t_from is not None and t_until is not None and t_until < t_from:
            msg = f"effective_until ({self.effective_until}) cannot be earlier than effective_from ({self.effective_from})"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "subject_key": self.subject_key,
            "subject_label": self.subject_label,
            "dimension": self.dimension,
            "location": self.location,
            "entity": self.entity,
            "state": self.state,
            "detail": self.detail,
            "source_fragment_ids": list(self.source_fragment_ids),
        }
        if self.effective_from is not None:
            d["effective_from"] = self.effective_from
        if self.effective_until is not None:
            d["effective_until"] = self.effective_until
        return d

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        allowed_fragment_ids: set[int] | None = None,
    ) -> OperationalObservationPayload:
        raw_state = str(data.get("state", "")).strip().upper()
        if raw_state not in OPERATIONAL_STATES:
            msg = (
                f"Invalid operational observation state '{data.get('state')}'; "
                f"expected one of {sorted(OPERATIONAL_STATES)}"
            )
            raise ValueError(msg)

        raw_ids = data.get("source_fragment_ids", [])
        if isinstance(raw_ids, (list, tuple)):
            clean_ids = tuple(
                int(x) for x in raw_ids if isinstance(x, (int, str)) and str(x).isdigit()
            )
        else:
            clean_ids = ()

        if not clean_ids:
            msg = "source_fragment_ids cannot be empty"
            raise ValueError(msg)

        if allowed_fragment_ids is not None:
            invalid_ids = set(clean_ids) - allowed_fragment_ids
            if invalid_ids:
                msg = (
                    f"Observation references fragment IDs {invalid_ids} "
                    f"not in allowed_fragment_ids {allowed_fragment_ids}"
                )
                raise ValueError(msg)

        raw_eff_from = data.get("effective_from")
        eff_from = str(raw_eff_from).strip() if raw_eff_from and str(raw_eff_from).strip() else None
        raw_eff_until = data.get("effective_until")
        eff_until = (
            str(raw_eff_until).strip() if raw_eff_until and str(raw_eff_until).strip() else None
        )

        return cls(
            subject_key=str(data.get("subject_key", "")).strip(),
            subject_label=str(data.get("subject_label", "")).strip(),
            dimension=str(data.get("dimension", "")).strip(),
            location=str(data.get("location", "")).strip(),
            entity=str(data.get("entity", "")).strip(),
            state=raw_state,
            detail=str(data.get("detail", "")).strip(),
            source_fragment_ids=clean_ids,
            effective_from=eff_from,
            effective_until=eff_until,
        )


@dataclass(frozen=True)
class EventPayload:
    """Canonical event payload stored on story_revisions for brief and rich events."""

    topic: str = ""
    tags: tuple[str, ...] = ()
    urgency: str = "normal"
    publishability: str = "news"
    headline: str = ""
    digest_summary: str = ""
    operational_observations: tuple[OperationalObservationPayload, ...] = ()
    evidence_items: tuple[EvidenceItemPayload, ...] = ()
    enrichment_level: Literal["brief", "analysis"] = "analysis"
    key_facts: tuple[str, ...] = ()
    official_positions: tuple[dict[str, str], ...] = ()
    community_observations: tuple[str, ...] = ()
    conflicts_or_uncertainties: tuple[str, ...] = ()
    affected_areas: tuple[str, ...] = ()
    timeline_summary: str = ""
    confidence_score: float = 0.0
    representative_fragment_ids: tuple[int, ...] = ()
    analysis_version: str = ""
    category: str = ""  # deprecated compatibility field

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tags"] = list(self.tags)
        d["key_facts"] = list(self.key_facts)
        d["official_positions"] = [dict(p) for p in self.official_positions]
        d["community_observations"] = list(self.community_observations)
        d["conflicts_or_uncertainties"] = list(self.conflicts_or_uncertainties)
        d["affected_areas"] = list(self.affected_areas)
        d["representative_fragment_ids"] = list(self.representative_fragment_ids)
        d["operational_observations"] = [obs.to_dict() for obs in self.operational_observations]
        d["evidence_items"] = [item.to_dict() for item in self.evidence_items]
        return d

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        allowed_fragment_ids: set[int] | None = None,
    ) -> EventPayload:
        legacy_cat = str(data.get("category", "")).strip()
        tags = _normalize_open_tags(data.get("tags"), legacy_category=legacy_cat)

        raw_level = str(data.get("enrichment_level", "")).strip().lower()
        enrichment_level: Literal["brief", "analysis"] = (
            "brief" if raw_level == "brief" else "analysis"
        )

        obs_list: list[OperationalObservationPayload] = []
        for raw_obs in data.get("operational_observations") or []:
            if isinstance(raw_obs, dict):
                obs_list.append(
                    OperationalObservationPayload.from_dict(
                        raw_obs, allowed_fragment_ids=allowed_fragment_ids
                    )
                )

        evidence_list: list[EvidenceItemPayload] = []
        for raw_evi in data.get("evidence_items") or []:
            if isinstance(raw_evi, dict):
                evidence_list.append(
                    EvidenceItemPayload.from_dict(
                        raw_evi, allowed_fragment_ids=allowed_fragment_ids
                    )
                )

        def _to_str_tuple(val: Any) -> tuple[str, ...]:
            if isinstance(val, (list, tuple)):
                return tuple(str(x).strip() for x in val if str(x).strip())
            return ()

        def _to_int_tuple(val: Any) -> tuple[int, ...]:
            if isinstance(val, (list, tuple)):
                return tuple(int(x) for x in val if isinstance(x, (int, str)) and str(x).isdigit())
            return ()

        raw_positions = data.get("official_positions", [])
        clean_positions: list[dict[str, str]] = []
        if isinstance(raw_positions, (list, tuple)):
            for p in raw_positions:
                if isinstance(p, dict):
                    clean_positions.append(
                        {
                            "source": str(p.get("source", "")).strip(),
                            "statement": str(p.get("statement", "")).strip(),
                        }
                    )

        return cls(
            topic=str(data.get("topic", "")).strip(),
            tags=tags,
            urgency=str(data.get("urgency", "normal")).strip(),
            publishability=str(data.get("publishability", "news")).strip(),
            headline=str(data.get("headline", "")).strip(),
            digest_summary=str(data.get("digest_summary", "")).strip(),
            operational_observations=tuple(obs_list),
            evidence_items=tuple(evidence_list),
            enrichment_level=enrichment_level,
            key_facts=_to_str_tuple(data.get("key_facts")),
            official_positions=tuple(clean_positions),
            community_observations=_to_str_tuple(data.get("community_observations")),
            conflicts_or_uncertainties=_to_str_tuple(data.get("conflicts_or_uncertainties")),
            affected_areas=_to_str_tuple(data.get("affected_areas")),
            timeline_summary=str(data.get("timeline_summary", "")).strip(),
            confidence_score=float(data.get("confidence_score") or 0.0),
            representative_fragment_ids=_to_int_tuple(data.get("representative_fragment_ids")),
            analysis_version=str(data.get("analysis_version", "")).strip(),
            category=legacy_cat,
        )


def parse_event_payload(
    data: Mapping[str, Any],
    allowed_fragment_ids: set[int] | None = None,
) -> EventPayload:
    """Parse a mapping into a canonical EventPayload."""
    return EventPayload.from_dict(data, allowed_fragment_ids=allowed_fragment_ids)


_KEEP_PUBLISHABILITY = frozenset({"news", "brief"})


def ensure_keep_publishability(
    payload: EventPayload,
    *,
    default: Literal["news", "brief"] = "brief",
) -> EventPayload:
    """Keep Gate-retained Event payloads eligible for publication.

    This does not assert that source material is externally confirmed. It only
    prevents a downstream enrichment payload from contradicting an already-made
    KEEP retention decision.
    """
    if payload.publishability in _KEEP_PUBLISHABILITY:
        return payload
    return replace(payload, publishability=default)


def normalize_question_evidence(payload: EventPayload) -> EventPayload:
    """Normalize resident question evidence to CONTEXT and remove question-only operational observations."""
    canonical_items = tuple(
        replace(item, publication_use="CONTEXT")
        if item.kind == "resident_question" and item.publication_use != "CONTEXT"
        else item
        for item in payload.evidence_items
    )
    question_fragment_ids = {
        fid
        for item in canonical_items
        if item.kind == "resident_question"
        for fid in item.source_fragment_ids
    }
    if question_fragment_ids:
        kept_observations = tuple(
            obs
            for obs in payload.operational_observations
            if any(fid not in question_fragment_ids for fid in obs.source_fragment_ids)
        )
    else:
        kept_observations = payload.operational_observations

    return replace(
        payload,
        evidence_items=canonical_items,
        operational_observations=kept_observations,
    )
