"""Deterministic reader-quality diagnostics for Event-First article drafts.

The checks in this module are intentionally narrow.  They identify prose units
that a bounded copy-editing pass can repair while leaving Evidence Boundary
validation in ``article_validator`` as the authority for factual safety.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import diagnose_article_coverage
from src.publication.article_material import ArticleMaterialProjection
from src.publication.article_models import StructuredArticleDraft, _split_sentences_safe

QualitySeverity = Literal["repair", "warning", "blocking"]

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
            "version": "article-reader-quality-v2",
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
                        severity="repair",
                    )
                )
            p_idx += 1

    coverage = diagnose_article_coverage(draft, coverage_plan, context=context)
    suppressed_ids = set(
        getattr(material_projection, "suppressed_story_ids", ()) if material_projection else ()
    )
    for story in coverage_plan.stories:
        if story.prominence != "DEVELOP" or story.story_id in suppressed_ids:
            continue
        if story.story_id not in coverage.uncovered_story_ids:
            continue
        support_ids = tuple(
            sid
            for sid in (*story.support_ids, *story.detail_support_ids)
            if sid in context.support_by_id
            and _support_story_id(context.support_by_id[sid]) == story.story_id
            and _projected_support_is_citable(sid, context, material_projection)
        )
        if not support_ids:
            continue
        findings.append(
            ArticleReaderQualityFinding(
                code="MISSING_DEVELOP_STORY",
                unit_id=_patch_target_for_missing_story(draft, coverage_plan, story.story_id),
                message=(
                    f"Ключевой сюжет «{story.topic}» не раскрыт; добавьте его в этот фрагмент "
                    "по указанным подтверждениям."
                ),
                support_ids=tuple(dict.fromkeys(support_ids)),
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
