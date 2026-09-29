"""Edition-profile geography for article planning and narrative ordering."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from src.city_context import CityContextResolver, CityProfileError
from src.publication.article_brief import ArticleBriefLine, ArticleEditorialBrief
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_material import ArticleMaterialProjection

_CITYWIDE_SCOPE_RE = re.compile(
    r"(?:весь город|всего города|по всему городу|во вс[её]м городе|"
    r"повсюду в городе|город целиком|в городе везде)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleStoryGeography:
    """A conservative, profile-backed geographic label for one Story."""

    group_key: str | None = None
    area_id: str | None = None
    area_name: str | None = None
    place_names: tuple[str, ...] = ()
    distinct_area_focus: tuple[str, ...] = ()
    ambiguous: bool = False

    @property
    def focus(self) -> str | None:
        if self.distinct_area_focus:
            return "separate areas (not one district): " + "; ".join(self.distinct_area_focus)
        if self.ambiguous:
            return None
        if self.area_name and self.place_names:
            return f"{self.area_name} ({', '.join(self.place_names)})"
        if self.area_name:
            return self.area_name
        if self.place_names:
            return ", ".join(self.place_names)
        return None


@lru_cache(maxsize=16)
def _load_place_resolver(profile_path: str) -> CityContextResolver | None:
    path = Path(profile_path)
    if not path.is_file():
        return None
    try:
        resolver = CityContextResolver.from_yaml(path)
    except (CityProfileError, FileNotFoundError):
        return None
    return resolver


def resolve_article_place_resolver(
    context: ArticleEditorialContext,
) -> CityContextResolver | None:
    """Load only the current edition's checked-in city profile."""
    slug = (context.edition_slug or "").strip()
    if not slug or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in slug
    ):
        return None
    path = Path("data/city_profiles") / f"{slug}.yaml"
    resolver = _load_place_resolver(str(path))
    if resolver is not None and resolver.profile_id.casefold() != slug.casefold():
        return None
    return resolver


def resolve_article_place_area_map(
    text: str,
    resolver: CityContextResolver | None,
) -> dict[str, frozenset[str]]:
    """Return verified area memberships for named places in one source text.

    These labels support editorial grouping only. They do not encode physical
    proximity; proximity between places still requires an explicit source
    statement that relates those same places.
    """
    if resolver is None or not text.strip():
        return {}
    try:
        entities = resolver.resolve(text).entities
    except Exception:
        return {}

    result: dict[str, set[str]] = {}
    for entity in entities:
        if (
            entity.kind not in {"place", "area"}
            or not entity.canonical_name
            or entity.confidence != "high"
        ):
            continue
        area_keys = set(resolver.geographic_area_group_keys(entity))
        if area_keys:
            place_key = entity.canonical_name.casefold().replace("ё", "е")
            result.setdefault(place_key, set()).update(area_keys)
    return {place: frozenset(area_ids) for place, area_ids in result.items()}


def resolve_article_place_names(
    text: str,
    resolver: CityContextResolver | None,
) -> frozenset[str]:
    """Return canonical, unambiguous edition place names in text."""
    if resolver is None or not text.strip():
        return frozenset()
    try:
        entities = resolver.resolve(text).entities
    except Exception:
        return frozenset()
    accepted_types = {
        "street",
        "lane",
        "boulevard",
        "prospect",
        "highway",
        "landmark",
        "district",
        "neighborhood",
        "settlement",
        "village",
        "city",
        "",
    }
    return frozenset(
        entity.canonical_name.casefold().replace("ё", "е")
        for entity in entities
        if entity.kind in {"place", "area"}
        and entity.confidence == "high"
        and entity.object_type in accepted_types
        and entity.canonical_name
    )


def build_article_story_geography_map(
    *,
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    material_projection: ArticleMaterialProjection,
    resolver: CityContextResolver | None = None,
) -> dict[str, ArticleStoryGeography]:
    """Resolve story locations from citable projected text, never raw directory payload."""
    resolver = resolver if resolver is not None else resolve_article_place_resolver(context)
    result: dict[str, ArticleStoryGeography] = {}
    if resolver is None:
        return {story.story_id: ArticleStoryGeography() for story in coverage_plan.stories}

    supports_by_story: dict[str, list[str]] = {
        story.story_id: [] for story in coverage_plan.stories
    }
    for support in context.support_index:
        if support.publication_use != "PUBLISH" or support.evidence_kind == "resident_question":
            continue
        story_id = support.story_id
        if story_id in supports_by_story:
            if material_projection.actions_by_support_id.get(support.support_id) not in {
                "KEEP",
                "TRIM_DIRECTORY",
            }:
                continue
            projected = material_projection.text_by_support_id.get(support.support_id, "").strip()
            if projected:
                supports_by_story[story_id].append(projected)

    for story_id, texts in supports_by_story.items():
        area_candidates: dict[str, str] = {}
        colloquial_ids: set[str] = set()
        colloquial_names: dict[str, str] = {}
        place_names: dict[str, str] = {}
        area_focus_names: dict[str, set[str]] = {}
        has_ambiguous_place = False
        has_citywide_scope = False
        for text in texts:
            has_citywide_scope = has_citywide_scope or bool(_CITYWIDE_SCOPE_RE.search(text))
            annotation = resolver.resolve(text)
            for entity in annotation.entities:
                if entity.kind == "place":
                    place_names[entity.entity_id] = entity.canonical_name
                    has_ambiguous_place = has_ambiguous_place or entity.confidence != "high"
                if entity.kind == "area":
                    for area in entity.municipal_areas:
                        if area.confidence == "high":
                            area_candidates[area.area_id] = area.area_name
                    colloquial_ids.update(entity.colloquial_area_ids)
                    for area_id in entity.colloquial_area_ids:
                        colloquial_names[area_id] = entity.canonical_name
                        parent = resolver.local_area_parent(area_id)
                        if parent:
                            parent_id, parent_name = parent
                            area_candidates[parent_id] = parent_name
                elif entity.kind == "place":
                    if entity.confidence == "high":
                        for area in entity.municipal_areas:
                            if area.confidence == "high":
                                area_candidates[area.area_id] = area.area_name
                                area_focus_names.setdefault(f"municipal:{area.area_id}", set()).add(
                                    entity.canonical_name
                                )
                    colloquial_ids.update(entity.colloquial_area_ids)
                    for area_id in entity.colloquial_area_ids:
                        colloquial_names[area_id] = entity.canonical_name
                        area_focus_names.setdefault(f"colloquial:{area_id}", set()).add(
                            entity.canonical_name
                        )
                        parent = resolver.local_area_parent(area_id)
                        if parent:
                            parent_id, parent_name = parent
                            area_candidates[parent_id] = parent_name
                            area_focus_names.setdefault(f"municipal:{parent_id}", set()).add(
                                entity.canonical_name
                            )

        distinct_area_focus = tuple(
            f"{area_name} ({', '.join(sorted(area_focus_names.get(f'municipal:{area_id}', ())))})"
            if area_focus_names.get(f"municipal:{area_id}")
            else area_name
            for area_id, area_name in sorted(area_candidates.items())
        )
        distinct_area_focus += tuple(
            f"{colloquial_names[area_id]} ({', '.join(sorted(area_focus_names.get(f'colloquial:{area_id}', ())))})"
            for area_id in sorted(colloquial_ids)
            if area_id in colloquial_names
            and f"colloquial:{area_id}" in area_focus_names
            and resolver.local_area_parent(area_id) is None
        )
        if len(distinct_area_focus) < 2:
            distinct_area_focus = ()

        if has_citywide_scope and (area_candidates or place_names or colloquial_ids):
            result[story_id] = ArticleStoryGeography(
                place_names=tuple(sorted(place_names.values())),
                distinct_area_focus=distinct_area_focus,
                ambiguous=True,
            )
        elif len(area_candidates) == 1 and not has_ambiguous_place:
            area_id, area_name = next(iter(area_candidates.items()))
            result[story_id] = ArticleStoryGeography(
                group_key=f"municipal:{area_id}",
                area_id=area_id,
                area_name=area_name,
                place_names=tuple(sorted(place_names.values())),
            )
        elif len(area_candidates) > 1:
            # Preserve each verified place-to-area mapping for the writer; an
            # ambiguous Story must never be described as belonging to one area.
            result[story_id] = ArticleStoryGeography(
                place_names=tuple(sorted(place_names.values())),
                distinct_area_focus=distinct_area_focus,
                ambiguous=True,
            )
        elif len(colloquial_ids) == 1 and not area_candidates and not has_ambiguous_place:
            colloquial_id = next(iter(colloquial_ids))
            result[story_id] = ArticleStoryGeography(
                group_key=f"colloquial:{colloquial_id}",
                area_id=colloquial_id,
                area_name=colloquial_names.get(
                    colloquial_id, colloquial_id.replace("_", " ").capitalize()
                ),
                place_names=tuple(sorted(place_names.values())),
            )
        elif len(place_names) == 1 and not has_ambiguous_place:
            place_id, place_name = next(iter(place_names.items()))
            result[story_id] = ArticleStoryGeography(
                group_key=f"place:{place_id}",
                place_names=(place_name,),
            )
        else:
            result[story_id] = ArticleStoryGeography(
                place_names=tuple(sorted(place_names.values())),
                distinct_area_focus=distinct_area_focus,
                ambiguous=bool(place_names or colloquial_ids),
            )
    return result


def split_and_order_article_brief_by_geography(
    brief: ArticleEditorialBrief,
    story_geography: dict[str, ArticleStoryGeography],
    context: ArticleEditorialContext,
) -> ArticleEditorialBrief:
    """Separate known distant areas, keep each Story's supports attached, and cluster lines by area."""
    used_line_ids = {line.line_id for line in brief.lines}
    transformed: list[ArticleBriefLine] = []
    grouping_key_by_line: dict[str, str | None] = {}
    story_to_line: dict[str, str] = {}

    for line in brief.lines:
        known_keys = {
            story_geography.get(story_id, ArticleStoryGeography()).group_key
            for story_id in line.story_ids
        }
        known_keys.discard(None)
        has_unlocated = any(
            story_geography.get(story_id, ArticleStoryGeography()).group_key is None
            for story_id in line.story_ids
        )
        split_required = len(known_keys) > 1 or (bool(known_keys) and has_unlocated)

        groups: OrderedDict[str, list[str]] = OrderedDict()
        if split_required:
            for story_id in line.story_ids:
                geo = story_geography.get(story_id, ArticleStoryGeography())
                key = geo.group_key or f"unlocated:{story_id}"
                groups.setdefault(key, []).append(story_id)
        else:
            initial_key = next(iter(known_keys), None)
            groups[initial_key or f"line:{line.line_id}"] = list(line.story_ids)

        for group_index, (key, story_ids) in enumerate(groups.items(), start=1):
            group_geos = [
                story_geography.get(story_id, ArticleStoryGeography()) for story_id in story_ids
            ]
            usable = [geo for geo in group_geos if geo.group_key == key]
            area_id = usable[0].area_id if usable else None
            area_name = usable[0].area_name if usable else None
            place_names = tuple(dict.fromkeys(name for geo in usable for name in geo.place_names))
            line_id = line.line_id
            if split_required and group_index > 1:
                suffix = group_index
                line_id = f"{line.line_id}-geo-{suffix}"
                while line_id in used_line_ids:
                    suffix += 1
                    line_id = f"{line.line_id}-geo-{suffix}"
                used_line_ids.add(line_id)
            story_set = set(story_ids)
            supports = tuple(
                support_id
                for support_id in line.support_ids
                if getattr(context.support_by_id.get(support_id), "story_id", "") in story_set
            )
            salient = tuple(item for item in line.salient_support_ids if item in supports)
            caveats = tuple(item for item in line.caveat_support_ids if item in supports)
            transformed_line = replace(
                line,
                line_id=line_id,
                story_ids=tuple(story_ids),
                support_ids=supports,
                relation=(line.relation if len(story_ids) > 1 else "independent"),
                salient_support_ids=salient,
                caveat_support_ids=caveats,
                geographic_area_id=area_id,
                geographic_area_name=area_name,
                geographic_place_names=place_names,
            )
            transformed.append(transformed_line)
            grouping_key_by_line[line_id] = key if usable else None
            for story_id in story_ids:
                story_to_line[story_id] = line_id

    # Keep reports from a known area contiguous even when the model interleaved
    # other neighborhoods in its original roadmap. Unlocated lines retain order.
    ordered_groups: OrderedDict[str, list[ArticleBriefLine]] = OrderedDict()
    for index, line in enumerate(transformed):
        geography_key = grouping_key_by_line.get(line.line_id)
        bucket = f"geography:{geography_key}" if geography_key else f"line:{index}:{line.line_id}"
        ordered_groups.setdefault(bucket, []).append(line)
    ordered_lines = tuple(line for group in ordered_groups.values() for line in group)

    dispositions = tuple(
        replace(disposition, line_id=story_to_line[disposition.story_id])
        if disposition.line_id is not None and disposition.story_id in story_to_line
        else disposition
        for disposition in brief.dispositions
    )
    return replace(brief, lines=ordered_lines, dispositions=dispositions)
