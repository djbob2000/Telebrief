"""Profile-backed district hints for named places in digest writer material.

The checked-in city profile is the only source of district membership. A hint
tells the writer which district a named street or place belongs to. It is not a
statement about the event and it does not imply proximity to other places.
The profile also lists words that only look like places (a masked swear word, a
nickname); the writer is told about them and the digest audit blocks them.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from src.city_context import CityContextResolver

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@lru_cache(maxsize=8)
def _resolver_for_slug(edition_slug: str) -> CityContextResolver | None:
    if not edition_slug or not _SLUG_RE.match(edition_slug):
        return None
    path = Path("data/city_profiles") / f"{edition_slug}.yaml"
    if not path.is_file():
        return None
    resolver = CityContextResolver.from_yaml(path)
    if resolver.profile_id.casefold() != edition_slug.casefold():
        return None
    return resolver


def place_area_hints(edition_slug: str, text: str) -> tuple[str, ...]:
    """Return "place: district" hints for named places in one citable text."""
    resolver = _resolver_for_slug(edition_slug)
    if resolver is None or not text.strip():
        return ()
    try:
        entities = resolver.resolve(text).entities
    except Exception:  # noqa: BLE001 - a hint is optional; never block the digest
        return ()
    hints: list[str] = []
    for entity in entities:
        if entity.kind != "place" or entity.confidence != "high" or not entity.canonical_name:
            continue
        districts = sorted(
            {
                candidate.area_name
                for candidate in entity.municipal_areas
                if candidate.confidence == "high" and candidate.area_name
            }
        )
        if districts:
            hints.append(f"{entity.canonical_name}: {', '.join(districts)}")
    return tuple(dict.fromkeys(hints))


def _non_place_hint(resolver: CityContextResolver, name: str) -> str:
    note = resolver.non_place_name_note(name)
    return f"{name}: {note}" if note else name


def non_place_fields(edition_slug: str, text: str) -> dict[str, list[str]]:
    """Return the writer-material field for profile non-place names in one text."""
    resolver = _resolver_for_slug(edition_slug)
    if resolver is None or not text.strip():
        return {}
    names = dict.fromkeys(name for _, _, name in resolver.non_place_name_spans(text))
    if not names:
        return {}
    return {"not_place_names": [_non_place_hint(resolver, name) for name in names]}


def non_places_presented_as_places(edition_slug: str, text: str) -> tuple[str, ...]:
    """Return "name: why" for each profile non-place name this prose presents as a place."""
    resolver = _resolver_for_slug(edition_slug)
    if resolver is None or not text.strip():
        return ()
    from src.publication.article_quality import non_place_names_as_places

    return tuple(
        _non_place_hint(resolver, name) for name in non_place_names_as_places(text, resolver)
    )
