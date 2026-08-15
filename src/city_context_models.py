"""Data models for deterministic City Context Resolver and Story Context Enricher."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AreaCandidate:
    """A candidate municipal or colloquial area associated with an entity."""

    area_set: str
    area_id: str
    area_name: str
    confidence: str
    coverage_kind: str
    source_ref: str


@dataclass(frozen=True)
class ResolvedEntity:
    """A resolved named entity (street, area, provider, route, etc.)."""

    kind: str  # "place", "area", "provider", "route"
    entity_id: str  # e.g., "street:Шевченка", "point", "route:15"
    matched_text: str
    canonical_name: str
    object_type: str = ""  # "street", "boulevard", "lane", "prospect", etc.
    confidence: str = "high"  # "high" or "ambiguous"
    municipal_areas: tuple[AreaCandidate, ...] = ()
    colloquial_area_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CityContextAnnotation:
    """City context annotation attached to a single SourceRecord."""

    entities: tuple[ResolvedEntity, ...] = ()


@dataclass(frozen=True)
class AreaEvidence:
    """Story-level evidence linking a story card to a specific canonical area."""

    area_set: str
    area_id: str
    source_refs: tuple[str, ...]
    direct_area_refs: tuple[str, ...] = ()
    inferred_from_place_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScaleEvidence:
    """Deterministic scale evidence computed for a StoryCard."""

    observed_area_ids: tuple[str, ...] = ()
    observed_count: int = 0
    total_comparable_areas: int | None = None
    coverage_ratio: float | None = None
    geographic_spread: bool = False
    broad_prevalence_supported: bool = False
    majority_supported: bool = False


@dataclass(frozen=True)
class StoryContext:
    """Aggregated local geographical context for a single StoryCard."""

    card_id: str
    municipal_areas: tuple[AreaEvidence, ...] = ()
    colloquial_area_ids: tuple[str, ...] = ()
    scale: ScaleEvidence = ScaleEvidence()
