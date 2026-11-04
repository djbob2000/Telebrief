"""Edition geographic scope types, hashing, resolver, and contract definition."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

import psycopg

from src.config_loader import Config, EditionScopeConfig
from src.domain.edition_geography import (
    _COMMON_OUT_OF_SCOPE_LOCATIONS,
    EditionGeographyContext,
    resolve_edition_geography,
)

SCOPE_VERSION = "v1"
EditionScopeClass = Literal["LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"]


@dataclass(frozen=True)
class EditionScopeDecision:
    story_id: int
    scope: EditionScopeClass
    confidence: float
    reason: str


def scope_config_payload(scope: EditionScopeConfig) -> dict[str, object]:
    return {
        "name": scope.name,
        "focus_places": list(scope.focus_places),
        "direct_impact_only": scope.direct_impact_only,
        "notes": list(scope.notes),
    }


def scope_config_hash(scope: EditionScopeConfig) -> str:
    raw = json.dumps(
        scope_config_payload(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def resolve_edition_scope(
    conn: psycopg.AsyncConnection,
    config: Config,
    edition_id: int,
) -> tuple[str, EditionScopeConfig]:
    cur = await conn.execute("SELECT slug, name FROM editions WHERE id = %s", (edition_id,))
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"edition {edition_id} not found")
    slug = str(row[0])
    name = str(row[1]) if len(row) > 1 and row[1] else slug.capitalize()
    scope = config.settings.edition_scopes.get(slug)
    if scope is None:
        geo = resolve_edition_geography(slug, name)
        scope = EditionScopeConfig(
            name=geo.edition_name,
            focus_places=geo.target_locations,
            direct_impact_only=False,
            notes=(),
        )
    return slug, scope


def build_scope_contract(
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None = None,
) -> str:
    focus_list = ", ".join(scope.focus_places)
    notes_block = ""
    if scope.notes:
        notes_block = "\n" + "\n".join(f"- {note}" for note in scope.notes)

    geo_block = ""
    if geo_context is not None:
        geo_block = "\n\n" + geo_context.to_prompt_section()

    return f"""GEOGRAPHIC SCOPE RULES:
- LOCAL: event occurs in one of the configured focus places ({focus_list}).
- DIRECT_IMPACT: event occurs outside the focus places but evidence explicitly states a concrete consequence inside them.
- OUT_OF_SCOPE: external event with no explicit concrete local consequence.
- UNCERTAIN: available evidence is insufficient to establish LOCAL or DIRECT_IMPACT.

CRITICAL INVARIANTS:
1. Source membership alone is not evidence of locality.
2. Same oblast/region/nation, broad strategic relevance, and similarly named military directions are not DIRECT_IMPACT by themselves (same region is not enough).
3. Explicit external geography overrides a local-source assumption unless a concrete local consequence is stated.
Target Edition: {scope.name}
Focus Places: {focus_list}{notes_block}{geo_block}"""


def _norm_geo_text(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").replace("’", "'").replace("`", "'").split())


def _geo_stem(name: str) -> str:
    norm = _norm_geo_text(name)
    for suffix in ("ськ", "ск", "е", "я", "а", "у", "і", "и", "о", "ь"):
        if norm.endswith(suffix) and len(norm) - len(suffix) >= 4:
            norm = norm[: -len(suffix)]
            break
    return norm.rstrip("ь'")


_CENTER_EXCLUSION_PREFIXES_RE = re.compile(
    r"\b(?:торгов|бизнес|бізнес|детск|дитяч|культурн|медицинск|медичн|кризисн|кризов|сервисн|сервісн|развлекательн|розважальн|реабилитацион|реабілітаційн|диагностическ|діагностичн|перинатальн|волонтерск|волонтерськ|гуманитарн|гуманітарн|координацион|координаційн|логистическ|логістичн|учебн|навчальн|фитнес|фітнес|пресс|областн|обласн|районн|административн|адміністративн|населен)[а-яяіїє]*\s+центр",
    re.IGNORECASE,
)
_CENTER_EXCLUSION_SUFFIXES_RE = re.compile(
    r"центр[а-яіїє]*\s+(?:занятост|зайнятост|помощ|допомог|реабилитац|реабілітац|безопасност|безпек|запорож|запоріж|славянск|слов[’\'`]?янськ|краматорск|донецк|дніпр|днепр|харьков|харків|киев|київ|одесс|одес)",
    re.IGNORECASE,
)


def _contains_any_anchor(text: str, anchors: set[str] | tuple[str, ...] | list[str]) -> bool:
    norm_text = _norm_geo_text(text)
    for anchor in anchors:
        if not anchor:
            continue

        anchor_lower = anchor.casefold()
        if anchor_lower == "центр":
            matches = list(re.finditer(r"\bцентр[а-яіїє]*\b", norm_text))
            if not matches:
                continue
            has_valid_center = False
            for m in matches:
                start_window = max(0, m.start() - 35)
                end_window = min(len(norm_text), m.end() + 35)
                window_text = norm_text[start_window:end_window]
                if _CENTER_EXCLUSION_PREFIXES_RE.search(
                    window_text
                ) or _CENTER_EXCLUSION_SUFFIXES_RE.search(window_text):
                    continue
                has_valid_center = True
                break
            if has_valid_center:
                return True
            continue

        stem = _geo_stem(anchor)
        if stem:
            escaped_stem = re.escape(stem)
            matches = list(re.finditer(rf"\b{escaped_stem}[а-яіїє]*\b", norm_text))
            for m in matches:
                word = m.group(0)
                # Prevent matching street adjectives (e.g., Севастопольская) when looking for a city (Севастополь).
                # But allow if the anchor itself is an adjective (e.g., Бердянская коса).
                is_adj_anchor = anchor.endswith(
                    (
                        "ский",
                        "ская",
                        "ское",
                        "ские",
                        "ський",
                        "ська",
                        "ське",
                        "ські",
                        "ському",
                        "ского",
                        "ськом",
                        "ського",
                        "ской",
                    )
                )
                is_adj_word = word.endswith(
                    (
                        "ский",
                        "ская",
                        "ское",
                        "ские",
                        "ський",
                        "ська",
                        "ське",
                        "ські",
                        "ському",
                        "ского",
                        "ськом",
                        "ського",
                        "ской",
                    )
                )
                if not is_adj_anchor and is_adj_word:
                    continue
                return True

        norm_anchor = _norm_geo_text(anchor)
        if norm_anchor:
            escaped_norm = re.escape(norm_anchor)
            matches = list(re.finditer(rf"\b{escaped_norm}[а-яіїє]*\b", norm_text))
            for m in matches:
                word = m.group(0)
                is_adj_anchor = norm_anchor.endswith(
                    (
                        "ский",
                        "ская",
                        "ское",
                        "ские",
                        "ський",
                        "ська",
                        "ське",
                        "ські",
                        "ському",
                        "ского",
                        "ськом",
                        "ського",
                        "ской",
                    )
                )
                is_adj_word = word.endswith(
                    (
                        "ский",
                        "ская",
                        "ское",
                        "ские",
                        "ський",
                        "ська",
                        "ське",
                        "ські",
                        "ському",
                        "ского",
                        "ськом",
                        "ського",
                        "ской",
                    )
                )
                if not is_adj_anchor and is_adj_word:
                    continue
                return True

    return False


def broad_region_without_focus_impact(
    *,
    basis_texts: tuple[str, ...] | list[str],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool:
    """Detect if all basis fragments are broad regional summaries without focus anchors."""
    if not basis_texts:
        return False

    focus_anchors: set[str] = set()
    focus_anchors.update(scope.focus_places)
    if geo_context is not None:
        focus_anchors.update(geo_context.target_locations)
        focus_anchors.update(geo_context.district_locations)

    region_anchors: set[str] = set()
    if geo_context is not None and geo_context.region_name:
        region_anchors.add(geo_context.region_name)
    generic_region_stems = {"област", "регион", "region", "across", "по всей"}

    has_broad_region_mention = False
    for text in basis_texts:
        norm = _norm_geo_text(text)
        if _contains_any_anchor(text, focus_anchors):
            return False
        if _contains_any_anchor(text, region_anchors) or any(
            stem in norm for stem in generic_region_stems
        ):
            has_broad_region_mention = True

    return has_broad_region_mention


_RELOCATED_OR_IDP_ORGANIZATIONS = (
    "військової адміністрації",
    "військова адміністрація",
    "військовій адміністрації",
    "военной администрации",
    "военная администрация",
    "бмва",
    "рідна_стежка",
    "рідна стежка",
    "медицина херсонщини",
    "аквазоо",
    "aquazoo",
    "бердянськ мва",
    "бердянська мва",
    "бердянської мва",
    "бердянській мва",
    "бердянская мва",
    "бердянской мва",
    "бердянськ мтг",
    "бердянська мтг",
    "бердянської мтг",
    "бердянській мтг",
    "бердянская мтг",
    "бердянской мтг",
)

_IDP_TERMS = (
    "внутрішньо переміщен",
    "внутренне перемещен",
    "переміщених осіб",
    "перемещенных лиц",
    "переселенц",
    "впо",
    "впл",
    "бердянськ громад",
    "бердянська громад",
    "бердянської громад",
    "бердянській громад",
    "бердянск громад",
    "бердянская громад",
    "бердянской громад",
    "бердянскую громад",
    "бердянськ мтг",
    "бердянск мтг",
    "бердянськ мва",
    "бердянск мва",
)

_EXTERNAL_LOGISTICS_OR_LOCATIONS = (
    "запоріжж",
    "запорож",
    "петропіль",
    "петропол",
    "нова пошта",
    "нової пошти",
    "новою поштою",
    "гуманітарна нова пошта",
    "дніпро",
    "київ",
    "киев",
)


def external_relocated_idp_event(
    *,
    basis_texts: tuple[str, ...] | list[str],
) -> bool:
    """Detect if basis fragments describe external activities for IDPs/evacuees or relocated bodies."""
    if not basis_texts:
        return False

    for text in basis_texts:
        norm = _norm_geo_text(text)
        if any(org in norm for org in _RELOCATED_OR_IDP_ORGANIZATIONS):
            return True
        has_idp = any(term in norm for term in _IDP_TERMS)
        has_ext = any(ext in norm for ext in _EXTERNAL_LOGISTICS_OR_LOCATIONS)
        if has_idp and has_ext:
            return True

    # Also inspect combined text across fragments to catch split items
    combined_norm = " ".join(_norm_geo_text(t) for t in basis_texts)
    if any(org in combined_norm for org in _RELOCATED_OR_IDP_ORGANIZATIONS):
        return True
    has_combined_idp = any(term in combined_norm for term in _IDP_TERMS)
    has_combined_ext = any(ext in combined_norm for ext in _EXTERNAL_LOGISTICS_OR_LOCATIONS)
    if has_combined_idp and has_combined_ext:
        return True

    return False


def _focus_anchors(
    scope: EditionScopeConfig, geo_context: EditionGeographyContext | None
) -> set[str]:
    """Every configured local anchor: the edition's settlements, districts and neighborhoods."""
    anchors: set[str] = set(scope.focus_places)
    if geo_context is not None:
        anchors.update(geo_context.target_locations)
        anchors.update(geo_context.district_locations)
    return {anchor for anchor in anchors if anchor}


def _settlement_anchors(
    scope: EditionScopeConfig, geo_context: EditionGeographyContext | None
) -> set[str]:
    """Anchors that name the edition's own city or district settlements.

    Neighborhood names (a "Slobodka", a "Center", a "Port") recur in many cities,
    so only these settlement names can tie a message to the edition by themselves.
    """
    anchors: set[str] = set(scope.focus_places)
    if geo_context is not None:
        anchors.add(geo_context.edition_name)
        anchors.update(geo_context.district_locations)
    return {anchor for anchor in anchors if anchor}


def _external_locations(
    scope: EditionScopeConfig, geo_context: EditionGeographyContext | None
) -> set[str]:
    """Known out-of-scope places, without any name that is also a local anchor."""
    locations = (
        geo_context.out_of_scope_locations
        if geo_context is not None
        else _COMMON_OUT_OF_SCOPE_LOCATIONS
    )
    norm_focus = {_norm_geo_text(anchor) for anchor in _focus_anchors(scope, geo_context)}
    return {loc for loc in locations if loc and _norm_geo_text(loc) not in norm_focus}


# First-person locators tie a paragraph to its author's own place, which may
# differ from a place the same message mentions earlier ("Was in X. Here we...").
_FIRST_PERSON_LOCAL_DEIXIS_RE = re.compile(
    r"(?<!\w)(?:у нас|в нашем|в нашому|у нашому|у меня|у мене)(?!\w)"
)


@dataclass(frozen=True)
class FragmentMessageContext:
    """A fragment's own text and the other paragraphs of its source message."""

    text: str
    earlier_message_texts: tuple[str, ...] = ()
    later_message_texts: tuple[str, ...] = ()


def fragment_placed_outside_by_message(
    fragment: FragmentMessageContext,
    *,
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool:
    """Return True when an earlier paragraph of the same message places it outside the edition.

    The fragmenter splits a long post into paragraphs. A later paragraph that names
    no settlement, or only a neighborhood name that other cities share, keeps the
    location its message states earlier (for example a headline naming another city).
    It is placed outside only when no paragraph of the message names the edition's
    city or district settlements and the fragment has no first-person locator.
    """
    if not fragment.earlier_message_texts:
        return False
    if _FIRST_PERSON_LOCAL_DEIXIS_RE.search(_norm_geo_text(fragment.text)):
        return False
    message_texts = (
        fragment.text,
        *fragment.earlier_message_texts,
        *fragment.later_message_texts,
    )
    settlements = _settlement_anchors(scope, geo_context)
    if any(_contains_any_anchor(text, settlements) for text in message_texts):
        return False
    external = _external_locations(scope, geo_context)
    return any(_contains_any_anchor(text, external) for text in fragment.earlier_message_texts)


def story_placed_outside_by_message(
    *,
    basis_fragments: tuple[FragmentMessageContext, ...] | list[FragmentMessageContext],
    story_fragments: tuple[FragmentMessageContext, ...] | list[FragmentMessageContext],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool:
    """Return True when every cited scope basis is placed outside by its own message.

    A Story keeps its scope when any of its fragments carries a local anchor that its
    own message does not place elsewhere, so one external post cannot remove an
    unrelated local report that clustered with it.
    """
    if not basis_fragments:
        return False
    if not all(
        fragment_placed_outside_by_message(item, scope=scope, geo_context=geo_context)
        for item in basis_fragments
    ):
        return False
    focus = _focus_anchors(scope, geo_context)
    return not any(
        _contains_any_anchor(item.text, focus)
        and not fragment_placed_outside_by_message(item, scope=scope, geo_context=geo_context)
        for item in story_fragments
    )


def message_context_names_place(
    texts: tuple[str, ...] | list[str],
    *,
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool:
    """Return True when any text names a local anchor or a known external place."""
    anchors = _focus_anchors(scope, geo_context) | _external_locations(scope, geo_context)
    return any(_contains_any_anchor(text, anchors) for text in texts)


def external_city_without_focus_impact(
    *,
    basis_texts: tuple[str, ...] | list[str],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool:
    """Detect if basis fragments mention an explicit out-of-scope city and zero focus anchors."""
    if not basis_texts:
        return False

    focus_anchors = _focus_anchors(scope, geo_context)
    filtered_out_of_scope = _external_locations(scope, geo_context)

    has_out_of_scope = False
    for text in basis_texts:
        if _contains_any_anchor(text, focus_anchors):
            return False
        if _contains_any_anchor(text, filtered_out_of_scope):
            has_out_of_scope = True

    return has_out_of_scope
