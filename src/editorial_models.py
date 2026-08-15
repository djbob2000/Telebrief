"""Provider-agnostic models for the Story Card editorial pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

IMPORTANCE_VALUES = {"high", "medium", "low"}
STATUS_VALUES = {"established", "attributed", "disputed"}


def _extract_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        ref_val = ""
        if isinstance(item, str) and item.strip():
            ref_val = item.strip()
        elif isinstance(item, dict):
            for key in ("source_ref", "source_id", "ref", "id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ref_val = candidate.strip()
                    break
        if ref_val and ref_val not in seen:
            seen.add(ref_val)
            refs.append(ref_val)
    return refs


def _parse_story_elements(items: Any, rep_refs: list[str]) -> list[StoryElement]:
    if not isinstance(items, list):
        return []
    result: list[StoryElement] = []
    for item in items:
        try:
            result.append(StoryElement.from_dict(item, card_refs=rep_refs))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _parse_uncertainties(items: Any, rep_refs: list[str]) -> list[Uncertainty]:
    if not isinstance(items, list):
        return []
    result: list[Uncertainty] = []
    for item in items:
        try:
            result.append(Uncertainty.from_dict(item, card_refs=rep_refs))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _sanitize_elements(
    elements: list[StoryElement], available_refs: set[str]
) -> list[StoryElement]:
    clean: list[StoryElement] = []
    for elem in elements:
        clean_refs = [r for r in elem.source_refs if r in available_refs]
        if clean_refs:
            clean.append(
                StoryElement(
                    text=elem.text,
                    source_refs=clean_refs,
                    status=elem.status,
                    attribution=elem.attribution,
                    areas=list(elem.areas),
                )
            )
    return clean


def _sanitize_uncertainties(
    uncertainties: list[Uncertainty], available_refs: set[str]
) -> list[Uncertainty]:
    clean: list[Uncertainty] = []
    for unc in uncertainties:
        clean_refs = [r for r in unc.related_source_refs if r in available_refs]
        clean.append(
            Uncertainty(
                text=unc.text,
                basis=unc.basis,
                related_source_refs=clean_refs,
            )
        )
    return clean


@dataclass
class StoryElement:
    text: str
    source_refs: list[str]
    status: str = "attributed"
    attribution: str = ""
    areas: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("StoryElement.text must be non-empty")
        if self.status not in STATUS_VALUES:
            raise ValueError(f"status must be one of {sorted(STATUS_VALUES)}")
        if (
            not isinstance(self.source_refs, list)
            or not self.source_refs
            or not all(isinstance(ref, str) and ref.strip() for ref in self.source_refs)
        ):
            raise ValueError("StoryElement.source_refs must contain non-empty strings")

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | str, card_refs: list[str] | None = None
    ) -> "StoryElement":
        fallback_refs = [r for r in (card_refs or []) if isinstance(r, str) and r.strip()]
        if isinstance(data, str):
            text = data.strip()
            if not text:
                raise ValueError("StoryElement.text must be non-empty")
            if not fallback_refs:
                raise ValueError("StoryElement.source_refs must contain non-empty strings")
            return cls(text=text, source_refs=list(fallback_refs), status="attributed")
        if not isinstance(data, dict):
            raise ValueError("story element must be an object or string")

        raw_refs = (
            data.get("source_refs")
            or data.get("sources")
            or data.get("refs")
            or data.get("evidence")
        )
        refs = _extract_refs(raw_refs)
        if not refs:
            refs = list(fallback_refs)

        status = str(data.get("status") or "attributed")
        if status not in STATUS_VALUES:
            status = "attributed"

        areas = (
            [str(a) for a in data.get("areas", []) if str(a).strip()]
            if isinstance(data.get("areas"), list)
            else []
        )

        return cls(
            text=str(data.get("text", "")).strip(),
            source_refs=refs,
            status=status,
            attribution=str(data.get("attribution", "")),
            areas=areas,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Uncertainty:
    text: str
    basis: str = "unspecified"
    related_source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Uncertainty.text must be non-empty")
        if not isinstance(self.basis, str) or not self.basis.strip():
            self.basis = "unspecified"
        if not isinstance(self.related_source_refs, list) or not all(
            isinstance(ref, str) and ref.strip() for ref in self.related_source_refs
        ):
            raise ValueError("Uncertainty.related_source_refs must contain non-empty strings")

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | str, card_refs: list[str] | None = None
    ) -> "Uncertainty":
        fallback_refs = [r for r in (card_refs or []) if isinstance(r, str) and r.strip()]
        if isinstance(data, str):
            text = data.strip()
            if not text:
                raise ValueError("Uncertainty.text must be non-empty")
            return cls(text=text, basis="unspecified", related_source_refs=list(fallback_refs))
        if not isinstance(data, dict):
            raise ValueError("uncertainty must be an object or string")

        raw_refs = (
            data.get("related_source_refs")
            or data.get("source_refs")
            or data.get("sources")
            or data.get("refs")
        )
        refs = _extract_refs(raw_refs)
        if not refs:
            refs = list(fallback_refs)

        basis = str(data.get("basis", "")).strip() or "unspecified"
        return cls(
            text=str(data.get("text", "")).strip(),
            basis=basis,
            related_source_refs=refs,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StoryCard:
    id: str
    topic: str
    importance: str
    summary: str
    representative_source_refs: list[str] = field(default_factory=list)
    story_kind: str = ""
    timeframe: str = ""
    current_status: str = ""
    next_known_step: str = ""
    editorial_angle: dict[str, Any] | None = None
    hard_facts: list[StoryElement] = field(default_factory=list)
    community_observations: list[StoryElement] = field(default_factory=list)
    useful_details: list[StoryElement] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.importance not in IMPORTANCE_VALUES:
            raise ValueError(f"importance must be one of {sorted(IMPORTANCE_VALUES)}")
        if not self.id.strip() or not self.topic.strip() or not self.summary.strip():
            raise ValueError("StoryCard id, topic and summary must be non-empty")
        if self.editorial_angle is not None:
            if not isinstance(self.editorial_angle, dict):
                raise ValueError("editorial_angle must be an object")
            basis_refs = self.editorial_angle.get("basis_refs", [])
            if not isinstance(basis_refs, list) or not all(
                isinstance(ref, str) and ref.strip() for ref in basis_refs
            ):
                raise ValueError("editorial_angle.basis_refs must contain non-empty strings")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoryCard":
        if not isinstance(data, dict):
            raise ValueError("story card must be an object")

        rep_refs = _extract_refs(
            data.get("representative_source_refs")
            or data.get("source_refs")
            or data.get("sources")
            or data.get("refs")
            or data.get("evidence")
        )

        hard_facts = _parse_story_elements(data.get("hard_facts") or data.get("facts"), rep_refs)
        obs_raw = (
            data.get("community_observations") or data.get("observations") or data.get("community")
        )
        community_observations = _parse_story_elements(obs_raw, rep_refs)
        useful_details = _parse_story_elements(
            data.get("useful_details") or data.get("details"), rep_refs
        )
        uncertainties = _parse_uncertainties(data.get("uncertainties"), rep_refs)

        importance = data.get("importance", "medium")
        if importance not in IMPORTANCE_VALUES:
            importance = "medium"

        return cls(
            id=str(data.get("id", "")),
            topic=str(data.get("topic", "")),
            importance=importance,
            summary=str(data.get("summary", "")),
            representative_source_refs=rep_refs,
            story_kind=str(data.get("story_kind", "")),
            timeframe=str(data.get("timeframe", "")),
            current_status=str(data.get("current_status", "")),
            next_known_step=str(data.get("next_known_step", "")),
            editorial_angle=data.get("editorial_angle"),
            hard_facts=hard_facts,
            community_observations=community_observations,
            useful_details=useful_details,
            uncertainties=uncertainties,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "importance": self.importance,
            "summary": self.summary,
            "story_kind": self.story_kind,
            "timeframe": self.timeframe,
            "current_status": self.current_status,
            "next_known_step": self.next_known_step,
            "editorial_angle": self.editorial_angle,
            "representative_source_refs": list(self.representative_source_refs),
            "hard_facts": [item.to_dict() for item in self.hard_facts],
            "community_observations": [item.to_dict() for item in self.community_observations],
            "useful_details": [item.to_dict() for item in self.useful_details],
            "uncertainties": [item.to_dict() for item in self.uncertainties],
        }

    def all_source_refs(self) -> set[str]:
        refs: set[str] = set(self.representative_source_refs)
        for elements in (self.hard_facts, self.community_observations, self.useful_details):
            for element in elements:
                refs.update(element.source_refs)
        for uncertainty in self.uncertainties:
            refs.update(uncertainty.related_source_refs)
        if self.editorial_angle:
            refs.update(self.editorial_angle.get("basis_refs", []))
        return refs

    def validate_refs(self, available_refs: set[str]) -> None:
        missing = sorted(self.all_source_refs() - available_refs)
        if missing:
            raise ValueError(
                f"StoryCard {self.id} has unresolved source refs: {', '.join(missing)}"
            )

    def sanitized_against_refs(self, available_refs: set[str]) -> "StoryCard | None":
        clean_rep_refs = [r for r in self.representative_source_refs if r in available_refs]
        clean_facts = _sanitize_elements(self.hard_facts, available_refs)
        clean_obs = _sanitize_elements(self.community_observations, available_refs)
        clean_details = _sanitize_elements(self.useful_details, available_refs)
        clean_unc = _sanitize_uncertainties(self.uncertainties, available_refs)

        clean_angle = None
        if self.editorial_angle:
            angle_refs = [
                r for r in self.editorial_angle.get("basis_refs", []) if r in available_refs
            ]
            clean_angle = dict(self.editorial_angle)
            clean_angle["basis_refs"] = angle_refs

        had_refs = bool(self.all_source_refs())
        had_elements = bool(self.hard_facts or self.community_observations or self.useful_details)
        has_valid_elements = bool(clean_facts or clean_obs or clean_details)
        has_valid_refs = bool(
            clean_rep_refs
            or any(elem.source_refs for elem in clean_facts + clean_obs + clean_details)
            or any(unc.related_source_refs for unc in clean_unc)
            or (clean_angle and clean_angle.get("basis_refs"))
        )

        if had_refs and not has_valid_refs:
            return None
        if had_elements and not has_valid_elements:
            return None

        return StoryCard(
            id=self.id,
            topic=self.topic,
            importance=self.importance,
            summary=self.summary,
            story_kind=self.story_kind,
            timeframe=self.timeframe,
            current_status=self.current_status,
            next_known_step=self.next_known_step,
            editorial_angle=clean_angle,
            representative_source_refs=clean_rep_refs,
            hard_facts=clean_facts,
            community_observations=clean_obs,
            useful_details=clean_details,
            uncertainties=clean_unc,
        )


@dataclass
class EditorialAnalysis:
    cards: list[StoryCard]
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditorialAnalysis":
        if not isinstance(data, dict):
            raise ValueError("editorial analysis must be an object")
        # Compatibility with one previously persisted claim-registry response. New
        # analysis prompts never emit this shape, but accepting it keeps old dry-run
        # artifacts readable while the Story Card pipeline is rolled out.
        if "cards" not in data and isinstance(data.get("claims"), list):
            cards = []
            for index, claim in enumerate(data["claims"], start=1):
                if not isinstance(claim, dict) or not claim.get("claim"):
                    continue
                refs = [
                    str(item.get("source_id"))
                    for item in claim.get("evidence", [])
                    if isinstance(item, dict) and item.get("source_id")
                ]
                refs = [
                    ref if not ref.startswith("S") or len(ref) >= 7 else f"S{int(ref[1:]):06d}"
                    for ref in refs
                ]
                status = claim.get("status", "attributed")
                if status == "unknown":
                    status = "attributed"
                element = StoryElement(
                    text=str(claim["claim"]),
                    source_refs=refs or ["legacy-source"],
                    status=status,
                )
                cards.append(
                    StoryCard(
                        id=str(claim.get("id", f"SC{index:03d}")),
                        topic=str(claim.get("event_key", "local news")),
                        importance="medium",
                        summary=str(claim["claim"]),
                        hard_facts=[element],
                    )
                )
            return cls(cards=cards)
        cards = [StoryCard.from_dict(item) for item in data.get("cards", [])]
        return cls(
            cards=cards,
            labels=dict(data.get("labels", {})),
            excluded_refs=list(data.get("excluded_refs", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards": [card.to_dict() for card in self.cards],
            "labels": self.labels,
            "excluded_refs": self.excluded_refs,
        }

    def all_source_refs(self) -> set[str]:
        refs: set[str] = set()
        for card in self.cards:
            refs.update(card.all_source_refs())
        refs.update(self.labels.keys())
        refs.update(self.excluded_refs)
        return refs

    def validate_refs(self, available_refs: set[str]) -> None:
        for card in self.cards:
            card.validate_refs(available_refs)

    def sanitized_against_refs(self, available_refs: set[str]) -> "EditorialAnalysis":
        clean_cards: list[StoryCard] = []
        for card in self.cards:
            sanitized_card = card.sanitized_against_refs(available_refs)
            if sanitized_card is not None:
                clean_cards.append(sanitized_card)
        clean_labels = {k: v for k, v in self.labels.items() if k in available_refs}
        clean_excluded = [r for r in self.excluded_refs if r in available_refs]
        return EditorialAnalysis(
            cards=clean_cards,
            labels=clean_labels,
            excluded_refs=clean_excluded,
        )


@dataclass
class SourceRecord:
    ref: str
    message: Any
    source_type: str
    parent_ref: str | None = None
    context_text: str = ""


@dataclass
class PreparedBundle:
    records: dict[str, SourceRecord]
    prompt_text: str
    total_messages: int
    candidate_count: int
