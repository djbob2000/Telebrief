"""Deterministic reader-quality diagnostics for Event-First article drafts.

The checks in this module are intentionally narrow.  They identify prose units
that a bounded copy-editing pass can repair while leaving Evidence Boundary
validation in ``article_validator`` as the authority for factual safety.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import diagnose_article_coverage
from src.publication.article_material import ArticleMaterialProjection
from src.publication.article_models import StructuredArticleDraft, _split_sentences_safe

QualitySeverity = Literal["repair", "warning", "blocking"]

# These findings describe article topology that cannot be repaired safely by
# replacing one paragraph.  The generator may ask for one full recomposition;
# ArticleEditor deliberately leaves them for ArticleFinalizer to reject if they
# remain afterwards.
ARTICLE_WHOLE_DRAFT_FINDING_CODES = frozenset(
    {
        "REPEATED_CENTRAL_THESIS",
        "DIRECTORY_TIMETABLE_SECTION",
        "MULTI_SENTENCE_ADDRESS_STATUS_ROSTER",
    }
)

_QUOTE_RE = re.compile(r"[«“\"]([^»”\"]{1,240})[»”\"]")
_STREET_RE = re.compile(
    r"(?:улиц[аеы]|ул\.?|проспект[ае]?|просп\.?|переулк[ае]?|пер\.?|район[ае]?)\s+([а-яёa-z0-9-]+)",
    re.IGNORECASE,
)
_LOCATION_ID_RE = re.compile(
    r"\b(?P<kind>дом\w*|д\.?|здан\w*|подъезд\w*|квартир\w*|насос\w*|линия|линии|ветка)"
    r"\s*(?:№\s*)?(?P<value>\d+[а-яёa-z]?)\b",
    re.IGNORECASE,
)
_TEMPORAL_MARKERS = {
    "morning": re.compile(r"\bутром\b", re.IGNORECASE),
    "daytime": re.compile(r"\bдн[её]м\b", re.IGNORECASE),
    "evening": re.compile(r"\b(?:вечером|к\s+вечеру)\b", re.IGNORECASE),
    "night": re.compile(r"\bночью\b", re.IGNORECASE),
    "previous_day": re.compile(r"\b(?:вчера|позавчера|накануне)\b", re.IGNORECASE),
    "current_day": re.compile(r"\bсегодня\b", re.IGNORECASE),
    "earlier": re.compile(r"\b(?:ранее|прежде|до\s+этого|сначала)\b", re.IGNORECASE),
    "now": re.compile(r"\b(?:сейчас|теперь|на\s+данный\s+момент)\b", re.IGNORECASE),
    "later": re.compile(r"\b(?:позже|затем|потом)\b", re.IGNORECASE),
}
_TEMPORAL_VARIATION_PAIRS = {
    frozenset(("morning", "evening")),
    frozenset(("morning", "night")),
    frozenset(("daytime", "evening")),
    frozenset(("daytime", "night")),
    frozenset(("evening", "night")),
    frozenset(("previous_day", "current_day")),
    frozenset(("previous_day", "now")),
    frozenset(("earlier", "now")),
    frozenset(("earlier", "later")),
}
_ARTICLE_DIRECTORY_TERMS_RE = re.compile(
    r"\b(?:магазин\w*|кафе|ресторан\w*|салон\w*|бутик\w*|мастерск\w*|"
    r"офис\w*|пункт\s+при[её]ма|при[её]м\w*\s+металлолом\w*|"
    r"пункт\w*\s+выдач\w*|постамат\w*|торгов\w*\s+центр\w*)\b",
    re.IGNORECASE,
)
_ARTICLE_TIME_RANGE_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:до|[-–—])\s*\d{1,2}(?::\d{2})?\b",
    re.IGNORECASE,
)
_ARTICLE_ROUTINE_SCHEDULE_RE = re.compile(
    r"\b(?:режим|график)\w*\s+работ\w*|\b(?:ежедневно|каждый\s+день|"
    r"без\s+выходных|пн\.?\s*[-–—]\s*(?:пт|сб)\.?)\b",
    re.IGNORECASE,
)
_ARTICLE_SCHEDULE_CHANGE_RE = re.compile(
    r"\b(?:измен\w*|сократ\w*|перенес\w*|отмен\w*|приостанов\w*|"
    r"восстанов\w*|закрыт\w*|не\s+буд\w*\s+работ\w*|временно)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleReaderQualityFinding:
    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...] = ()
    severity: QualitySeverity = "warning"

    def to_metadata(self) -> dict[str, object]:
        return {
            "code": self.code,
            "unit_id": self.unit_id,
            "message": self.message,
            "support_ids": list(self.support_ids),
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ArticleReaderQualityReport:
    findings: tuple[ArticleReaderQualityFinding, ...] = ()

    @property
    def repair_findings(self) -> tuple[ArticleReaderQualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity in {"repair", "blocking"})

    @property
    def blocking_findings(self) -> tuple[ArticleReaderQualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "blocking")

    @property
    def needs_edit(self) -> bool:
        return bool(self.repair_findings)

    def to_metadata(self) -> dict[str, object]:
        by_severity = {
            severity: sum(1 for finding in self.findings if finding.severity == severity)
            for severity in ("repair", "warning", "blocking")
        }
        by_code: dict[str, int] = {}
        for finding in self.findings:
            by_code[finding.code] = by_code.get(finding.code, 0) + 1
        return {
            "version": "article-reader-quality-v4",
            "finding_count": len(self.findings),
            "needs_edit": self.needs_edit,
            "counts_by_severity": by_severity,
            "counts_by_code": by_code,
            "findings": [finding.to_metadata() for finding in self.findings],
        }


def _support_story_id(support: ArticleSupport) -> str:
    if support.story_id:
        return support.story_id
    match = re.match(r"(story:[^:]+)", support.support_id)
    return match.group(1) if match else ""


def _projected_support_is_citable(
    support_id: str,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
) -> bool:
    support = context.support_by_id.get(support_id)
    if (
        support is None
        or support.publication_use != "PUBLISH"
        or support.evidence_kind == "resident_question"
    ):
        return False
    if material_projection is None:
        return True

    suppressed_story_ids = set(getattr(material_projection, "suppressed_story_ids", ()))
    if _support_story_id(support) in suppressed_story_ids:
        return False
    if (
        getattr(material_projection, "actions_by_support_id", {}).get(support_id)
        == "SUPPRESS_PROMOTION_ONLY"
    ):
        return False
    return bool(getattr(material_projection, "text_by_support_id", {}).get(support_id, "").strip())


def _support_ids_for_unit(unit: object) -> tuple[str, ...]:
    ids = getattr(unit, "cited_support_ids", ())
    return tuple(dict.fromkeys(str(sid) for sid in ids if sid))


def _paragraph_is_directory_timetable_material(
    paragraph: object,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
) -> bool:
    text = str(getattr(paragraph, "text", "") or "")
    support_ids = _citable_support_ids(
        _support_ids_for_unit(paragraph), context, material_projection
    )
    if not text or not support_ids:
        return False

    projected_support_texts = [
        (
            material_projection.text_by_support_id.get(support_id, "")
            if material_projection is not None
            else context.support_by_id[support_id].text
        )
        for support_id in support_ids
    ]
    projected_material = " ".join(value for value in projected_support_texts if value)
    if not projected_material:
        return False

    # Routine business hours and service directories have little city-life
    # value after the access details are stripped. A changed schedule or a
    # concrete public-service disruption is news and must remain admissible.
    if _ARTICLE_SCHEDULE_CHANGE_RE.search(text) or _ARTICLE_SCHEDULE_CHANGE_RE.search(
        projected_material
    ):
        return False
    directory_text = f"{text} {projected_material}"
    if not _ARTICLE_DIRECTORY_TERMS_RE.search(directory_text):
        return False
    if not (
        _ARTICLE_TIME_RANGE_RE.search(text)
        or _ARTICLE_ROUTINE_SCHEDULE_RE.search(text)
        or _ARTICLE_TIME_RANGE_RE.search(projected_material)
        or _ARTICLE_ROUTINE_SCHEDULE_RE.search(projected_material)
    ):
        return False
    return True


def _paragraph_service_observations(
    paragraph: object,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None = None,
) -> list[tuple[str, str, str, ArticleSupport]]:
    observations: list[tuple[str, str, str, ArticleSupport]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for support_id in _citable_support_ids(
        _support_ids_for_unit(paragraph), context, material_projection
    ):
        support = context.support_by_id[support_id]
        source_text = " ".join((support.text, support.source_text)).strip()
        service, place = _service_and_place(source_text, place_resolver)
        state = _state_polarity(source_text)
        if not service or not state:
            continue
        source_places = {place} if place else _extract_place_keys(source_text, place_resolver)
        for source_place in source_places:
            key = (service, source_place, state, support.support_id)
            if key not in seen:
                seen.add(key)
                observations.append((service, source_place, state, support))
    return observations


def _citable_support_ids(
    support_ids: Sequence[str],
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            support_id
            for support_id in support_ids
            if _projected_support_is_citable(support_id, context, material_projection)
        )
    )


def _claim_story_ids(
    support_ids: Sequence[str], context: ArticleEditorialContext
) -> tuple[str, ...]:
    stories: list[str] = []
    for support_id in support_ids:
        support = context.support_by_id.get(support_id)
        if support is None:
            continue
        story_id = _support_story_id(support)
        if story_id and story_id not in stories:
            stories.append(story_id)
    return tuple(stories)


def _unit_claims(
    unit: object, text: str, support_ids: Sequence[str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    claims = getattr(unit, "claims", ())
    if claims:
        return tuple(
            (str(claim.text), tuple(str(sid) for sid in claim.cited_support_ids if sid))
            for claim in claims
            if getattr(claim, "text", "").strip()
        )
    return ((text, tuple(support_ids)),) if text.strip() else ()


def _extract_place_keys(text: str, place_resolver: Any | None = None) -> set[str]:
    if place_resolver is not None:
        try:
            resolved = place_resolver.resolve(text).entities
        except Exception:
            resolved = ()
        accepted_types = {
            "street",
            "lane",
            "boulevard",
            "prospect",
            "highway",
            "district",
            "neighborhood",
            "settlement",
            "village",
            "city",
        }
        resolved_places = {
            unicodedata.normalize("NFKC", entity.canonical_name).casefold().replace("ё", "е")
            for entity in resolved
            if entity.kind == "place"
            and entity.object_type in accepted_types
            and entity.canonical_name
        }
        if resolved_places:
            return resolved_places

    matched_places: set[str] = set()
    for match in _STREET_RE.finditer(text):
        place = match.group(1).casefold().replace("ё", "е")
        place = re.sub(r"(?:ого|ому|ой|ая|ое|ые|ым|ем|ом|и|ы|а|у|е)$", "", place)
        if place:
            matched_places.add(place)
    return matched_places


def _effective_intervals_are_disjoint(first: ArticleSupport, second: ArticleSupport) -> bool:
    if not (
        first.effective_from
        and first.effective_until
        and second.effective_from
        and second.effective_until
    ):
        return False

    def normalize(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    first_start = normalize(first.effective_from)
    first_end = normalize(first.effective_until)
    second_start = normalize(second.effective_from)
    second_end = normalize(second.effective_until)
    if first_start >= first_end or second_start >= second_end:
        return False
    return first_end < second_start or second_end < first_start


def _has_shared_common_condition(
    text: str,
    observations: Sequence[tuple[str, str, str, ArticleSupport]],
    place_resolver: Any | None = None,
) -> bool:
    """Allow compact prose for one supported service state across many places.

    This is deliberately stricter than a narrative relation: every cited
    observation must agree on service and polarity, every observed place must
    appear in one sentence, and that sentence must state the condition once.
    """
    if not observations or ";" in text or len(_split_sentences_safe(text)) != 1:
        return False

    service_states = {(service, state) for service, _place, state, _support in observations}
    if len(service_states) != 1:
        return False
    service, state = next(iter(service_states))
    places = {place for _service, place, _state, _support in observations if place}
    mentioned_places = _extract_place_keys(text, place_resolver)
    if len(places) < 4 or places != mentioned_places:
        return False

    # If the source analysis has explicit effective intervals, require one
    # common overlap before describing the reports as a shared condition.
    # Missing intervals remain usable; disjoint known intervals do not.
    timed_intervals = [
        (support.effective_from, support.effective_until)
        for _service, _place, _state, support in observations
        if support.effective_from is not None and support.effective_until is not None
    ]
    if timed_intervals:
        latest_start = max(start for start, _end in timed_intervals)
        earliest_end = min(end for _start, end in timed_intervals)
        if latest_start >= earliest_end:
            return False

    lowered = text.casefold()
    # These negative words describe absence of a problem/report, or explicitly
    # deny an outage; they cannot establish a negative service state.
    if re.search(
        r"\b(?:нет\s+(?:проблем\w*|перебо\w*|подтвержден\w*|сведен\w*)|"
        r"не\s+(?:отключен\w*|пропал\w*|отсутств\w*|ограничен\w*))\b",
        lowered,
    ):
        return False
    # A common-state sentence must name the service and must not introduce an
    # unsupported contrast between places or time periods.
    service_markers = {
        "water": ("вод", "водоканал", "водовод", "насос"),
        "power": ("свет", "электр", "энерг", "напряж"),
        "connectivity": ("интернет", "связ", "провайдер", "роутер"),
        "heating_or_gas": ("газ", "отоплен"),
    }.get(service, ())
    if not service_markers or not any(marker in lowered for marker in service_markers):
        return False
    if re.search(
        r"\b(?:но|однако|тогда\s+как|в\s+то\s+время\s+как|при\s+этом|между\s+тем|"
        r"одни\b.{0,100}\bдругие)\b",
        lowered,
    ):
        return False
    if _state_polarity(text) != state:
        return False

    state_markers = (
        r"\bнет\b|\bотсутств\w*|\bограничен\w*|\bотключ\w*|\bпропал\w*|"
        r"\bне\s+(?:работ\w*|восстанов\w*|пода\w*|появил\w*|включил\w*)|"
        r"\bбез\s+(?:свет\w*|вод\w*|газ\w*|отоплен\w*|интернет\w*|связ\w*)"
        if state == "negative"
        else r"\bвосстанов\w*|\bпоявил\w*|\bподача\s+есть\b|\bработа\w*|"
        r"\bдоступн\w*|\bвключил\w*"
    )
    # Repeating «нет/работает/восстановили» after each address is still a
    # roster, even if commas replace semicolons.
    return len(re.findall(state_markers, lowered)) == 1


def _has_narrative_relation(
    text: str,
    observations: Sequence[tuple[str, str, str, ArticleSupport]],
    place_resolver: Any | None = None,
) -> bool:
    """Return true only when prose structure and its cited sources agree.

    A connector word is never sufficient by itself. Contrasts need the same
    service reported in different states at the named places. Progressions
    need the same service/place, different supported states, a progression
    phrase, and non-overlapping explicit effective intervals.
    """
    if _has_shared_common_condition(text, observations, place_resolver):
        return True

    lowered = text.casefold()
    contrast_marker = bool(
        re.search(
            r"(?:тогда\s+как|в\s+то\s+время\s+как|при\s+этом|в\s+отличие\s+от|"
            r"однако|между\s+тем|но|а)\s+(?:на|в|у)\b|"
            r"одни\b.{0,180}\bдругие\b",
            lowered,
        )
    )
    progression_cue = bool(
        re.search(
            r"\b(?:за\s+(?:(?:несколько|пару|\d+)\s+)?(?:час\w*|дн\w*)|"
            r"в\s+течение\s+(?:дня|нескольких\s+час\w*)|"
            r"через\s+несколько\s+час\w*)\b",
            lowered,
        )
        and re.search(r"\b(?:измен\w*|смен\w*|перемен\w*)\b", lowered)
    )
    if not contrast_marker and not progression_cue:
        return False

    mentioned_places = _extract_place_keys(text, place_resolver)
    if not mentioned_places:
        return False

    covered_support_ids: set[str] = set()
    for index, (first_service, first_place, first_state, first_support) in enumerate(observations):
        if first_place not in mentioned_places:
            continue
        for second_service, second_place, second_state, second_support in observations[index + 1 :]:
            if (
                first_support.support_id == second_support.support_id
                or first_service != second_service
                or first_state == second_state
            ):
                continue
            places_are_contrasted = (
                contrast_marker and first_place != second_place and second_place in mentioned_places
            )
            effective_progression = (
                progression_cue
                and first_place == second_place
                and _effective_intervals_are_disjoint(first_support, second_support)
            )
            if places_are_contrasted or effective_progression:
                covered_support_ids.update((first_support.support_id, second_support.support_id))

    relevant_support_ids = {observation[3].support_id for observation in observations}
    return bool(relevant_support_ids) and relevant_support_ids <= covered_support_ids


def _temporal_markers(text: str) -> set[str]:
    return {name for name, pattern in _TEMPORAL_MARKERS.items() if pattern.search(text)}


def _has_temporal_progression(first: str, second: str) -> bool:
    return any(
        frozenset((first_marker, second_marker)) in _TEMPORAL_VARIATION_PAIRS
        for first_marker in _temporal_markers(first)
        for second_marker in _temporal_markers(second)
    )


def _normalized_heading(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", normalized).split())


def _material_tokens(text: str) -> set[str]:
    ignored = {
        "это",
        "для",
        "что",
        "как",
        "или",
        "при",
        "после",
        "перед",
        "около",
        "сообщают",
        "сообщили",
        "жители",
        "житель",
        "городе",
        "города",
        "улице",
        "улица",
    }
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return {
        token
        for token in re.findall(r"[\w-]+", normalized)
        if len(token) >= 3 and token not in ignored
    }


def _claim_is_materially_new(first_text: str, second_text: str) -> bool:
    first_service, first_place = _service_and_place(first_text)
    second_service, second_place = _service_and_place(second_text)
    first_state, second_state = _state_polarity(first_text), _state_polarity(second_text)
    if first_service and second_service and first_service != second_service:
        return True
    if first_place and second_place and first_place != second_place:
        return True
    if first_state and second_state and first_state != second_state:
        return True
    if _has_temporal_progression(first_text, second_text):
        return True

    consequence_pattern = re.compile(
        r"(?:из-за\s+этого|поэтому|в\s+результате|в\s+итоге|чтобы|для\s+того\s+чтобы|"
        r"пришлось|что\s+позволило)\s+([^.!?]{3,140})",
        re.IGNORECASE,
    )
    first_consequence = consequence_pattern.search(first_text)
    second_consequence = consequence_pattern.search(second_text)
    if bool(first_consequence) != bool(second_consequence):
        return True
    if first_consequence and second_consequence:
        first_result = _material_tokens(first_consequence.group(1))
        second_result = _material_tokens(second_consequence.group(1))
        if first_result and second_result and first_result != second_result:
            return True

    first_tokens, second_tokens = _material_tokens(first_text), _material_tokens(second_text)
    if not first_tokens or not second_tokens:
        return False
    similarity = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
    return similarity < 0.68


def _supports_show_distinct_effective_times(
    first_ids: Sequence[str],
    second_ids: Sequence[str],
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
) -> bool:
    first_supports = [
        context.support_by_id[sid]
        for sid in _citable_support_ids(first_ids, context, material_projection)
        if sid in context.support_by_id
    ]
    second_supports = [
        context.support_by_id[sid]
        for sid in _citable_support_ids(second_ids, context, material_projection)
        if sid in context.support_by_id
    ]
    first_timed = [s for s in first_supports if s.effective_from and s.effective_until]
    second_timed = [s for s in second_supports if s.effective_from and s.effective_until]
    return bool(first_timed and second_timed) and not any(
        _time_overlap(first, second) for first in first_timed for second in second_timed
    )


def _support_unit_index(draft: StructuredArticleDraft) -> dict[str, tuple[str, ...]]:
    by_support: dict[str, list[str]] = {}
    for sid in draft.title_support_ids:
        by_support.setdefault(sid, []).append("TITLE")
    for claim in draft.title_claims:
        for sid in claim.cited_support_ids:
            by_support.setdefault(sid, []).append("TITLE")
    for sid in draft.lead_support_ids:
        by_support.setdefault(sid, []).append("LEAD")
    for claim in draft.lead_claims:
        for sid in claim.cited_support_ids:
            by_support.setdefault(sid, []).append("LEAD")
    p_idx = 1
    for sec in draft.sections:
        for para in sec.paragraphs:
            unit_id = f"P{p_idx:03d}"
            for sid in _support_ids_for_unit(para):
                by_support.setdefault(sid, []).append(unit_id)
            for claim in para.claims:
                for sid in claim.cited_support_ids:
                    by_support.setdefault(sid, []).append(unit_id)
            p_idx += 1
    return {sid: tuple(ids) for sid, ids in by_support.items()}


def _time_overlap(first: ArticleSupport, second: ArticleSupport) -> bool:
    """Require explicit effective intervals; observed_at cannot create overlap."""
    if first.effective_from is None or first.effective_until is None:
        return False
    if second.effective_from is None or second.effective_until is None:
        return False

    def normalize(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    first_start, first_end = normalize(first.effective_from), normalize(first.effective_until)
    second_start, second_end = normalize(second.effective_from), normalize(second.effective_until)
    return max(first_start, second_start) <= min(first_end, second_end)


def _service_and_place(
    text: str,
    place_resolver: Any | None = None,
) -> tuple[str, str]:
    lowered = text.casefold()
    if any(token in lowered for token in ("вод", "водоканал", "водовод", "насос")):
        service = "water"
    elif any(token in lowered for token in ("свет", "электр", "энерг", "напряж")):
        service = "power"
    elif any(token in lowered for token in ("интернет", "связ", "провайдер", "роутер")):
        service = "connectivity"
    elif any(token in lowered for token in ("газ", "отоплен")):
        service = "heating_or_gas"
    else:
        service = ""

    place_match = _STREET_RE.search(text)
    if not place_match:
        return service, ""
    if place_resolver is not None:
        resolver_inputs = [text]
        matched_name = place_match.group(1)
        stem = re.sub(
            r"(?:ого|ому|ой|ая|ое|ые|ым|ем|ом|и|ы|а|у|е)$",
            "",
            matched_name.casefold().replace("ё", "е"),
        )
        if stem != matched_name.casefold().replace("ё", "е"):
            for suffix in ("а", "я", "ая", "яя", "ый", "ий", "ой", "ська", "ская"):
                candidate_name = stem + suffix
                resolver_inputs.append(
                    text[: place_match.start(1)] + candidate_name + text[place_match.end(1) :]
                )
        try:
            resolved_places = {
                entity.canonical_name.casefold().replace("ё", "е")
                for resolver_input in resolver_inputs
                for entity in place_resolver.resolve(resolver_input).entities
                if entity.kind == "place"
                and entity.object_type in {"street", "lane", "boulevard", "prospect", "highway"}
                and entity.canonical_name
            }
        except Exception:
            resolved_places = set()
        if len(resolved_places) == 1:
            return service, next(iter(resolved_places))
        if len(resolved_places) > 1:
            # A sentence with multiple local places cannot establish that the
            # opposite reports concern the same service point.
            return service, ""

    place = place_match.group(1).casefold().replace("ё", "е")
    # A light stem makes «Садовой» and «Садовая» the same canonical street
    # without pretending to be a world geocoder.
    place = re.sub(r"(?:ого|ому|ой|ая|ое|ые|ым|ем|ом|и|ы|а|у|е)$", "", place)
    return service, place


def _state_polarity(text: str) -> str:
    lowered = text.casefold()
    # Remove negated positive verbs before looking for positive markers. This
    # keeps «не работает» and «не восстановили» in the negative bucket.
    positive_scan = re.sub(
        r"\bне\s+(?:работа\w*|восстанов\w*|появил\w*|включил\w*)",
        "",
        lowered,
    )
    positive = any(
        marker in positive_scan
        for marker in ("восстанов", "появил", "подача есть", "работает", "доступн", "включил")
    )
    negative = bool(re.search(r"\bнет(?:\W|$)", lowered)) or any(
        marker in lowered
        for marker in (
            "отсутств",
            "ограничен",
            "отключ",
            "пропал",
            "не работа",
            "не восстанов",
            "не пода",
        )
    )
    if positive and not negative:
        return "positive"
    if negative and not positive:
        return "negative"
    return ""


def _paragraph_story_ids(paragraph: object, context: ArticleEditorialContext) -> tuple[str, ...]:
    stories: list[str] = []
    for sid in _support_ids_for_unit(paragraph):
        support = context.support_by_id.get(sid)
        if support is None:
            continue
        story_id = _support_story_id(support)
        if story_id and story_id not in stories:
            stories.append(story_id)
    return tuple(stories)


def _patch_target_for_missing_story(
    draft: StructuredArticleDraft,
    plan: ArticleCoveragePlan,
    story_id: str,
) -> str:
    section = plan.section_for_story(story_id)
    if section is not None:
        for index, draft_section in enumerate(draft.sections, start=1):
            if draft_section.heading.casefold() == section.title.casefold():
                if draft_section.paragraphs:
                    prior = sum(len(s.paragraphs) for s in draft.sections[: index - 1])
                    return f"P{prior + 1:03d}"
    # A heading cannot receive a missing story's prose.  Select an existing
    # paragraph wherever one exists, then the lead/title as content-bearing
    # units.  This keeps every finding patchable without inventing an empty H
    # target.
    prior = 0
    for draft_section in draft.sections:
        if draft_section.paragraphs:
            return f"P{prior + 1:03d}"
        prior += len(draft_section.paragraphs)
    # LEAD is the only body-capable editor target left when the draft has no
    # paragraphs; TITLE can change a headline but cannot add missing coverage.
    return "LEAD"


def _diagnose_contradictions(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None = None,
    place_resolver: Any | None = None,
) -> list[ArticleReaderQualityFinding]:
    # Compare what the draft actually claims.  Conflicting raw reports are
    # publishable when the prose describes their local variation; source text
    # alone must never turn a correctly framed contrast into a rejection.
    claim_records: list[tuple[str, tuple[str, ...], str]] = []
    if draft.lead_claims:
        claim_records.extend(
            (claim.text, claim.cited_support_ids, "LEAD") for claim in draft.lead_claims
        )
    elif draft.lead:
        claim_records.append((draft.lead, draft.lead_support_ids, "LEAD"))
    p_idx = 1
    for section in draft.sections:
        for paragraph in section.paragraphs:
            unit_id = f"P{p_idx:03d}"
            if paragraph.claims:
                claim_records.extend(
                    (claim.text, claim.cited_support_ids, unit_id) for claim in paragraph.claims
                )
            elif paragraph.text:
                claim_records.append((paragraph.text, paragraph.cited_support_ids, unit_id))
            p_idx += 1

    support_by_id = context.support_by_id
    unit_texts: dict[str, list[str]] = {}
    if draft.lead:
        unit_texts["LEAD"] = [draft.lead]
    p_idx = 1
    for section in draft.sections:
        for paragraph in section.paragraphs:
            unit_texts.setdefault(f"P{p_idx:03d}", []).append(paragraph.text)
            p_idx += 1

    def has_explicit_temporal_difference(first: str, second: str) -> bool:
        first_markers = {
            marker for marker, pattern in _TEMPORAL_MARKERS.items() if pattern.search(first)
        }
        second_markers = {
            marker for marker, pattern in _TEMPORAL_MARKERS.items() if pattern.search(second)
        }
        return any(
            frozenset((first_marker, second_marker)) in _TEMPORAL_VARIATION_PAIRS
            for first_marker in first_markers
            for second_marker in second_markers
        )

    def explicitly_framed_variation(
        first_text: str,
        second_text: str,
        *unit_ids: str,
    ) -> bool:
        texts: list[str] = []
        for unit_id in unit_ids:
            texts.extend(unit_texts.get(unit_id, ()))
        text = " ".join(texts).casefold()
        explicit = re.search(
            r"(?:сведения\s+расходят|сообщени\w*\s+расходят|картина\s+неоднород|"
            r"одни\s+[^.]{0,100}\s+други|по\s+одним\s+сообщени|по\s+другим\s+сообщени|"
            r"разные\s+сообщени|сначала\s+[^.]{0,100}\s+(?:позже|затем))",
            text,
        )
        if explicit:
            return True
        if has_explicit_temporal_difference(first_text, second_text):
            return True
        # Connectors such as «тогда как» are useful evidence of a localized
        # contrast only when both claim atoms live in the same prose unit;
        # never let a transition elsewhere in a section excuse an unrelated
        # pair of opposite assertions.
        return len(set(unit_ids)) == 1 and bool(re.search(r"тогда\s+как|в\s+то\s+же\s+время", text))

    def distinguishes_finer_location(text: str) -> bool:
        # Street-level equality is insufficient when the prose names a house,
        # building, entrance, pump, line, or another supply point.  Those
        # reports can be a valid localized contrast even with overlapping
        # effective intervals.
        return bool(
            re.search(
                r"\b(?:друг(?:ом|их)|одн(?:ом|их)|соседн(?:ем|их)|разн(?:ых|ом))\s+"
                r"(?:дом\w*|здан\w*|подъезд\w*|квартир\w*|насос\w*|линий|лини\w*)|"
                r"\b(?:дом\w*\s*№?\s*\d+|насос\w*|линия\w*|подъезд\w*)\b",
                text.casefold(),
            )
        )

    def grounded_location_ids(text: str) -> set[tuple[str, str]]:
        locations: set[tuple[str, str]] = set()
        for match in _LOCATION_ID_RE.finditer(text):
            raw_kind = match.group("kind").casefold().rstrip(".")
            if raw_kind.startswith(("дом", "д", "здан")):
                kind = "building"
            elif raw_kind.startswith("подъезд"):
                kind = "entrance"
            elif raw_kind.startswith("квартир"):
                kind = "apartment"
            elif raw_kind.startswith("насос"):
                kind = "pump"
            elif raw_kind.startswith("лини"):
                kind = "line"
            else:
                kind = "branch"
            locations.add((kind, match.group("value").casefold().replace("ё", "е")))
        return locations

    def has_grounded_location_difference(
        first_text: str,
        second_text: str,
        first_supports: Sequence[ArticleSupport],
        second_supports: Sequence[ArticleSupport],
    ) -> bool:
        if not (
            distinguishes_finer_location(first_text) or distinguishes_finer_location(second_text)
        ):
            return False

        def source_location_ids(supports: Sequence[ArticleSupport]) -> set[tuple[str, str]]:
            return grounded_location_ids(
                " ".join(f"{support.text} {support.source_text}" for support in supports)
            )

        first_grounded = grounded_location_ids(first_text) & source_location_ids(first_supports)
        second_grounded = grounded_location_ids(second_text) & source_location_ids(second_supports)
        return any(
            first_kind == second_kind and first_value != second_value
            for first_kind, first_value in first_grounded
            for second_kind, second_value in second_grounded
        )

    findings: list[ArticleReaderQualityFinding] = []
    seen_contradictions: set[tuple[frozenset[str], str, str]] = set()
    for index, (first_text, first_ids, first_unit) in enumerate(claim_records):
        first_service, first_place = _service_and_place(first_text, place_resolver)
        first_state = _state_polarity(first_text)
        if not first_service or not first_place or not first_state:
            continue
        first_supports = [
            support_by_id[sid]
            for sid in first_ids
            if sid in support_by_id
            and support_by_id[sid].publication_use == "PUBLISH"
            and support_by_id[sid].evidence_kind != "resident_question"
            and _projected_support_is_citable(sid, context, material_projection)
        ]
        for second_text, second_ids, second_unit in claim_records[index + 1 :]:
            second_service, second_place = _service_and_place(second_text, place_resolver)
            second_state = _state_polarity(second_text)
            if (
                first_service != second_service
                or first_place != second_place
                or first_state == second_state
                or not second_state
            ):
                continue
            if explicitly_framed_variation(first_text, second_text, first_unit, second_unit):
                continue
            second_supports = [
                support_by_id[sid]
                for sid in second_ids
                if sid in support_by_id
                and support_by_id[sid].publication_use == "PUBLISH"
                and support_by_id[sid].evidence_kind != "resident_question"
                and _projected_support_is_citable(sid, context, material_projection)
            ]
            if not any(
                _time_overlap(first_support, second_support)
                for first_support in first_supports
                for second_support in second_supports
            ):
                continue
            if has_grounded_location_difference(
                first_text, second_text, first_supports, second_supports
            ):
                continue
            support_ids = tuple(dict.fromkeys((*first_ids, *second_ids)))
            contradiction_key = (frozenset(support_ids), first_service, first_place)
            if contradiction_key in seen_contradictions:
                continue
            seen_contradictions.add(contradiction_key)
            findings.append(
                ArticleReaderQualityFinding(
                    code="CONTRADICTORY_SERVICE_STATE",
                    unit_id=first_unit
                    if first_unit.startswith(("P", "L", "T", "H"))
                    else second_unit,
                    message=(
                        "В тексте рядом оказались противоположные состояния одной услуги "
                        "в одном месте и в перекрывающийся период; передайте локальный контраст явно."
                    ),
                    support_ids=support_ids,
                    severity="blocking",
                )
            )
    return findings


def diagnose_article_quality(
    draft: StructuredArticleDraft,
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None = None,
    place_resolver: Any | None = None,
) -> ArticleReaderQualityReport:
    findings: list[ArticleReaderQualityFinding] = []

    heading_units: list[tuple[str, str, tuple[str, ...]]] = [
        (
            "TITLE",
            draft.title,
            _citable_support_ids(
                (
                    *draft.title_support_ids,
                    *(sid for claim in draft.title_claims for sid in claim.cited_support_ids),
                ),
                context,
                material_projection,
            ),
        )
    ]
    for section_index, section in enumerate(draft.sections, start=1):
        heading_units.append(
            (
                f"H{section_index:03d}",
                section.heading,
                _citable_support_ids(
                    (
                        *section.heading_support_ids,
                        *(
                            sid
                            for claim in section.heading_claims
                            for sid in claim.cited_support_ids
                        ),
                    ),
                    context,
                    material_projection,
                ),
            )
        )
    duplicate_headings: dict[str, list[str]] = {}
    for index, (first_id, first_text, first_support_ids) in enumerate(heading_units):
        normalized = _normalized_heading(first_text)
        if not normalized:
            continue
        for later_id, later_text, later_support_ids in heading_units[index + 1 :]:
            if normalized != _normalized_heading(later_text):
                continue
            target_id = later_id if later_id.startswith("H") else first_id
            if target_id.startswith("H"):
                duplicate_headings.setdefault(target_id, []).extend(
                    (*first_support_ids, *later_support_ids)
                )
    for target_id, support_ids in duplicate_headings.items():
        findings.append(
            ArticleReaderQualityFinding(
                code="DUPLICATE_ARTICLE_HEADING",
                unit_id=target_id,
                message="Заголовок повторяет название статьи или другой главы; уточните его тему.",
                support_ids=tuple(dict.fromkeys(support_ids)),
                severity="blocking",
            )
        )

    # A lead may introduce a DEVELOP storyline, but it should not be the only
    # place where that supported storyline appears.  The coverage diagnostics
    # validate claim-to-Story links here; prose style alone cannot trigger it.
    lead_only = diagnose_article_coverage(
        StructuredArticleDraft(
            title="",
            title_support_ids=(),
            lead=draft.lead,
            lead_support_ids=draft.lead_support_ids,
            lead_claims=draft.lead_claims,
            sections=(),
        ),
        coverage_plan,
        context=context,
    )
    body_only = diagnose_article_coverage(
        StructuredArticleDraft(
            title="",
            title_support_ids=(),
            lead="",
            lead_support_ids=(),
            sections=draft.sections,
        ),
        coverage_plan,
        context=context,
    )
    suppressed_story_ids = set(
        getattr(material_projection, "suppressed_story_ids", ()) if material_projection else ()
    )
    lead_citable_story_ids = set(
        _claim_story_ids(
            _citable_support_ids(
                tuple(sid for claim in draft.lead_claims for sid in claim.cited_support_ids),
                context,
                material_projection,
            ),
            context,
        )
    )
    body_claim_support_ids = tuple(
        sid
        for section in draft.sections
        for paragraph in section.paragraphs
        for claim in _unit_claims(paragraph, paragraph.text, _support_ids_for_unit(paragraph))
        for sid in claim[1]
    )
    body_citable_story_ids = set(
        _claim_story_ids(
            _citable_support_ids(body_claim_support_ids, context, material_projection), context
        )
    )
    body_story_ids = set(body_only.covered_story_ids) & body_citable_story_ids
    for story in coverage_plan.stories:
        if (
            story.prominence != "DEVELOP"
            or story.story_id in suppressed_story_ids
            or story.story_id not in lead_only.covered_story_ids
            or story.story_id not in lead_citable_story_ids
            or story.story_id in body_story_ids
        ):
            continue
        story_support_ids = set(story.support_ids) | set(story.detail_support_ids)
        lead_support_ids = tuple(
            dict.fromkeys(
                sid
                for claim in draft.lead_claims
                for sid in claim.cited_support_ids
                if sid in story_support_ids
                and _projected_support_is_citable(sid, context, material_projection)
            )
        )
        if not lead_support_ids:
            continue
        findings.append(
            ArticleReaderQualityFinding(
                code="UNDEVELOPED_LEAD_PROMISE",
                unit_id="LEAD",
                message=(
                    f"Вводная часть обещает раскрыть ключевой сюжет «{story.topic}», "
                    "но основной текст к нему не возвращается."
                ),
                support_ids=lead_support_ids,
                severity="blocking",
            )
        )

    # A run is actionable only when it looks like one paragraph per unrelated
    # Story.  A single short paragraph is a valid compact mention.
    p_idx = 1
    for section in draft.sections:
        paragraphs = section.paragraphs
        run_start = 0
        while run_start < len(paragraphs):
            run_end = run_start
            story_ids: list[str] = []
            while run_end < len(paragraphs):
                paragraph = paragraphs[run_end]
                if len(_split_sentences_safe(paragraph.text)) != 1:
                    break
                stories = _paragraph_story_ids(paragraph, context)
                if len(stories) != 1 or stories[0] in story_ids:
                    break
                story_ids.append(stories[0])
                run_end += 1
            if run_end - run_start >= 3:
                for offset in range(run_start, run_end):
                    paragraph = paragraphs[offset]
                    findings.append(
                        ArticleReaderQualityFinding(
                            code="ARTICLE_INVENTORY_RHYTHM",
                            unit_id=f"P{p_idx + offset:03d}",
                            message=(
                                "Несколько однофактных абзацев подряд читаются как перечень; "
                                "свяжите соседние сюжеты плавным переходом и сохраните детали."
                            ),
                            support_ids=_support_ids_for_unit(paragraph),
                            severity="blocking",
                        )
                    )
            run_start = max(run_end, run_start + 1)
        p_idx += len(paragraphs)

    # Quote rolls are local paragraph repairs; the existing fact validator
    # remains responsible for whether each quote is source-supported.
    p_idx = 1
    for section in draft.sections:
        for paragraph in section.paragraphs:
            if len(_QUOTE_RE.findall(paragraph.text)) > 2:
                findings.append(
                    ArticleReaderQualityFinding(
                        code="QUOTE_ROLL_PARAGRAPH",
                        unit_id=f"P{p_idx:03d}",
                        message="В одном абзаце больше двух прямых цитат; объедините сообщения косвенной речью.",
                        support_ids=_support_ids_for_unit(paragraph),
                        # The editor should repair this locally, but an
                        # unresolved quote roll violates the reader-facing
                        # quote contract and must not pass finalization.
                        severity="blocking",
                    )
                )
            p_idx += 1

    # A packed roster is a sentence/claim-level shape, not a paragraph-wide
    # length or address quota.  A paragraph may weave several small place-based
    # reports together without making any one sentence a roster.
    p_idx = 1
    address_roster_support_ids: list[str] = []
    for section in draft.sections:
        for paragraph in section.paragraphs:
            if paragraph.claims:
                sentence_supports = (
                    (
                        sentence,
                        _citable_support_ids(
                            claim.cited_support_ids
                            if claim.cited_support_ids
                            else _support_ids_for_unit(paragraph),
                            context,
                            material_projection,
                        ),
                    )
                    for claim in paragraph.claims
                    for sentence in _split_sentences_safe(claim.text)
                )
            else:
                paragraph_support_ids = _citable_support_ids(
                    _support_ids_for_unit(paragraph),
                    context,
                    material_projection,
                )
                sentence_supports = (
                    (sentence, paragraph_support_ids)
                    for sentence in _split_sentences_safe(paragraph.text)
                )

            overloaded_support_ids: list[str] = []
            for sentence, sentence_support_ids in sentence_supports:
                if not sentence_support_ids:
                    continue
                source_observations: list[tuple[str, str, str, ArticleSupport]] = []
                seen_observations: set[tuple[str, str, str, str]] = set()
                for support_id in sentence_support_ids:
                    support = context.support_by_id[support_id]
                    source_text = " ".join((support.text, support.source_text)).strip()
                    service, place = _service_and_place(source_text, place_resolver)
                    state = _state_polarity(source_text)
                    if not service or not state:
                        continue
                    source_places = (
                        {place} if place else _extract_place_keys(source_text, place_resolver)
                    )
                    for source_place in source_places:
                        observation_key = (
                            service,
                            source_place,
                            state,
                            support.support_id,
                        )
                        if observation_key not in seen_observations:
                            seen_observations.add(observation_key)
                            source_observations.append((service, source_place, state, support))

                sentence_places = _extract_place_keys(sentence, place_resolver)
                if (
                    len(sentence_places) >= 4
                    and len(source_observations) >= 4
                    and not _has_narrative_relation(sentence, source_observations, place_resolver)
                ):
                    overloaded_support_ids.extend(sentence_support_ids)

            if overloaded_support_ids:
                findings.append(
                    ArticleReaderQualityFinding(
                        code="OVERLOADED_ROSTER_PARAGRAPH",
                        unit_id=f"P{p_idx:03d}",
                        message=(
                            "В одном абзаце собран перечень разных адресов и состояний без "
                            "связующего сравнения; сгруппируйте наблюдения и сохраните важные различия."
                        ),
                        support_ids=tuple(dict.fromkeys(overloaded_support_ids)),
                        severity="blocking",
                    )
                )

            paragraph_observations = _paragraph_service_observations(
                paragraph, context, material_projection, place_resolver
            )
            claim_sentences = (
                [
                    (sentence, claim.cited_support_ids or _support_ids_for_unit(paragraph))
                    for claim in paragraph.claims
                    for sentence in _split_sentences_safe(claim.text)
                ]
                if paragraph.claims
                else [
                    (sentence, _support_ids_for_unit(paragraph))
                    for sentence in _split_sentences_safe(paragraph.text)
                ]
            )
            address_status_records: list[tuple[str, str, str, tuple[str, ...]]] = []
            for sentence, sentence_support_ids in claim_sentences:
                service, place = _service_and_place(sentence, place_resolver)
                state = _state_polarity(sentence)
                if not service or not place or not state:
                    continue
                grounded = any(
                    observed_service == service
                    and observed_place == place
                    and observed_state == state
                    and support.support_id in sentence_support_ids
                    for observed_service, observed_place, observed_state, support in paragraph_observations
                )
                if grounded:
                    address_status_records.append(
                        (service, place, state, tuple(sentence_support_ids))
                    )

            roster_services = {service for service, _place, _state, _ids in address_status_records}
            roster_places = {place for _service, place, _state, _ids in address_status_records}
            if (
                len(claim_sentences) >= 3
                and len(address_status_records) >= 3
                and len(roster_places) >= 3
                and len(roster_services) == 1
                and not _has_narrative_relation(
                    paragraph.text, paragraph_observations, place_resolver
                )
            ):
                address_roster_support_ids.extend(
                    support_id
                    for _service, _place, _state, ids in address_status_records
                    for support_id in ids
                )
            p_idx += 1

    if address_roster_support_ids:
        findings.append(
            ArticleReaderQualityFinding(
                code="MULTI_SENTENCE_ADDRESS_STATUS_ROSTER",
                unit_id="ARTICLE",
                message=(
                    "Адреса и состояния услуги разнесены по однотипным предложениям без "
                    "подтверждённого сравнения или развития; соберите сообщения в связный локальный контраст."
                ),
                support_ids=tuple(dict.fromkeys(address_roster_support_ids)),
                severity="blocking",
            )
        )

    directory_findings: list[str] = []
    for section in draft.sections:
        paragraph_results = [
            (
                paragraph,
                _paragraph_is_directory_timetable_material(paragraph, context, material_projection),
            )
            for paragraph in section.paragraphs
        ]
        directory_paragraphs = [
            paragraph for paragraph, is_directory in paragraph_results if is_directory
        ]
        if not directory_paragraphs:
            continue
        directory_count = len(directory_paragraphs)
        total_count = len(section.paragraphs)
        dense_single_paragraph = any(
            len(_ARTICLE_TIME_RANGE_RE.findall(paragraph.text)) >= 3
            or len(_STREET_RE.findall(paragraph.text)) >= 3
            for paragraph in directory_paragraphs
        )
        if dense_single_paragraph or (
            directory_count >= 2 and directory_count / max(total_count, 1) >= 0.6
        ):
            directory_findings.extend(
                support_id
                for paragraph in directory_paragraphs
                for support_id in _support_ids_for_unit(paragraph)
            )
    if directory_findings:
        findings.append(
            ArticleReaderQualityFinding(
                code="DIRECTORY_TIMETABLE_SECTION",
                unit_id="ARTICLE",
                message=(
                    "Раздел в основном перечисляет адреса и обычные часы работы; оставьте только "
                    "изменение, полезное жителям, и уберите каталог."
                ),
                support_ids=tuple(dict.fromkeys(directory_findings)),
                severity="blocking",
            )
        )

    # Compare supported claims across the lead and distinct chapters.  Shared
    # Story/support linkage is required, and a different state, effective
    # interval, explicit time progression, or consequence is reader value.
    quality_units: list[dict[str, Any]] = []
    if draft.lead:
        quality_units.append(
            {
                "unit_id": "LEAD",
                "section_index": -1,
                "text": draft.lead,
                "support_ids": tuple(draft.lead_support_ids),
                "claims": _unit_claims(draft, draft.lead, draft.lead_support_ids),
            }
        )
    p_idx = 1
    for section_index, section in enumerate(draft.sections):
        for paragraph in section.paragraphs:
            unit_support_ids = _support_ids_for_unit(paragraph)
            quality_units.append(
                {
                    "unit_id": f"P{p_idx:03d}",
                    "section_index": section_index,
                    "text": paragraph.text,
                    "support_ids": unit_support_ids,
                    "claims": _unit_claims(paragraph, paragraph.text, unit_support_ids),
                }
            )
            p_idx += 1
    repetition_findings: dict[str, tuple[list[str], set[str]]] = {}
    for index, first_unit in enumerate(quality_units):
        for later_unit in quality_units[index + 1 :]:
            if first_unit["section_index"] == later_unit["section_index"]:
                continue
            duplicate_support_ids: set[str] = set()
            repeated_story_ids: set[str] = set()
            for first_text, first_ids in first_unit["claims"]:
                first_citable_ids = _citable_support_ids(first_ids, context, material_projection)
                first_stories = set(_claim_story_ids(first_citable_ids, context))
                for later_text, later_ids in later_unit["claims"]:
                    later_citable_ids = _citable_support_ids(
                        later_ids, context, material_projection
                    )
                    later_stories = set(_claim_story_ids(later_citable_ids, context))
                    shared_stories = first_stories & later_stories
                    shared_supports = set(first_citable_ids) & set(later_citable_ids)
                    if not shared_stories and not shared_supports:
                        continue
                    if _claim_is_materially_new(first_text, later_text):
                        continue
                    if _supports_show_distinct_effective_times(
                        first_citable_ids,
                        later_citable_ids,
                        context,
                        material_projection,
                    ):
                        continue
                    duplicate_support_ids.update((*first_citable_ids, *later_citable_ids))
                    repeated_story_ids.update(shared_stories)
            if not duplicate_support_ids:
                continue
            existing = repetition_findings.get(later_unit["unit_id"])
            if existing is None:
                repetition_findings[later_unit["unit_id"]] = (
                    list(duplicate_support_ids),
                    repeated_story_ids,
                )
            else:
                existing[0].extend(duplicate_support_ids)
                existing[1].update(repeated_story_ids)
    for unit_id, (repeated_support_ids, repeated_story_ids) in repetition_findings.items():
        major_story_repeated = any(
            coverage_plan.by_story_id.get(story_id) is not None
            and coverage_plan.by_story_id[story_id].prominence == "DEVELOP"
            for story_id in repeated_story_ids
        )
        findings.append(
            ArticleReaderQualityFinding(
                code="CROSS_SECTION_REPETITION",
                unit_id=unit_id,
                message=(
                    "Этот поддержанный факт уже прозвучал в другой части статьи без нового "
                    "состояния, времени или последствия."
                ),
                support_ids=tuple(dict.fromkeys(repeated_support_ids)),
                severity="blocking" if major_story_repeated else "repair",
            )
        )

    lead_unit = next((unit for unit in quality_units if unit["unit_id"] == "LEAD"), None)
    body_units = [unit for unit in quality_units if unit["unit_id"].startswith("P")]
    if lead_unit is not None and len(body_units) >= 2:
        middle_units, closing_unit = body_units[:-1], body_units[-1]
        repeated_thesis_supports: list[str] = []
        for lead_text, lead_ids in lead_unit["claims"]:
            lead_citable_ids = _citable_support_ids(lead_ids, context, material_projection)
            lead_story_ids = set(_claim_story_ids(lead_citable_ids, context))
            for story_id in lead_story_ids:
                lead_story = coverage_plan.by_story_id.get(story_id)
                if (
                    lead_story is None
                    or lead_story.prominence != "DEVELOP"
                    or story_id in suppressed_story_ids
                ):
                    continue

                def matching_claims(
                    unit: dict[str, Any],
                    target_story_id: str = story_id,
                ) -> list[tuple[str, tuple[str, ...]]]:
                    return [
                        (text, _citable_support_ids(ids, context, material_projection))
                        for text, ids in unit["claims"]
                        if target_story_id
                        in _claim_story_ids(
                            _citable_support_ids(ids, context, material_projection), context
                        )
                    ]

                closing_matches = matching_claims(closing_unit)
                middle_matches = [
                    (unit, claim) for unit in middle_units for claim in matching_claims(unit)
                ]
                for closing_text, closing_ids in closing_matches:
                    if _claim_is_materially_new(
                        lead_text, closing_text
                    ) or _supports_show_distinct_effective_times(
                        lead_citable_ids, closing_ids, context, material_projection
                    ):
                        continue
                    for _middle_unit, (middle_text, middle_ids) in middle_matches:
                        if (
                            not _claim_is_materially_new(lead_text, middle_text)
                            and not _claim_is_materially_new(middle_text, closing_text)
                            and not _supports_show_distinct_effective_times(
                                lead_citable_ids, middle_ids, context, material_projection
                            )
                            and not _supports_show_distinct_effective_times(
                                middle_ids, closing_ids, context, material_projection
                            )
                        ):
                            repeated_thesis_supports.extend(
                                (*lead_citable_ids, *middle_ids, *closing_ids)
                            )
                            break
                    if repeated_thesis_supports:
                        break
                if repeated_thesis_supports:
                    break

        if repeated_thesis_supports:
            findings.append(
                ArticleReaderQualityFinding(
                    code="REPEATED_CENTRAL_THESIS",
                    unit_id="ARTICLE",
                    message=(
                        "Центральная мысль о ключевом сюжете повторена во вступлении, основной части "
                        "и концовке без нового состояния, периода или последствия."
                    ),
                    support_ids=tuple(dict.fromkeys(repeated_thesis_supports)),
                    severity="blocking",
                )
            )

    coverage = diagnose_article_coverage(draft, coverage_plan, context=context)
    suppressed_ids = set(
        getattr(material_projection, "suppressed_story_ids", ()) if material_projection else ()
    )
    for story in coverage_plan.stories:
        if story.prominence != "DEVELOP" or story.story_id in suppressed_ids:
            continue
        if story.story_id not in coverage.uncovered_story_ids:
            continue
        missing_story_support_ids = tuple(
            sid
            for sid in (*story.support_ids, *story.detail_support_ids)
            if sid in context.support_by_id
            and _support_story_id(context.support_by_id[sid]) == story.story_id
            and _projected_support_is_citable(sid, context, material_projection)
        )
        if not missing_story_support_ids:
            continue
        findings.append(
            ArticleReaderQualityFinding(
                code="MISSING_DEVELOP_STORY",
                unit_id=_patch_target_for_missing_story(draft, coverage_plan, story.story_id),
                message=(
                    f"Ключевой сюжет «{story.topic}» не раскрыт; добавьте его в этот фрагмент "
                    "по указанным подтверждениям."
                ),
                support_ids=tuple(dict.fromkeys(missing_story_support_ids)),
                severity="blocking",
            )
        )

    # Details are useful repair hints, but their omission alone is not a
    # publication blocker. Only emit this for an otherwise covered story so a
    # missing DEVELOP finding remains the single actionable signal for a whole
    # omitted storyline.
    uncovered_details = set(coverage.uncovered_detail_support_ids)
    unit_index = _support_unit_index(draft)
    for story in coverage_plan.stories:
        if story.story_id in suppressed_ids or story.story_id in coverage.uncovered_story_ids:
            continue
        for support_id in story.detail_support_ids:
            if support_id not in uncovered_details or not _projected_support_is_citable(
                support_id, context, material_projection
            ):
                continue
            target = next(
                (
                    unit_id
                    for cited_id, unit_ids in unit_index.items()
                    if cited_id == support_id
                    for unit_id in unit_ids
                    if unit_id.startswith(("P", "L", "T", "H"))
                ),
                None,
            )
            findings.append(
                ArticleReaderQualityFinding(
                    code="MISSING_DETAIL_SUPPORT",
                    unit_id=target
                    or _patch_target_for_missing_story(draft, coverage_plan, story.story_id),
                    message=(
                        f"В сюжете «{story.topic}» не использована запланированная конкретная деталь; "
                        "сохраните её, если она помогает читателю понять повседневные последствия."
                    ),
                    support_ids=(support_id,),
                    severity="repair",
                )
            )

    findings.extend(_diagnose_contradictions(draft, context, material_projection, place_resolver))
    # Stable order makes metadata and editor retries reproducible.
    return ArticleReaderQualityReport(findings=tuple(findings))
