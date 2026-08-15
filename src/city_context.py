"""Deterministic Berdyansk City Context Resolver and Story Context Enricher."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from src.city_context_models import (
    AreaCandidate,
    AreaEvidence,
    CityContextAnnotation,
    ResolvedEntity,
    ScaleEvidence,
    StoryContext,
)
from src.editorial_models import EditorialAnalysis, PreparedBundle, StoryCard


class CityProfileError(RuntimeError):
    """Raised when CityProfile YAML is malformed, invalid, or has an unsupported schema."""

    pass


_OBJECT_TYPE_MAP: dict[str, str] = {
    "ул": "street",
    "ул.": "street",
    "улица": "street",
    "улице": "street",
    "улицы": "street",
    "вул": "street",
    "вул.": "street",
    "вулиця": "street",
    "вулиці": "street",
    "бульвар": "boulevard",
    "бульваре": "boulevard",
    "бул": "boulevard",
    "бул.": "boulevard",
    "проспект": "prospect",
    "проспекте": "prospect",
    "пр-т": "prospect",
    "пр": "prospect",
    "пр.": "prospect",
    "пер": "lane",
    "пер.": "lane",
    "переулок": "lane",
    "переулке": "lane",
    "провулок": "lane",
    "провулку": "lane",
    "шоссе": "highway",
    "шосе": "highway",
}

_ROUTE_CONTEXT_REGEX = re.compile(
    r"(?:маршрут(?:ка|ки|у|ом|е)?|автобус(?:а|у|ом|е)?)\s*(?:№\s*|\#\s*)?(\d+[а-яА-Яa-zA-Z]?)|\b(\d+[а-яА-Яa-zA-Z]?)\s*(?:-?[йяе])?\s*(?:маршрут(?:ка|ки|у|ом|е)?|автобус(?:а|у|ом|е)?)",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Normalize text with NFKC, casefolding, ё->е conversion and whitespace collapsing."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


class CityContextResolver:
    """Deterministic entity resolver using checked-in city profile."""

    def __init__(self, profile_data: dict[str, Any]) -> None:
        self._profile = profile_data
        self._validate_profile()
        self._build_indexes()

    @classmethod
    def from_yaml(cls, path: str | Path) -> CityContextResolver:
        """Load profile from YAML file. FileNotFoundError propagates; YAML/schema errors raise CityProfileError."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"CityProfile file not found: {p}")

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            raise CityProfileError(f"Failed to parse YAML file {p}: {exc}") from exc

        if not isinstance(data, dict):
            raise CityProfileError(f"CityProfile root must be a dict, got {type(data)}")

        return cls(data)

    def _validate_profile(self) -> None:
        if self._profile.get("schema_version") != 2:
            raise CityProfileError(
                f"Unsupported schema_version: {self._profile.get('schema_version')}, expected 2"
            )
        if "stable_context" not in self._profile:
            raise CityProfileError("Missing required 'stable_context' key in CityProfile")
        geography = self._profile.get("stable_context", {}).get("geography", {})
        if "area_sets" not in geography or "street_gazetteer" not in geography:
            raise CityProfileError("Missing required geography structures in CityProfile")

    def _build_indexes(self) -> None:
        geography = self._profile["stable_context"]["geography"]
        self._build_area_indexes(geography)
        self._build_place_indexes(geography)
        self._build_provider_indexes()
        self._build_route_indexes()

    def _build_area_indexes(self, geography: dict[str, Any]) -> None:
        self._area_aliases: list[dict[str, Any]] = []
        self._municipal_areas_by_id: dict[str, dict[str, Any]] = {}

        for area_set in geography.get("area_sets", []):
            set_id = area_set.get("id", "")
            for area in area_set.get("areas", []):
                area_id = area.get("id", "")
                area_name = area.get("name", "")
                if set_id == "municipal_neighborhood_committees_2021":
                    self._municipal_areas_by_id[area_id] = {
                        "area_set": set_id,
                        "area_id": area_id,
                        "area_name": area_name,
                    }
                aliases = list(area.get("aliases", []))
                if area_name and area_name not in aliases:
                    aliases.append(area_name)
                for alias in aliases:
                    norm_alias = _normalize(alias)
                    if norm_alias:
                        self._area_aliases.append(
                            {
                                "norm_alias": norm_alias,
                                "alias_raw": alias,
                                "area_set": set_id,
                                "area_id": area_id,
                                "area_name": area_name,
                            }
                        )

        self._area_aliases.sort(key=lambda x: len(x["norm_alias"]), reverse=True)

    def _build_place_indexes(self, geography: dict[str, Any]) -> None:
        self._places: list[dict[str, Any]] = []
        entries = geography.get("street_gazetteer", {}).get("entries", [])
        for entry in entries:
            canonical_name = entry.get("canonical_name", "")
            object_type = entry.get("object_type", "street")
            entity_id = f"{object_type}:{canonical_name}"
            canonical_parts = canonical_name.split(maxsplit=1)
            name_root = canonical_parts[1] if len(canonical_parts) > 1 else canonical_name
            memberships = entry.get("area_memberships", [])
            colloquials = tuple(entry.get("colloquial_area_ids", []))

            raw_aliases: list[str] = []
            for a in entry.get("aliases", []):
                if isinstance(a, dict):
                    t = a.get("text", "")
                    if t:
                        raw_aliases.append(t)
                elif isinstance(a, str):
                    raw_aliases.append(a)

            if name_root and name_root not in raw_aliases:
                raw_aliases.append(name_root)

            self._places.append(
                {
                    "entity_id": entity_id,
                    "canonical_name": canonical_name,
                    "name_root": name_root,
                    "object_type": object_type,
                    "memberships": memberships,
                    "colloquial_area_ids": colloquials,
                    "aliases": raw_aliases,
                }
            )

    def _build_provider_indexes(self) -> None:
        self._providers: list[dict[str, Any]] = []
        connectivity = self._profile.get("operational_context", {}).get(
            "connectivity", {}
        ) or self._profile.get("stable_context", {}).get("infrastructure", {}).get(
            "service_organizations", {}
        )
        for _group_name, providers_list in connectivity.items():
            if isinstance(providers_list, list):
                for provider in providers_list:
                    p_id = provider.get("id", "")
                    p_name = provider.get("name", "")
                    aliases = list(provider.get("aliases", []))
                    if p_name and p_name not in aliases:
                        aliases.append(p_name)
                    for a in aliases:
                        norm_a = _normalize(a)
                        if norm_a:
                            self._providers.append(
                                {
                                    "norm_alias": norm_a,
                                    "entity_id": p_id,
                                    "canonical_name": p_name,
                                }
                            )
        self._providers.sort(key=lambda x: len(x["norm_alias"]), reverse=True)

    def _build_route_indexes(self) -> None:
        self._routes: list[dict[str, Any]] = []
        transit_routes = self._profile.get("operational_context", {}).get("transit", {}).get(
            "routes", []
        ) or self._profile.get("stable_context", {}).get("infrastructure", {}).get(
            "transport", {}
        ).get(
            "routes", []
        )
        for route in transit_routes:
            r_num = str(route.get("number") or route.get("route_number") or "")
            r_name = route.get("name", f"Маршрут {r_num}")
            if r_num:
                self._routes.append(
                    {
                        "route_number": r_num.casefold(),
                        "entity_id": f"route:{r_num}",
                        "canonical_name": r_name,
                    }
                )

    def resolve(self, text: str) -> CityContextAnnotation:
        """Deterministically resolve local entities referenced in text."""
        norm_text = _normalize(text)
        entities: list[ResolvedEntity] = []
        matched_ids: set[str] = set()

        entities.extend(self._resolve_routes(text, matched_ids))
        entities.extend(self._resolve_providers(norm_text, matched_ids))
        entities.extend(self._resolve_direct_areas(norm_text, matched_ids))
        entities.extend(self._resolve_places(norm_text, matched_ids))

        return CityContextAnnotation(entities=tuple(entities))

    def _resolve_routes(self, text: str, matched_ids: set[str]) -> list[ResolvedEntity]:
        resolved: list[ResolvedEntity] = []
        for match in _ROUTE_CONTEXT_REGEX.finditer(text):
            r_num = (match.group(1) or match.group(2) or "").strip().casefold()
            for r in self._routes:
                if r["route_number"] == r_num and r["entity_id"] not in matched_ids:
                    matched_ids.add(r["entity_id"])
                    resolved.append(
                        ResolvedEntity(
                            kind="route",
                            entity_id=r["entity_id"],
                            matched_text=match.group(0),
                            canonical_name=r["canonical_name"],
                            confidence="high",
                        )
                    )
        return resolved

    def _resolve_providers(self, norm_text: str, matched_ids: set[str]) -> list[ResolvedEntity]:
        resolved: list[ResolvedEntity] = []
        for p in self._providers:
            pattern = r"(?:\b|_|^)" + re.escape(p["norm_alias"]) + r"(?:\b|_|$)"
            if re.search(pattern, norm_text) and p["entity_id"] not in matched_ids:
                matched_ids.add(p["entity_id"])
                resolved.append(
                    ResolvedEntity(
                        kind="provider",
                        entity_id=p["entity_id"],
                        matched_text=p["norm_alias"],
                        canonical_name=p["canonical_name"],
                        confidence="high",
                    )
                )
        return resolved

    def _resolve_direct_areas(self, norm_text: str, matched_ids: set[str]) -> list[ResolvedEntity]:
        resolved: list[ResolvedEntity] = []
        for a in self._area_aliases:
            pattern = r"(?:\b|_|^)" + re.escape(a["norm_alias"]) + r"(?:\b|_|$)"
            if re.search(pattern, norm_text):
                area_id = a["area_id"]
                if area_id not in matched_ids:
                    matched_ids.add(area_id)
                    cand = AreaCandidate(
                        area_set=a["area_set"],
                        area_id=area_id,
                        area_name=a["area_name"],
                        confidence="high",
                        coverage_kind="whole_object",
                        source_ref="gazetteer",
                    )
                    mun_areas = (
                        (cand,) if a["area_set"] == "municipal_neighborhood_committees_2021" else ()
                    )
                    col_areas = (area_id,) if a["area_set"] == "local_colloquial_areas" else ()
                    resolved.append(
                        ResolvedEntity(
                            kind="area",
                            entity_id=area_id,
                            matched_text=a["alias_raw"],
                            canonical_name=a["area_name"],
                            confidence="high",
                            municipal_areas=mun_areas,
                            colloquial_area_ids=col_areas,
                        )
                    )
        return resolved

    def _resolve_places(self, norm_text: str, matched_ids: set[str]) -> list[ResolvedEntity]:
        resolved: list[ResolvedEntity] = []
        for place in self._places:
            matched, raw_text = self._match_place(place, norm_text)
            if matched and place["entity_id"] not in matched_ids:
                matched_ids.add(place["entity_id"])
                resolved.append(self._create_place_entity(place, raw_text))
        return resolved

    def _match_place(self, place: dict[str, Any], norm_text: str) -> tuple[bool, str]:
        aliases = [place["canonical_name"]] + place["aliases"]
        for alias in aliases:
            norm_alias = _normalize(alias)
            if not norm_alias:
                continue

            words = norm_alias.split()
            if words and words[0] in _OBJECT_TYPE_MAP:
                matched, res = self._match_typed_alias(norm_alias, words, alias, norm_text)
                if matched:
                    return True, res
            else:
                matched, res = self._match_untyped_alias(
                    norm_alias, alias, place["object_type"], norm_text
                )
                if matched:
                    return True, res
        return False, ""

    def _match_typed_alias(
        self,
        norm_alias: str,
        words: list[str],
        alias: str,
        norm_text: str,
    ) -> tuple[bool, str]:
        prefix_type = _OBJECT_TYPE_MAP[words[0]]
        alias_root = " ".join(words[1:])
        pattern = r"(?:\b|_|^)" + re.escape(norm_alias) + r"(?:\b|_|$)"
        if re.search(pattern, norm_text):
            return True, alias
        for p_word, p_type in _OBJECT_TYPE_MAP.items():
            if p_type == prefix_type:
                var_pattern = (
                    r"(?:\b|_|^)"
                    + re.escape(p_word)
                    + r"\s+"
                    + re.escape(alias_root)
                    + r"(?:\b|_|$)"
                )
                if re.search(var_pattern, norm_text):
                    return True, f"{p_word} {alias_root}"
        return False, ""

    def _match_untyped_alias(
        self,
        norm_alias: str,
        alias: str,
        place_type: str,
        norm_text: str,
    ) -> tuple[bool, str]:
        pattern = r"(?:\b|_|^)" + re.escape(norm_alias) + r"(?:\b|_|$)"
        match = re.search(pattern, norm_text)
        if not match:
            return False, ""

        preceding = norm_text[: match.start()].strip().split()
        if preceding:
            last_w = preceding[-1].strip(".,!?:;")
            if last_w in _OBJECT_TYPE_MAP and _OBJECT_TYPE_MAP[last_w] != place_type:
                return False, ""
        return True, alias

    def _create_place_entity(self, place: dict[str, Any], raw_text: str) -> ResolvedEntity:
        candidates: list[AreaCandidate] = []
        for m in place["memberships"]:
            area_id = m.get("area_id", "")
            cov = m.get("coverage", {})
            cov_kind = cov.get("kind", "whole_object") if isinstance(cov, dict) else "whole_object"
            cand_name = self._municipal_areas_by_id.get(area_id, {}).get("area_name", area_id)
            candidates.append(
                AreaCandidate(
                    area_set="municipal_neighborhood_committees_2021",
                    area_id=area_id,
                    area_name=cand_name,
                    confidence="ambiguous" if len(place["memberships"]) > 1 else "high",
                    coverage_kind=cov_kind,
                    source_ref="gazetteer",
                )
            )

        confidence = "ambiguous" if len(candidates) > 1 else "high"
        return ResolvedEntity(
            kind="place",
            entity_id=place["entity_id"],
            matched_text=raw_text or place["canonical_name"],
            canonical_name=place["canonical_name"],
            object_type=place["object_type"],
            confidence=confidence,
            municipal_areas=tuple(candidates),
            colloquial_area_ids=place["colloquial_area_ids"],
        )


class StoryContextEnricher:
    """Aggregates geographic evidence across all source refs belonging to a StoryCard."""

    def __init__(self, resolver: CityContextResolver) -> None:
        self._resolver = resolver

    def enrich(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
    ) -> dict[str, StoryContext]:
        """Aggregate story-level area evidence and compute deterministic ScaleEvidence."""
        story_contexts: dict[str, StoryContext] = {}
        for card in analysis.cards:
            story_contexts[card.id] = self._enrich_single_card(card, bundle)
        return story_contexts

    def _enrich_single_card(self, card: StoryCard, bundle: PreparedBundle) -> StoryContext:
        evidence_refs = card.all_source_refs() & bundle.records.keys()
        municipal_evidence_map: dict[str, dict[str, Any]] = {}
        colloquial_set: set[str] = set()

        for ref in sorted(evidence_refs):
            record = bundle.records[ref]
            annotation: CityContextAnnotation | None = getattr(record, "city_context", None)
            if not annotation:
                continue

            for entity in annotation.entities:
                for c_id in entity.colloquial_area_ids:
                    colloquial_set.add(c_id)

                if entity.kind == "area" and entity.confidence == "high":
                    for cand in entity.municipal_areas:
                        a_id = cand.area_id
                        self._record_evidence(
                            municipal_evidence_map, a_id, cand.area_set, ref, is_direct=True
                        )

                elif entity.kind == "place":
                    if entity.confidence != "ambiguous" and len(entity.municipal_areas) == 1:
                        cand = entity.municipal_areas[0]
                        a_id = cand.area_id
                        self._record_evidence(
                            municipal_evidence_map, a_id, cand.area_set, ref, is_direct=False
                        )

        area_evidences: list[AreaEvidence] = []
        observed_area_ids: list[str] = []

        for a_id in sorted(municipal_evidence_map.keys()):
            info = municipal_evidence_map[a_id]
            d_refs = tuple(sorted(info["direct_refs"]))
            i_refs = tuple(sorted(info["inferred_refs"]))
            all_refs = tuple(sorted(info["direct_refs"] | info["inferred_refs"]))
            area_evidences.append(
                AreaEvidence(
                    area_set=info["area_set"],
                    area_id=a_id,
                    source_refs=all_refs,
                    direct_area_refs=d_refs,
                    inferred_from_place_refs=i_refs,
                )
            )
            observed_area_ids.append(a_id)

        observed_count = len(observed_area_ids)
        scale = ScaleEvidence(
            observed_area_ids=tuple(observed_area_ids),
            observed_count=observed_count,
            total_comparable_areas=None,
            coverage_ratio=None,
            geographic_spread=(observed_count >= 2),
            broad_prevalence_supported=False,
            majority_supported=False,
        )

        return StoryContext(
            card_id=card.id,
            municipal_areas=tuple(area_evidences),
            colloquial_area_ids=tuple(sorted(colloquial_set)),
            scale=scale,
        )

    def _record_evidence(
        self,
        evidence_map: dict[str, dict[str, Any]],
        area_id: str,
        area_set: str,
        ref: str,
        is_direct: bool,
    ) -> None:
        if area_id not in evidence_map:
            evidence_map[area_id] = {
                "area_set": area_set,
                "area_id": area_id,
                "direct_refs": set(),
                "inferred_refs": set(),
            }
        key = "direct_refs" if is_direct else "inferred_refs"
        evidence_map[area_id][key].add(ref)
