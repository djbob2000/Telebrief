"""Deterministic Berdyansk City Context Resolver and Story Context Enricher."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
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


class EvalResult(Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


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
    r"(?:маршрут(?:ка|ки|у|ом|е)?|автобус(?:а|у|ом|е)?)\s*(?:№\s*|\#\s*)?(\d+(?:[/-]\d+)?[а-яА-Яa-zA-Z]?)"
    r"|\b(\d+(?:[/-]\d+)?[а-яА-Яa-zA-Z]?)\s*(?:-?[йяе])?\s*(?:маршрут(?:ка|ки|у|ом|е)?|автобус(?:а|у|ом|е)?)",
    re.IGNORECASE,
)

_CYRILLIC_TO_LATIN_SUFFIX: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "к": "k",
}


def _normalize(text: str) -> str:
    """Normalize text with NFKC, casefolding, ё->е conversion and whitespace collapsing."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalize_house_number(s: str) -> str:
    """Normalize house number casing and map Cyrillic letter suffixes to Latin."""
    cleaned = unicodedata.normalize("NFKC", str(s)).casefold().strip()
    res: list[str] = []
    for ch in cleaned:
        res.append(_CYRILLIC_TO_LATIN_SUFFIX.get(ch, ch))
    return "".join(res)


@dataclass(frozen=True)
class AddressContext:
    house_number: str | None = None
    normalized_house: str | None = None
    house_int: int | None = None
    parity: str | None = None  # "odd" | "even"
    explicit_side: str | None = None  # "odd" | "even"
    landmark_segment: tuple[str, str] | None = None
    single_landmark: str | None = None
    is_private_sector: bool = False


@dataclass(frozen=True)
class PlaceMatch:
    entity_id: str
    canonical_name: str
    object_type: str
    matched_text: str
    start: int
    end: int
    explicit_object_type: bool
    place_data: dict[str, Any]


def _eval_side(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult:
    target_side = coverage.get("side")
    effective_side = ctx.explicit_side or ctx.parity
    if effective_side is not None:
        return EvalResult.MATCH if effective_side == target_side else EvalResult.NO_MATCH
    return EvalResult.UNKNOWN


def _eval_house_ranges(ranges: list[Any], ctx: AddressContext) -> bool:
    for r in ranges:
        r_str = str(r)
        if "-" in r_str:
            parts = r_str.split("-", 1)
            if parts[0].isdigit() and parts[1].isdigit() and ctx.house_int is not None:
                if int(parts[0]) <= ctx.house_int <= int(parts[1]):
                    return True
            elif normalize_house_number(r_str) == ctx.normalized_house:
                return True
        elif normalize_house_number(r_str) == ctx.normalized_house:
            return True
    return False


_LANDMARK_ALIASES: dict[str, tuple[str, ...]] = {
    "Hretska": ("грецьк", "греческ", "люксембург"),
    "Kosmonavtiv": ("космонавт",),
    "Liepaiska": ("лієпайськ", "лиепайск"),
    "Lomonosova": ("ломоносов",),
    "Melitopolske_highway": ("мелітопольськ", "мелитопольск"),
    "Morska": ("морськ", "морск", "мазін", "мазин"),
    "Pershotravneva": ("першотравнев", "первомайск"),
    "Petrovskyi_shliakh": ("петровськ", "петровск"),
    "Pravdy": ("правд",),
    "Pryvokzalna": ("привокзальн", "привокзаль", "енгельс", "энгельс"),
    "Sevastopolska": ("севастопольськ", "севастопольск"),
    "Skhidnyi_prospect": ("східн", "восточн", "пролетарськ", "пролетарск"),
    "Sofiivska": ("софіївськ", "софиевск", "димитров"),
    "Tsentralna": ("центральн", "маркс"),
    "Tyshchenka": ("тищенк",),
    "Vilnyi_lane": ("вільн", "вольн"),
    "Volodymyra_Dovhanyuka": ("довганюк", "орджонікідз", "орджоникидз"),
    "Volonteriv": ("волонтер", "піонерськ", "пионерск"),
    "Yaroslava_Mudroho": ("мудр", "шаумян"),
}


def _eval_house_numbers(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult:
    if coverage.get("private_sector") and ctx.is_private_sector:
        return EvalResult.MATCH

    houses = [normalize_house_number(str(h)) for h in coverage.get("houses", [])]
    ranges = coverage.get("house_ranges", [])
    if ctx.normalized_house is None:
        return EvalResult.UNKNOWN

    if houses and ctx.normalized_house in houses:
        return EvalResult.MATCH

    if ranges:
        return EvalResult.MATCH if _eval_house_ranges(ranges, ctx) else EvalResult.NO_MATCH

    if houses:
        return EvalResult.NO_MATCH
    return EvalResult.UNKNOWN


def _eval_segment_from_to(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult | None:
    from_h = coverage.get("from_house")
    to_h = coverage.get("to_house")
    if (from_h is None and to_h is None) or ctx.normalized_house is None:
        return None

    from_h_int = int(from_h) if (from_h is not None and str(from_h).isdigit()) else None
    to_h_int = int(to_h) if (to_h is not None and str(to_h).isdigit()) else None

    if from_h_int is not None and ctx.house_int is not None and ctx.house_int < from_h_int:
        return EvalResult.NO_MATCH
    if to_h_int is not None and ctx.house_int is not None and ctx.house_int > to_h_int:
        return EvalResult.NO_MATCH

    target_side = coverage.get("side")
    if (
        from_h is not None
        and from_h_int is None
        and normalize_house_number(str(from_h)) == ctx.normalized_house
    ):
        return _eval_side(coverage, ctx) if target_side else EvalResult.MATCH

    if (
        to_h is not None
        and to_h_int is None
        and normalize_house_number(str(to_h)) == ctx.normalized_house
    ):
        return _eval_side(coverage, ctx) if target_side else EvalResult.MATCH

    if (from_h_int is not None or to_h_int is not None) and ctx.house_int is not None:
        return _eval_side(coverage, ctx) if target_side else EvalResult.MATCH

    return None


def _eval_segment_from_houses(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult | None:
    target_side = coverage.get("side")
    from_houses = [normalize_house_number(str(h)) for h in coverage.get("from_houses", [])]
    if from_houses and ctx.normalized_house is not None:
        if ctx.normalized_house in from_houses:
            return _eval_side(coverage, ctx) if target_side else EvalResult.MATCH
        if target_side:
            eff_side = ctx.explicit_side or ctx.parity
            if eff_side is not None and eff_side != target_side:
                return EvalResult.NO_MATCH
    return None


def _eval_segment_landmarks(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult:
    if ctx.landmark_segment:
        from_lm = coverage.get("from_landmark", "")
        to_lm = coverage.get("to_landmark", "")
        if from_lm and to_lm:
            from_forms = _LANDMARK_ALIASES.get(from_lm, (from_lm.casefold(),))
            to_forms = _LANDMARK_ALIASES.get(to_lm, (to_lm.casefold(),))
            l1, l2 = ctx.landmark_segment
            l1_from = any(f in l1 for f in from_forms)
            l2_to = any(f in l2 for f in to_forms)
            l1_to = any(f in l1 for f in to_forms)
            l2_from = any(f in l2 for f in from_forms)
            if (l1_from and l2_to) or (l1_to and l2_from):
                return EvalResult.MATCH
    return EvalResult.UNKNOWN


def _eval_segment(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult:
    if "except_segment" in coverage and isinstance(coverage["except_segment"], dict):
        excluded = {"kind": "segment", **coverage["except_segment"]}
        except_res = evaluate_coverage(excluded, ctx)
        if except_res == EvalResult.MATCH:
            return EvalResult.NO_MATCH

    fh_res = _eval_segment_from_houses(coverage, ctx)
    if fh_res is not None:
        return fh_res

    from_to_res = _eval_segment_from_to(coverage, ctx)
    if from_to_res is not None:
        return from_to_res

    target_side = coverage.get("side")
    if target_side:
        eff_side = ctx.explicit_side or ctx.parity
        if eff_side is not None and eff_side != target_side:
            return EvalResult.NO_MATCH

    return _eval_segment_landmarks(coverage, ctx)


def _eval_any_of(coverage: dict[str, Any], ctx: AddressContext) -> EvalResult:
    clauses = coverage.get("clauses", [])
    if not clauses:
        return EvalResult.UNKNOWN
    results = [evaluate_coverage(c, ctx) for c in clauses]
    if any(r == EvalResult.MATCH for r in results):
        return EvalResult.MATCH
    if all(r == EvalResult.NO_MATCH for r in results):
        return EvalResult.NO_MATCH
    return EvalResult.UNKNOWN


def evaluate_coverage(coverage: dict[str, Any] | None, ctx: AddressContext) -> EvalResult:
    """Evaluate a single coverage specification against local address context."""
    if not coverage or not isinstance(coverage, dict):
        return EvalResult.MATCH

    kind = coverage.get("kind", "whole_object")
    if kind == "whole_object":
        return EvalResult.MATCH
    if kind == "side":
        return _eval_side(coverage, ctx)
    if kind == "house_numbers":
        return _eval_house_numbers(coverage, ctx)
    if kind == "segment":
        return _eval_segment(coverage, ctx)
    if kind == "any_of":
        return _eval_any_of(coverage, ctx)

    return EvalResult.UNKNOWN


def resolve_place_memberships(
    place: dict[str, Any],
    ctx: AddressContext,
    municipal_areas_by_id: dict[str, dict[str, Any]],
) -> tuple[tuple[AreaCandidate, ...], str]:
    """Apply narrowing precedence and evaluate candidate municipal area memberships."""
    memberships = place.get("memberships", [])
    if not memberships:
        return (), "high"

    evaluations: list[tuple[dict[str, Any], EvalResult]] = []
    for m in memberships:
        cov = m.get("coverage")
        cov_dict = cov if isinstance(cov, dict) else {}
        res = evaluate_coverage(cov_dict, ctx)
        evaluations.append((m, res))

    specific_matches = [
        m
        for m, res in evaluations
        if res == EvalResult.MATCH
        and m.get("coverage", {}).get("kind", "whole_object") != "whole_object"
    ]

    if specific_matches:
        retained = specific_matches
        is_definite = True
    else:
        non_no_matches = [(m, res) for m, res in evaluations if res != EvalResult.NO_MATCH]
        has_unknown = any(
            res == EvalResult.UNKNOWN
            for m, res in non_no_matches
            if m.get("coverage", {}).get("kind", "whole_object") != "whole_object"
        )
        if has_unknown:
            retained = [m for m, res in non_no_matches]
            is_definite = False
        else:
            retained = [m for m, res in non_no_matches if res == EvalResult.MATCH]
            is_definite = True

    candidates: list[AreaCandidate] = []
    unique_area_ids: set[str] = set()
    for m in retained:
        area_id = m.get("area_id", "")
        unique_area_ids.add(area_id)
        cov = m.get("coverage", {})
        cov_kind = cov.get("kind", "whole_object") if isinstance(cov, dict) else "whole_object"
        cand_name = municipal_areas_by_id.get(area_id, {}).get("area_name", area_id)
        candidates.append(
            AreaCandidate(
                area_set="municipal_neighborhood_committees_2021",
                area_id=area_id,
                area_name=cand_name,
                confidence="high" if (len(unique_area_ids) == 1 and is_definite) else "ambiguous",
                coverage_kind=cov_kind,
                source_ref="gazetteer",
            )
        )

    confidence = "high" if (len(unique_area_ids) == 1 and is_definite) else "ambiguous"
    if confidence == "ambiguous":
        candidates = [
            AreaCandidate(
                area_set=c.area_set,
                area_id=c.area_id,
                area_name=c.area_name,
                confidence="ambiguous",
                coverage_kind=c.coverage_kind,
                source_ref=c.source_ref,
            )
            for c in candidates
        ]

    return tuple(candidates), confidence


def _make_vowel_pattern(root: str) -> str:
    """Build a regex pattern handling Ukrainian/Russian vowel variations without morphology chopping."""
    chars: list[str] = []
    for ch in root:
        if ch in ("и", "і", "ы"):
            chars.append("[иіы]")
        elif ch in ("е", "є", "э"):
            chars.append("[еєэ]")
        else:
            chars.append(re.escape(ch))
    return "".join(chars)


def _make_stem_pattern(root: str) -> str:
    """Build a regex pattern handling Ukrainian/Russian vowels and adjective/noun suffixes for typed matches."""
    chars: list[str] = []
    for ch in root:
        if ch in ("и", "і", "ы"):
            chars.append("[иіы]")
        elif ch in ("е", "є", "э"):
            chars.append("[еєэ]")
        else:
            chars.append(re.escape(ch))
    norm = "".join(chars)
    for suffix in (
        "ського",
        "ского",
        "ському",
        "скому",
        "ським",
        "ским",
        "ською",
        "ской",
        "ських",
        "ских",
        "ськими",
        "скими",
        "ском",
        "ській",
        "ське",
        "ское",
        "ський",
        "ский",
        "ська",
        "ская",
        "ську",
        "скую",
        "ські",
        "ские",
        "ией",
        "ією",
        "ием",
        "ієм",
        "ия",
        "ія",
        "ии",
        "ії",
        "ию",
        "ію",
        "ому",
        "ему",
        "ого",
        "его",
        "ым",
        "им",
        "ом",
        "ем",
        "ая",
        "яя",
        "ую",
        "юю",
        "ой",
        "ей",
        "ые",
        "ие",
        "их",
        "ых",
    ):
        suffix_pat = "".join(
            "[иіы]" if c in ("и", "і", "ы") else "[еєэ]" if c in ("е", "є", "э") else re.escape(c)
            for c in suffix
        )
        if norm.endswith(suffix_pat):
            stem = norm[: -len(suffix_pat)]
            return stem + r"[а-яa-z0-9_]*"
    return norm


def _extract_expanded_aliases(canonical_name: str, raw_aliases: list[str]) -> tuple[str, list[str]]:
    words = canonical_name.split()
    if words and words[0].lower().strip(".,") in _OBJECT_TYPE_MAP:
        name_root = " ".join(words[1:])
    elif words and words[-1].lower().strip(".,") in _OBJECT_TYPE_MAP:
        name_root = " ".join(words[:-1])
    else:
        name_root = canonical_name

    aliases = list(raw_aliases)
    if name_root and name_root not in aliases:
        aliases.append(name_root)

    expanded = list(aliases)
    for a in aliases:
        a_words = a.split()
        if a_words and len(a_words) > 1:
            if a_words[0].lower().strip(".,") in _OBJECT_TYPE_MAP:
                root = " ".join(a_words[1:])
                if root and root not in expanded:
                    expanded.append(root)
            elif a_words[-1].lower().strip(".,") in _OBJECT_TYPE_MAP:
                root = " ".join(a_words[:-1])
                if root and root not in expanded:
                    expanded.append(root)
    return name_root, expanded


def _extract_place_tokens(expanded_aliases: list[str]) -> tuple[str, ...]:
    tokens: set[str] = set()
    for a in expanded_aliases:
        norm_a = _normalize(a)
        if not norm_a:
            continue
        for w in norm_a.split():
            if len(w) >= 3 and w not in (
                "вул",
                "вулиця",
                "ул",
                "улица",
                "проспект",
                "бульвар",
                "шоссе",
                "шосе",
            ):
                folded = w.replace("і", "и").replace("є", "е")
                tokens.add(folded[:4] if len(folded) >= 4 else folded)
    return tuple(tokens)


def _find_colliding_ids(matches: list[PlaceMatch]) -> set[str]:
    collision_entity_ids: set[str] = set()
    for i, m1 in enumerate(matches):
        for j, m2 in enumerate(matches):
            if i != j and m1.entity_id != m2.entity_id:
                if max(m1.start, m2.start) < min(m1.end, m2.end):
                    if not m1.explicit_object_type or not m2.explicit_object_type:
                        collision_entity_ids.add(m1.entity_id)
                        collision_entity_ids.add(m2.entity_id)
    return collision_entity_ids


def _match_typed_roots(
    place: dict[str, Any],
    norm_text: str,
    seen_spans: set[tuple[int, int]],
) -> list[PlaceMatch]:
    matches: list[PlaceMatch] = []
    name_root = place.get("name_root", place["canonical_name"])
    norm_root = _normalize(name_root)
    if not norm_root:
        return matches

    root_pat = _make_stem_pattern(norm_root)
    target_type = place["object_type"]
    for p_word, p_type in _OBJECT_TYPE_MAP.items():
        if p_type == target_type:
            pat_pref = r"(?<!\w)" + re.escape(p_word) + r"\.?(?:\s+)" + root_pat + r"(?!\w)"
            for m in re.finditer(pat_pref, norm_text):
                span = (m.start(), m.end())
                if span not in seen_spans:
                    seen_spans.add(span)
                    matches.append(
                        PlaceMatch(
                            entity_id=place["entity_id"],
                            canonical_name=place["canonical_name"],
                            object_type=place["object_type"],
                            matched_text=m.group(0),
                            start=m.start(),
                            end=m.end(),
                            explicit_object_type=True,
                            place_data=place,
                        )
                    )
            pat_suff = r"(?<!\w)" + root_pat + r"(?:\s+)" + re.escape(p_word) + r"\.?(?!\w)"
            for m in re.finditer(pat_suff, norm_text):
                span = (m.start(), m.end())
                if span not in seen_spans:
                    seen_spans.add(span)
                    matches.append(
                        PlaceMatch(
                            entity_id=place["entity_id"],
                            canonical_name=place["canonical_name"],
                            object_type=place["object_type"],
                            matched_text=m.group(0),
                            start=m.start(),
                            end=m.end(),
                            explicit_object_type=True,
                            place_data=place,
                        )
                    )
    return matches


def _check_surrounding_object_type(
    norm_text: str, start_pos: int, end_pos: int, target_type: str
) -> tuple[bool, int, int, bool]:
    preceding = norm_text[:start_pos].strip().split()
    if preceding:
        last_w = preceding[-1].strip(".,!?:;")
        if last_w in _OBJECT_TYPE_MAP:
            if _OBJECT_TYPE_MAP[last_w] != target_type:
                return False, start_pos, end_pos, False
            p_start = norm_text.rfind(preceding[-1], 0, start_pos)
            return True, p_start, end_pos, True

    following = norm_text[end_pos:].strip().split()
    if following:
        first_w = following[0].strip(".,!?:;")
        if first_w in _OBJECT_TYPE_MAP:
            if _OBJECT_TYPE_MAP[first_w] != target_type:
                return False, start_pos, end_pos, False
            after_idx = norm_text.find(following[0], end_pos)
            if after_idx >= 0:
                return True, start_pos, after_idx + len(following[0]), True

    return True, start_pos, end_pos, False


def _match_untyped(
    norm_alias: str,
    place: dict[str, Any],
    norm_text: str,
    seen_spans: set[tuple[int, int]],
) -> list[PlaceMatch]:
    matches: list[PlaceMatch] = []
    pat = r"(?<!\w)" + _make_vowel_pattern(norm_alias) + r"(?!\w)"
    for m in re.finditer(pat, norm_text):
        if any(s_start <= m.start() and m.end() <= s_end for s_start, s_end in seen_spans):
            continue

        valid, start_pos, end_pos, explicit_type = _check_surrounding_object_type(
            norm_text, m.start(), m.end(), place["object_type"]
        )
        if not valid:
            continue

        span = (start_pos, end_pos)
        if span not in seen_spans:
            seen_spans.add(span)
            matches.append(
                PlaceMatch(
                    entity_id=place["entity_id"],
                    canonical_name=place["canonical_name"],
                    object_type=place["object_type"],
                    matched_text=norm_text[start_pos:end_pos],
                    start=start_pos,
                    end=end_pos,
                    explicit_object_type=explicit_type,
                    place_data=place,
                )
            )
    return matches


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
                                "compiled_pat": re.compile(
                                    r"(?<!\w)" + _make_vowel_pattern(norm_alias) + r"(?!\w)"
                                ),
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
            memberships = entry.get("area_memberships", [])
            colloquials = tuple(entry.get("colloquial_area_ids", []))

            raw_aliases: list[str] = [
                a.get("text", "") if isinstance(a, dict) else a
                for a in entry.get("aliases", [])
                if (a.get("text", "") if isinstance(a, dict) else a)
            ]

            name_root, expanded_aliases = _extract_expanded_aliases(canonical_name, raw_aliases)
            tokens = _extract_place_tokens(expanded_aliases)

            self._places.append(
                {
                    "entity_id": entity_id,
                    "canonical_name": canonical_name,
                    "name_root": name_root,
                    "object_type": object_type,
                    "memberships": memberships,
                    "colloquial_area_ids": colloquials,
                    "aliases": expanded_aliases,
                    "pre_tokens": tokens,
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
                                    "compiled_pat": re.compile(
                                        r"(?<!\w)" + re.escape(norm_a) + r"(?!\w)"
                                    ),
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

        route_entities, route_spans = self._resolve_routes_with_spans(text, matched_ids)
        entities.extend(route_entities)
        entities.extend(self._resolve_providers(norm_text, matched_ids))
        entities.extend(self._resolve_direct_areas(norm_text, matched_ids))
        entities.extend(self._resolve_places(norm_text, matched_ids, route_spans))

        return CityContextAnnotation(entities=tuple(entities))

    def _resolve_routes_with_spans(
        self, text: str, matched_ids: set[str]
    ) -> tuple[list[ResolvedEntity], list[tuple[int, int]]]:
        resolved: list[ResolvedEntity] = []
        spans: list[tuple[int, int]] = []
        norm_text = _normalize(text)
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
            # Find span in normalized text
            m_norm = _normalize(match.group(0))
            idx = norm_text.find(m_norm)
            if idx >= 0:
                spans.append((idx, idx + len(m_norm)))
        return resolved, spans

    def _resolve_providers(self, norm_text: str, matched_ids: set[str]) -> list[ResolvedEntity]:
        resolved: list[ResolvedEntity] = []
        for p in self._providers:
            if p["compiled_pat"].search(norm_text) and p["entity_id"] not in matched_ids:
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
            if a["compiled_pat"].search(norm_text):
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

    def _resolve_places(
        self, norm_text: str, matched_ids: set[str], route_spans: list[tuple[int, int]]
    ) -> list[ResolvedEntity]:
        folded_text = norm_text.replace("і", "и").replace("є", "е")
        candidate_places = [
            p for p in self._places if any(tok in folded_text for tok in p["pre_tokens"])
        ]
        if not candidate_places:
            return []

        matches: list[PlaceMatch] = []
        for place in candidate_places:
            matches.extend(self._find_place_matches(place, norm_text))

        if not matches:
            return []

        collision_entity_ids = _find_colliding_ids(matches)
        resolved: list[ResolvedEntity] = []
        processed_entity_ids: set[str] = set()

        for match in matches:
            if match.entity_id in matched_ids or match.entity_id in processed_entity_ids:
                continue
            processed_entity_ids.add(match.entity_id)
            matched_ids.add(match.entity_id)

            ctx = self._extract_address_context(norm_text, match, route_spans)
            candidates, confidence = resolve_place_memberships(
                match.place_data, ctx, self._municipal_areas_by_id
            )

            if match.entity_id in collision_entity_ids:
                confidence = "ambiguous"
                candidates = tuple(
                    AreaCandidate(
                        area_set=c.area_set,
                        area_id=c.area_id,
                        area_name=c.area_name,
                        confidence="ambiguous",
                        coverage_kind=c.coverage_kind,
                        source_ref=c.source_ref,
                    )
                    for c in candidates
                )

            resolved.append(
                ResolvedEntity(
                    kind="place",
                    entity_id=match.entity_id,
                    matched_text=match.matched_text,
                    canonical_name=match.canonical_name,
                    object_type=match.object_type,
                    confidence=confidence,
                    municipal_areas=candidates,
                    colloquial_area_ids=match.place_data["colloquial_area_ids"],
                )
            )

        return resolved

    def _find_place_matches(self, place: dict[str, Any], norm_text: str) -> list[PlaceMatch]:
        seen_spans: set[tuple[int, int]] = set()
        matches = _match_typed_roots(place, norm_text, seen_spans)
        aliases = [place["canonical_name"]] + place["aliases"]
        for alias in aliases:
            norm_alias = _normalize(alias)
            if norm_alias:
                matches.extend(_match_untyped(norm_alias, place, norm_text, seen_spans))
        return matches

    def _extract_address_context(
        self, norm_text: str, match: PlaceMatch, route_spans: list[tuple[int, int]]
    ) -> AddressContext:
        post_window = norm_text[match.end : match.end + 50]
        surrounding = norm_text[max(0, match.start - 30) : match.end + 50]

        house_number: str | None = None
        norm_house: str | None = None
        house_int: int | None = None
        parity: str | None = None

        h_match = re.search(
            r"^(?:[,\s]+(?:(?:дом|д\.|будинок|б\.|№)\s*)?|(?:\s+))(\d+(?:[/-]\d+)?[а-яa-z]?)(?!\w)",
            post_window,
        )
        if h_match:
            num_start = match.end + h_match.start(1)
            num_end = match.end + h_match.end(1)
            is_route_num = any(r_s <= num_start and num_end <= r_e for r_s, r_e in route_spans)
            if not is_route_num:
                raw_num = h_match.group(1)
                norm_house = normalize_house_number(raw_num)
                house_number = raw_num
                int_m = re.match(r"^\d+", norm_house)
                if int_m:
                    house_int = int(int_m.group(0))
                    parity = "even" if house_int % 2 == 0 else "odd"

        explicit_side: str | None = None
        if re.search(r"\b(нечетн\w*|непарн\w*)\b", surrounding):
            explicit_side = "odd"
        elif re.search(r"\b(четн\w*|парн\w*)\b", surrounding):
            explicit_side = "even"

        is_private_sector = bool(
            re.search(r"\b(частн\w*\s+сектор\w*|приватн\w*\s+сектор\w*)\b", surrounding)
        )

        landmark_seg: tuple[str, str] | None = None
        seg_match = re.search(r"(?:от|від)\s+([\w\s]+?)\s+(?:до|по)\s+([\w\s]+)", surrounding)
        if seg_match:
            landmark_seg = (seg_match.group(1).strip(), seg_match.group(2).strip())

        single_landmark: str | None = None
        single_m = re.search(
            r"(?:на\s+пере[хк]рестке|на\s+перехресті|на\s+углу|угол|возле|біля|пересечени\w*)\s+(?:с|з)?\s*([\w\s]+)",
            surrounding,
        )
        if single_m:
            single_landmark = single_m.group(1).strip()

        return AddressContext(
            house_number=house_number,
            normalized_house=norm_house,
            house_int=house_int,
            parity=parity,
            explicit_side=explicit_side,
            landmark_segment=landmark_seg,
            single_landmark=single_landmark,
            is_private_sector=is_private_sector,
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
