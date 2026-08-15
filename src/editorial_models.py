"""Provider-agnostic models for the Story Card editorial pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

IMPORTANCE_VALUES = {"high", "medium", "low"}
STATUS_VALUES = {"established", "attributed", "disputed"}


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
        if not isinstance(self.source_refs, list) or not all(
            isinstance(ref, str) and ref.strip() for ref in self.source_refs
        ):
            raise ValueError("StoryElement.source_refs must contain non-empty strings")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoryElement":
        if not isinstance(data, dict):
            raise ValueError("story element must be an object")
        return cls(
            text=data.get("text", ""),
            source_refs=list(data.get("source_refs", [])),
            status=data.get("status", "attributed"),
            attribution=data.get("attribution", ""),
            areas=list(data.get("areas", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Uncertainty:
    text: str
    basis: str
    related_source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Uncertainty.text must be non-empty")
        if not self.basis.strip():
            raise ValueError("Uncertainty.basis must be non-empty")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Uncertainty":
        if not isinstance(data, dict):
            raise ValueError("uncertainty must be an object")
        return cls(
            text=data.get("text", ""),
            basis=data.get("basis", ""),
            related_source_refs=list(data.get("related_source_refs", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StoryCard:
    id: str
    topic: str
    importance: str
    summary: str
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoryCard":
        if not isinstance(data, dict):
            raise ValueError("story card must be an object")
        return cls(
            id=str(data.get("id", "")),
            topic=str(data.get("topic", "")),
            importance=data.get("importance", "medium"),
            summary=str(data.get("summary", "")),
            story_kind=str(data.get("story_kind", "")),
            timeframe=str(data.get("timeframe", "")),
            current_status=str(data.get("current_status", "")),
            next_known_step=str(data.get("next_known_step", "")),
            editorial_angle=data.get("editorial_angle"),
            hard_facts=[StoryElement.from_dict(item) for item in data.get("hard_facts", [])],
            community_observations=[
                StoryElement.from_dict(item) for item in data.get("community_observations", [])
            ],
            useful_details=[
                StoryElement.from_dict(item) for item in data.get("useful_details", [])
            ],
            uncertainties=[Uncertainty.from_dict(item) for item in data.get("uncertainties", [])],
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
            "hard_facts": [item.to_dict() for item in self.hard_facts],
            "community_observations": [item.to_dict() for item in self.community_observations],
            "useful_details": [item.to_dict() for item in self.useful_details],
            "uncertainties": [item.to_dict() for item in self.uncertainties],
        }

    def all_source_refs(self) -> set[str]:
        refs: set[str] = set()
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


@dataclass
class EditorialAnalysis:
    cards: list[StoryCard]
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditorialAnalysis":
        if not isinstance(data, dict):
            raise ValueError("editorial analysis must be an object")
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

    def validate_refs(self, available_refs: set[str]) -> None:
        for card in self.cards:
            card.validate_refs(available_refs)


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
