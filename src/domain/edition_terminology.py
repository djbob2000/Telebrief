"""Edition terminology: profile-declared canonical spellings (article-fidelity W6.4).

A city profile may declare ``stable_context.geography.terminology`` as a list of
``{variant, canonical}`` stems. The variant is a word stem that residents misspell or
use colloquially (for example a mistyped technical term); the canonical stem is the
spelling reader text uses. Matching is on the word start, so the ending (case and
inflection) is preserved. Direct quotations are never rewritten.

The module holds no city-specific data: all entries come from the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")
_TERM_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
# Same direct-quote delimiters as the area slang standardizer.
_QUOTED_SPAN_RE = re.compile(r"(«[^«»]*»|\"[^\"]*\"|“[^”]*”|„[^“”]*[“”])")


@dataclass(frozen=True)
class EditionTerm:
    variant: str
    canonical: str
    pattern: re.Pattern[str]


def _compile_term(variant: str, canonical: str) -> EditionTerm:
    return EditionTerm(
        variant=variant,
        canonical=canonical,
        pattern=re.compile(r"(?<!\w)" + re.escape(variant) + r"([^\W\d_]*)", re.IGNORECASE),
    )


def parse_edition_terminology(items: Any) -> tuple[EditionTerm, ...]:
    """Parse profile entries; malformed entries are ignored rather than applied."""
    if not isinstance(items, list):
        return ()
    terms: list[EditionTerm] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        variant = str(item.get("variant") or "").strip()
        canonical = str(item.get("canonical") or "").strip()
        if (
            not _TERM_RE.fullmatch(variant)
            or not _TERM_RE.fullmatch(canonical)
            or variant.casefold() == canonical.casefold()
            or variant.casefold() in seen
        ):
            continue
        seen.add(variant.casefold())
        terms.append(_compile_term(variant, canonical))
    # Longer variants first so that a longer stem is never shadowed by a shorter one.
    return tuple(sorted(terms, key=lambda term: len(term.variant), reverse=True))


@lru_cache(maxsize=16)
def load_edition_terminology(edition_slug: str) -> tuple[EditionTerm, ...]:
    """Load the checked-in profile's terminology for one edition slug."""
    slug = (edition_slug or "").strip()
    if not _SLUG_RE.fullmatch(slug):
        return ()
    path = Path("data/city_profiles") / f"{slug}.yaml"
    try:
        with open(path, encoding="utf-8") as handle:
            profile = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(profile, dict):
        return ()
    stable = profile.get("stable_context")
    geography = stable.get("geography") if isinstance(stable, dict) else None
    if not isinstance(geography, dict):
        return ()
    return parse_edition_terminology(geography.get("terminology"))


def _replace_term(term: EditionTerm, text: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        canonical = term.canonical
        if match.group(0)[:1].isupper():
            canonical = canonical[:1].upper() + canonical[1:]
        return canonical + match.group(1)

    return term.pattern.sub(substitute, text)


def apply_edition_terminology(text: str, terms: tuple[EditionTerm, ...]) -> str:
    """Replace declared variants outside direct quotes, keeping case and endings."""
    if not text or not terms:
        return text
    parts = _QUOTED_SPAN_RE.split(text)
    for index in range(0, len(parts), 2):
        for term in terms:
            parts[index] = _replace_term(term, parts[index])
    return "".join(parts)


def standardize_edition_terminology(text: str, edition_slug: str) -> str:
    """Apply the edition's declared canonical spellings to reader-facing text."""
    return apply_edition_terminology(text, load_edition_terminology(edition_slug))


def canonical_term_fields(edition_slug: str, text: str) -> list[str]:
    """Return ``variant -> canonical`` advisories for variants present in writer material."""
    if not text:
        return []
    return [
        f"{term.variant} -> {term.canonical}"
        for term in load_edition_terminology(edition_slug)
        if term.pattern.search(text)
    ]
