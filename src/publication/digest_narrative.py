"""Deterministic block planning, models, validation, and single-call writer for Event-First narrative digests."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from src.editorial_models import StoryCard
from src.publication.article_claims import (
    ConcreteClaim,
    extract_concrete_claims,
    find_unsupported_claims,
)
from src.publication.digest_presentation import (
    RequiredDigestFact,
    validate_digest_fact_ids,
)
from src.publication.errors import DigestCoverageInvariantError
from src.publication.evidence import PublicationEvidence

logger = logging.getLogger(__name__)

DIGEST_COMPOSITION_MEMBERSHIP_VERSION = "digest_membership_v2"

_INTERNAL_LEAKAGE_RE = re.compile(r"\[(?:story:\d+|SUPPORT\s+\d+|ref-\d+|tg:\S+)\]", re.IGNORECASE)
_INTERNAL_REPLY_ANNOTATION_RE = re.compile(
    r'\s*\(in_reply_to:\s*".*"\)\s*$', re.IGNORECASE | re.DOTALL
)
_DIGEST_ATTRIBUTION_RE = re.compile(
    r"\b(?:"
    r"(?:по\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев))"
    r"|(?:(?:местный\s+)?жител(?:и|ь)|горожан(?:е|ин)|очевид(?:цы|ец))\s+(?:сообща(?:ют|ет)|пиш(?:ут|ет)|отмеча(?:ют|ет)|жалу(?:ются|ется))"
    r")\s*(?:,|:)??\s*(?:что\s+)?",
    re.IGNORECASE,
)
_DIGEST_LEADING_ATTRIBUTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:по\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев))"
    r"|(?:(?:местный\s+)?жител(?:и|ь)|горожан(?:е|ин)|очевид(?:цы|ец))\s+(?:сообща(?:ют|ет)|пиш(?:ут|ет)|отмеча(?:ют|ет)|жалу(?:ются|ется))"
    r")\s*(?:,|:)??\s*(?:что\s+)?",
    re.IGNORECASE,
)
_GENERIC_DIGEST_TOPIC_RE = re.compile(
    r"^(?:в\s+фокусе\s+внимания|городское?\s+событие|городские\s+события|"
    r"коммунальная\s+сфера|городские\s+службы|текущая\s+обстановка|"
    r"ситуация|разное|другое)$",
    re.IGNORECASE,
)


def _sanitize_digest_support_text(text: str) -> str:
    """Remove Event-First reply metadata before projecting evidence to readers."""
    return _INTERNAL_REPLY_ANNOTATION_RE.sub("", text or "").strip()


def _strip_leading_digest_attribution(text: str) -> str:
    """Remove a leading community-attribution phrase from a reader headline."""
    cleaned = _DIGEST_LEADING_ATTRIBUTION_RE.sub("", text or "", count=1).strip()
    if not cleaned:
        return text.strip()
    return cleaned[:1].upper() + cleaned[1:]


def _deduplicate_digest_attribution(text: str) -> str:
    """Keep the first attribution in a fallback item and remove repeated ones."""
    seen = False

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        if not seen:
            seen = True
            return match.group(0)
        return ""

    cleaned = _DIGEST_ATTRIBUTION_RE.sub(replace, text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"([.!?])\s*,", r"\1", cleaned)
    return cleaned.strip()


def _headline_from_digest_fact(fact: str) -> str:
    """Use a grounded fact as a headline when the persisted topic is generic."""
    from src.publication.digest_presentation import _clean_fact_sentence

    cleaned = _clean_fact_sentence(fact)
    cleaned = re.sub(r"^(?:что|а)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"^(?:по\s+сообщениям\s+жителей|по\s+словам\s+горожан)[\s,:]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned.rstrip(". ")


def _strip_redundant_headline_from_body(headline: str, body: str) -> str:
    """Strip redundant verbatim repetition of headline from the beginning of body."""
    norm_h = " ".join((headline or "").casefold().split()).strip(" .,:;!-–—")
    norm_b = (body or "").strip()
    if not norm_h or not norm_b:
        return norm_b

    if norm_b.casefold().startswith(norm_h):
        stripped = norm_b[len(norm_h) :].lstrip(" ,:.-–—")
        # Strip leading conversational connectors like 'но ', 'а ', 'что '
        stripped = re.sub(r"^(?:но|а|что)\s+", "", stripped, flags=re.IGNORECASE).strip()
        if stripped:
            return stripped[:1].upper() + stripped[1:]

    att_m = re.match(
        r"^(по\s+(?:сообщениям\s+жителей|словам\s+горожан|информации\s+коммунальных\s+служб)[,\s:]*)",
        norm_b,
        flags=re.IGNORECASE,
    )
    if att_m:
        prefix = att_m.group(1)
        rest = norm_b[len(prefix) :].strip()
        if rest.casefold().startswith(norm_h):
            stripped = rest[len(norm_h) :].lstrip(" ,:.-–—")
            stripped = re.sub(r"^(?:но|а|что)\s+", "", stripped, flags=re.IGNORECASE).strip()
            if stripped:
                return f"{prefix}{stripped[:1].lower() + stripped[1:]}"

    parts = re.split(r"([.!?]\s+)", norm_b, maxsplit=1)
    if len(parts) >= 3:
        first_sent = parts[0].strip(" .,:;!-–—")
        if first_sent.casefold() == norm_h:
            remaining = parts[2].strip()
            if remaining:
                return remaining

    return norm_b


def _fix_redundant_headline_and_body(
    headline: str,
    body: str,
    topic_label: str = "",
) -> tuple[str, str]:
    """Ensure headline and body do not trigger REDUNDANT_HEADLINE_IN_BODY."""
    from src.publication.digest_quality_diagnostics import _check_redundant_headline_in_body

    if not _check_redundant_headline_in_body(headline, body):
        return headline, body

    # 1. Try stripping headline from beginning of body if a clean sentence remains
    stripped_b = _strip_redundant_headline_from_body(headline, body)
    if (
        stripped_b
        and len(stripped_b) >= 15
        and not _check_redundant_headline_in_body(headline, stripped_b)
    ):
        return headline, stripped_b

    # 2. Differentiate the headline by framing it with the topic label
    lbl = (topic_label or "").strip()
    if lbl and not _GENERIC_DIGEST_TOPIC_RE.fullmatch(lbl):
        new_headline = lbl
        if not _check_redundant_headline_in_body(new_headline, body):
            return new_headline, body

    # 3. Clean leading conversational or attribution noise from headline
    clean_h = re.sub(
        r"^(?:по\s+(?:информации|словам|сообщениям|данным)\s+[^\s,:]+[\s,:]*|(?:сообщается|пишут|отмечают)[,\s]*(?:что\s+)?|жител\w*\s+сообща\w*[\s,:]*(?:что\s+)?|в\s+городе\s+)",
        "",
        headline,
        flags=re.IGNORECASE,
    ).strip()
    if clean_h and len(clean_h) >= 10:
        clean_h = clean_h[0].upper() + clean_h[1:]
        first_clause = re.split(r"[,;—–]", clean_h)[0].strip()
        if len(first_clause) >= 10 and not _check_redundant_headline_in_body(first_clause, body):
            return first_clause, body
        if not _check_redundant_headline_in_body(clean_h, body):
            return clean_h, body

    # 4. NEVER return a generic placeholder like "Городские события" or "Новости города".
    # If topic_label is valid and specific, use it. Otherwise, preserve the original headline.
    if lbl and not _GENERIC_DIGEST_TOPIC_RE.fullmatch(lbl):
        return lbl, body

    return headline, body


_HEADLINE_ATTRIBUTION_PREFIX_RE = re.compile(
    r"^(?:(?:"
    r"по\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев)"
    r"|(?:(?:местный\s+)?жител(?:и|ь)|горожан(?:е|ин)|очевид(?:цы|ец))\s+(?:сообща(?:ют|ет)|пиш(?:ут|ет)|отмеча(?:ют|ет)|жалу(?:ются|ется)|делят(?:ся|ся)|рассказыва(?:ют|ет))"
    r"|(?:в\s+(?:соцсетях|местных\s+пабликах|сети|каналах)\s+(?:пишут|сообщают|появились))"
    r"|сообщают\s+(?:жители|горожане|очевидцы)"
    r")\s*(?:о\s+|об\s+|про\s+)?[,:]?\s*(?:что\s+)?)",
    re.IGNORECASE,
)

_BODY_ATTRIBUTION_PREFIX_RE = re.compile(
    r"^(?:(?:"
    r"по\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев)"
    r"|(?:(?:местный\s+)?жител(?:и|ь)|горожан(?:е|ин)|очевид(?:цы|ец))\s+(?:сообща(?:ют|ет)|пиш(?:ут|ет)|отмеча(?:ют|ет)|жалу(?:ются|ется)|делят(?:ся|ся)|рассказыва(?:ют|ет))"
    r")\s*[,:]?\s*(?:что\s+)?)",
    re.IGNORECASE,
)


def _fix_duplicated_attribution(headline: str, body: str) -> tuple[str, str]:
    """Ensure headline and body do not trigger DUPLICATED_ATTRIBUTION."""
    from src.publication.digest_quality_diagnostics import _check_duplicated_attribution

    if not _check_duplicated_attribution(headline, body):
        return headline, body

    # 1. Prefer stripping conversational attribution from headline so headline is a crisp subject
    new_headline = _HEADLINE_ATTRIBUTION_PREFIX_RE.sub("", headline).strip()
    if new_headline and len(new_headline) >= 5:
        new_headline = new_headline[0].upper() + new_headline[1:]
        if not _check_duplicated_attribution(new_headline, body):
            return new_headline, body

    # 2. If headline still has attribution, strip attribution from beginning of body
    new_body = _BODY_ATTRIBUTION_PREFIX_RE.sub("", body).strip()
    if new_body and len(new_body) >= 15:
        new_body = new_body[0].upper() + new_body[1:]
        if not _check_duplicated_attribution(headline, new_body):
            return headline, new_body

    return headline, body


def _fix_chat_leaks(text: str) -> str:
    """Normalize internal chat/channel references to natural journalistic attribution."""
    if not text:
        return text
    # Keep community attribution, but remove the technical channel as the
    # apparent source of the report.
    local_channel = r"(?:(?:городском|местном|районном|локальном)\s+)?канале"
    t = re.sub(
        rf"\bпо\s+сообщению\s+в\s+{local_channel}\b",
        "по сообщению жителя",
        text,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\bпо\s+сообщениям\s+в\s+{local_channel}\b",
        "по сообщениям жителей",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\bпубликаци[яи]\s+в\s+{local_channel}\s+указывают,?\s*что",
        "Жители сообщают, что",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\b(?:(позже|ранее)\s+)?в\s+{local_channel}\s+появил(?:ось|ись)\s+сообщени[ея],?\s*что",
        lambda match: (
            f"{match.group(1).capitalize()} жители сообщили, что"
            if match.group(1)
            else "Жители сообщили, что"
        ),
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\bв\s+(?:вечернем|утреннем|дневном)\s+сообщении\s+(?:местного|городского|районного)\s+канала\s+говорится,?\s*что",
        "По сообщениям жителей,",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\bсообщение\s+(?:местного|городского|районного)\s+канала\s+говорит,?\s*что",
        "Жители сообщают, что",
        t,
        flags=re.IGNORECASE,
    )
    # 1. "В городских чатах Бердянска обсуждают" -> "Жители Бердянска обсуждают"
    t = re.sub(
        r"\bв\s+(?:городских\s+|местных\s+|районных\s+)?чатах\s+([А-Яа-яA-Za-z-]+)\s+(обсужда\w*|сообща\w*|пиш\w*)\b",
        r"Жители \1 \2",
        text,
        flags=re.IGNORECASE,
    )
    # 2. "В городских чатах обсуждают" -> "Жители обсуждают"
    t = re.sub(
        r"\bв\s+(?:городских\s+|местных\s+|районных\s+)?чатах\s+(обсужда\w*|сообща\w*|пиш\w*)\b",
        r"Жители \1",
        t,
        flags=re.IGNORECASE,
    )
    # 3. "в городских чатах / в чатах / в чате / в пабликах / в каналах"
    t = re.sub(
        r"\bв\s+(?:городских\s+|местных\s+|районных\s+)?чатах(?:\s+[А-Яа-яA-Za-z-]+)?\b",
        "в городе",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\bв\s+(?:городском\s+|местном\s+|районном\s+)?чате(?:\s+[А-Яа-яA-Za-z-]+)?\b",
        "в городе",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\bв\s+(?:местных\s+)?пабликах\b", "в городе", t, flags=re.IGNORECASE)
    t = re.sub(
        r"\bв\s+(?:социальных\s+сетях|соцсетях|"
        r"(?:городском|местном|районном|локальном)\s+паблике)\b",
        "в городе",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\bв\s+(?:телеграм[- ]каналах|telegram[- ]каналах|каналах|"
        r"(?:городском|местном|районном|локальном)\s+канале)\b",
        "в городе",
        t,
        flags=re.IGNORECASE,
    )
    # 4. "участник(и) чата" -> "жители / очевидцы"
    t = re.sub(r"\bучастники?\s+чата\b", "жители", t, flags=re.IGNORECASE)
    t = re.sub(r"\bучастников\s+чата\b", "жителей", t, flags=re.IGNORECASE)
    t = re.sub(r"\bперекличк[а-я]*\b", "сообщения жителей", t, flags=re.IGNORECASE)
    # Clean whitespace and ensure sentence starts with uppercase
    t = re.sub(r"\s{2,}", " ", t).strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    # Capitalize after sentence ends
    t = re.sub(r"([.!?]\s+)([а-яё])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t


def sanitize_digest_narrative_draft(draft: DigestNarrativeDraft) -> DigestNarrativeDraft:
    """Sanitize all items in a narrative digest draft before quality audit and rendering."""
    from src.publication.digest_quality_diagnostics import _TEMPORAL_CHAIN_RE

    new_blocks = []
    for b in draft.blocks:
        new_items = []
        for it in b.items:
            clean_hl = _TEMPORAL_CHAIN_RE.sub(" ", it.headline)
            clean_hl = _fix_chat_leaks(clean_hl)
            clean_hl = re.sub(r"\s{2,}", " ", clean_hl).strip()

            clean_body = _TEMPORAL_CHAIN_RE.sub(" ", it.body)
            clean_body = _fix_chat_leaks(clean_body)
            clean_body = re.sub(r"\s{2,}", " ", clean_body).strip()

            clean_hl, clean_body = _fix_redundant_headline_and_body(clean_hl, clean_body)
            clean_hl, clean_body = _fix_duplicated_attribution(clean_hl, clean_body)

            new_items.append(
                replace(
                    it,
                    headline=clean_hl,
                    body=clean_body,
                )
            )
        new_blocks.append(replace(b, items=tuple(new_items)))
    return replace(draft, blocks=tuple(new_blocks))


def _clean_str_list(items: Any) -> list[str]:
    """Recursively extract non-empty trimmed strings from single items, lists, sets, or tuples."""
    out: list[str] = []
    if isinstance(items, (str, int)):
        val = str(items).strip()
        if val:
            out.append(val)
    elif isinstance(items, (list, tuple, set)):
        for x in items:
            out.extend(_clean_str_list(x))
    return out


@dataclass(frozen=True)
class DigestNarrativeValidationResult:
    """Outcome of validating a narrative digest draft against a deterministic plan."""

    is_valid: bool
    violations: tuple[str, ...]
    unsupported_claims: tuple[ConcreteClaim, ...]
    not_evaluated: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestNarrativeBlock:
    """Immutable presentation block grouping a fixed subset of rubric story cards."""

    block_id: str
    rubric_id: str
    rubric_title: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    canonical_notes: tuple[str, ...]
    required_facts: tuple[RequiredDigestFact, ...] = ()
    detail_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    merge_group_by_story: tuple[tuple[str, str], ...] = ()
    detail_roles_by_story: tuple[tuple[str, str], ...] = ()
    presentation_modes_by_story: tuple[tuple[str, str], ...] = ()
    dashboard_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    required_story_groups: tuple[tuple[str, ...], ...] = ()
    support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    topic_bundles: tuple[Any, ...] = ()
    # Frozen canonical composition units and their exact fact records. When
    # present, these replace topic-bundle and legacy membership inference.
    composition_units: tuple[Any, ...] = ()
    composition_fact_records: tuple[Any, ...] = ()
    composition_relations: tuple[Any, ...] = ()


@dataclass(frozen=True)
class DigestNarrativePlan:
    """Deterministic plan of immutable narrative digest blocks."""

    blocks: tuple[DigestNarrativeBlock, ...]
    edition_slug: str = ""


def _composition_narrative_plan(
    *,
    cards: Sequence[StoryCard],
    rubrics: Sequence[Any],
    presentation_plan: Any,
    edition_slug: str = "",
) -> DigestNarrativePlan:
    """Project the frozen composition without re-clustering or guessing membership."""
    composition = getattr(presentation_plan, "composition", None)
    presentation_required_facts = tuple(getattr(presentation_plan, "required_facts", ()) or ())
    composition_fact_records = tuple(getattr(composition, "fact_records", ()) or ())
    validate_digest_fact_ids(
        presentation_required_facts,
        error_code="DIGEST_DUPLICATE_REQUIRED_FACT_ID",
    )
    validate_digest_fact_ids(
        composition_fact_records,
        error_code="DIGEST_DUPLICATE_COMPOSITION_FACT_ID",
    )
    units = tuple(getattr(composition, "units", ()) or ())
    if not units:
        if getattr(composition, "admitted_story_ids", ()) or getattr(
            composition, "admitted_fact_ids", ()
        ):
            raise DigestCoverageInvariantError(
                "DIGEST_COMPOSITION_MISSING_UNITS: admitted membership has no units"
            )
        return DigestNarrativePlan(blocks=(), edition_slug=edition_slug)

    rubric_info: list[tuple[str, str, bool]] = []
    for rubric in rubrics:
        if isinstance(rubric, Mapping):
            rubric_info.append(
                (
                    str(rubric.get("id", "")),
                    str(rubric.get("title") or rubric.get("name") or ""),
                    bool(rubric.get("fallback", False)),
                )
            )
        else:
            rubric_info.append(
                (
                    str(getattr(rubric, "id", "")),
                    str(getattr(rubric, "name", "")),
                    bool(getattr(rubric, "fallback", False)),
                )
            )

    labels = {rid: title for rid, title, _ in rubric_info if rid}
    units_by_rubric: dict[str, list[Any]] = {}
    seen_unit_ids: set[str] = set()
    seen_fact_ids: set[str] = set()
    fact_records = {
        str(getattr(record, "fact_id", "")): record
        for record in composition_fact_records
        if getattr(record, "fact_id", "")
    }
    composition_relations = tuple(getattr(composition, "relations", ()) or ())
    required_facts = {
        str(getattr(fact, "fact_id", "")): fact
        for fact in presentation_required_facts
        if getattr(fact, "fact_id", "")
    }
    card_ids = {str(getattr(card, "id", "")) for card in cards}

    for unit in units:
        unit_id = str(getattr(unit, "unit_id", ""))
        rubric_id = str(getattr(unit, "rubric_id", ""))
        unit_facts = tuple(str(fid) for fid in (getattr(unit, "fact_ids", ()) or ()))
        unit_stories = tuple(str(sid) for sid in (getattr(unit, "story_ids", ()) or ()))
        unit_supports = tuple(str(sid) for sid in (getattr(unit, "support_ids", ()) or ()))
        if not unit_id or unit_id in seen_unit_ids:
            raise DigestCoverageInvariantError(f"DIGEST_COMPOSITION_INVALID_UNIT_ID:{unit_id!r}")
        if rubric_id not in labels:
            raise DigestCoverageInvariantError(
                f"DIGEST_COMPOSITION_UNKNOWN_RUBRIC:{unit_id}:{rubric_id}"
            )
        if not unit_facts and not unit_stories:
            raise DigestCoverageInvariantError(f"DIGEST_COMPOSITION_EMPTY_UNIT:{unit_id}")
        if len(unit_facts) != len(set(unit_facts)) or len(unit_stories) != len(set(unit_stories)):
            raise DigestCoverageInvariantError(f"DIGEST_COMPOSITION_DUPLICATE_MEMBERSHIP:{unit_id}")
        if unit_facts and not unit_supports:
            raise DigestCoverageInvariantError(
                f"DIGEST_COMPOSITION_FACTS_WITHOUT_SUPPORTS:{unit_id}"
            )
        if not unit_facts and not set(unit_stories).issubset(card_ids):
            raise DigestCoverageInvariantError(f"DIGEST_COMPOSITION_SUMMARY_CARD_MISSING:{unit_id}")
        record_story_ids: set[str] = set()
        record_support_ids: set[str] = set()
        for fact_id in unit_facts:
            if fact_id in seen_fact_ids:
                raise DigestCoverageInvariantError(f"DIGEST_COMPOSITION_DUPLICATE_FACT:{fact_id}")
            record = fact_records.get(fact_id)
            fact = required_facts.get(fact_id)
            if record is None or fact is None:
                raise DigestCoverageInvariantError(
                    f"DIGEST_COMPOSITION_FACT_RECORD_MISSING:{unit_id}:{fact_id}"
                )
            if str(getattr(record, "rubric_id", "")) != rubric_id:
                raise DigestCoverageInvariantError(
                    f"DIGEST_COMPOSITION_FACT_RUBRIC_MISMATCH:{unit_id}:{fact_id}"
                )
            rec_stories = {str(sid) for sid in getattr(record, "story_ids", ()) or ()}
            rec_supports = {str(sid) for sid in getattr(record, "support_ids", ()) or ()}
            fact_stories = {str(sid) for sid in getattr(fact, "story_ids", ()) or ()}
            if (
                rec_stories != fact_stories
                or not rec_supports
                or not rec_supports.issubset(set(unit_supports))
            ):
                raise DigestCoverageInvariantError(
                    f"DIGEST_COMPOSITION_FACT_PROVENANCE_MISMATCH:{unit_id}:{fact_id}"
                )
            record_story_ids.update(rec_stories)
            record_support_ids.update(rec_supports)
            seen_fact_ids.add(fact_id)
        if unit_facts and (
            record_story_ids != set(unit_stories)
            or not record_support_ids.issubset(set(unit_supports))
        ):
            raise DigestCoverageInvariantError(
                f"DIGEST_COMPOSITION_UNIT_PROVENANCE_MISMATCH:{unit_id}"
            )
        if not unit_facts and not unit_supports:
            raise DigestCoverageInvariantError(
                f"DIGEST_COMPOSITION_SUMMARY_WITHOUT_SUPPORTS:{unit_id}"
            )
        seen_unit_ids.add(unit_id)
        units_by_rubric.setdefault(rubric_id, []).append(unit)

    expected_facts = {str(fid) for fid in getattr(composition, "admitted_fact_ids", ()) or ()}
    if seen_fact_ids != expected_facts:
        raise DigestCoverageInvariantError(
            "DIGEST_COMPOSITION_FACT_PARTITION_MISMATCH: "
            f"missing={sorted(expected_facts - seen_fact_ids)} extra={sorted(seen_fact_ids - expected_facts)}"
        )
    expected_story_ids = {str(sid) for sid in getattr(composition, "admitted_story_ids", ()) or ()}
    unit_story_ids = {str(sid) for unit in units for sid in (getattr(unit, "story_ids", ()) or ())}
    if unit_story_ids != expected_story_ids:
        raise DigestCoverageInvariantError(
            "DIGEST_COMPOSITION_STORY_PARTITION_MISMATCH: "
            f"missing={sorted(expected_story_ids - unit_story_ids)} extra={sorted(unit_story_ids - expected_story_ids)}"
        )

    ordered_rubrics = [rid for rid, _title, _fallback in rubric_info if rid in units_by_rubric]
    blocks: list[DigestNarrativeBlock] = []
    for rubric_id in ordered_rubrics:
        rubric_units = tuple(units_by_rubric[rubric_id])
        block_facts = tuple(
            required_facts[fact_id] for unit in rubric_units for fact_id in unit.fact_ids
        )
        story_ids = tuple(
            dict.fromkeys(str(sid) for unit in rubric_units for sid in unit.story_ids)
        )
        support_ids = tuple(
            dict.fromkeys(str(sid) for unit in rubric_units for sid in unit.support_ids)
        )
        support_by_story: dict[str, list[str]] = {}
        for unit in rubric_units:
            unit_fact_ids = set(unit.fact_ids)
            if unit_fact_ids:
                for fact_id in unit_fact_ids:
                    record = fact_records[fact_id]
                    for story_id in record.story_ids:
                        owned = support_by_story.setdefault(str(story_id), [])
                        for support_id in record.support_ids:
                            if support_id not in owned:
                                owned.append(support_id)
            else:
                for story_id in unit.story_ids:
                    support_by_story[str(story_id)] = list(unit.support_ids)
        unit_records = tuple(
            fact_records[fact_id] for unit in rubric_units for fact_id in unit.fact_ids
        )
        blocks.append(
            DigestNarrativeBlock(
                block_id=f"block:{rubric_id}:composition",
                rubric_id=rubric_id,
                rubric_title=labels[rubric_id],
                story_ids=story_ids,
                support_ids=support_ids,
                canonical_notes=tuple(getattr(fact, "text", "") for fact in block_facts),
                required_facts=block_facts,
                required_story_groups=tuple(tuple(unit.story_ids) for unit in rubric_units),
                support_ids_by_story=tuple(
                    (sid, tuple(ids)) for sid, ids in support_by_story.items()
                ),
                composition_units=rubric_units,
                composition_fact_records=unit_records,
                composition_relations=tuple(
                    relation
                    for relation in composition_relations
                    if relation.left_fact_id in {record.fact_id for record in unit_records}
                    and relation.right_fact_id in {record.fact_id for record in unit_records}
                ),
            )
        )
    if set(units_by_rubric) != set(ordered_rubrics):
        raise DigestCoverageInvariantError("DIGEST_COMPOSITION_RUBRIC_ORDER_INVALID")
    return DigestNarrativePlan(blocks=tuple(blocks), edition_slug=edition_slug)


@dataclass(frozen=True)
class DigestSituationItemDraft:
    """A single rendered operational item within the City Situation section."""

    group_id: str
    label: str
    body: str
    cited_support_ids: tuple[str, ...]
    emoji: str = ""
    claims: tuple[DigestClaimAtom, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestSituationItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("situation item must be a mapping")
        group_id = str(raw.get("group_id", "")).strip()
        label = str(raw.get("label", "")).strip()
        body = str(raw.get("body", "")).strip()
        emoji = str(raw.get("emoji", "")).strip()
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        if not group_id or not label or not body or not support_ids:
            raise ValueError("situation item requires group_id, label, body and cited_support_ids")
        raw_claims = raw.get("claims", [])
        if raw_claims is None:
            raw_claims = []
        if not isinstance(raw_claims, list):
            raise ValueError("claims must be a list")
        claims_list = [DigestClaimAtom.from_dict(c) for c in raw_claims]
        return cls(
            group_id=group_id,
            label=label,
            body=body,
            cited_support_ids=support_ids,
            emoji=emoji,
            claims=tuple(claims_list),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "label": self.label,
            "body": self.body,
            "cited_support_ids": list(self.cited_support_ids),
            "emoji": self.emoji,
            "claims": [c.to_dict() for c in self.claims],
        }


@dataclass(frozen=True)
class DigestClaimAtom:
    """A single supported claim atom within a digest editorial item."""

    text: str
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()
    covered_fact_ids: tuple[str, ...] = ()
    summary_unit_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestClaimAtom:
        if not isinstance(raw, Mapping):
            raise ValueError("claim atom must be a mapping")
        text = str(raw.get("text", "")).strip()
        raw_stories = raw.get("covered_story_ids", [])
        if isinstance(raw_stories, (str, int)):
            raw_stories = [raw_stories]
        if not isinstance(raw_stories, list):
            raise ValueError("covered_story_ids must be a list")
        story_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_stories
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_facts = raw.get("covered_fact_ids", [])
        if isinstance(raw_facts, (str, int)):
            raw_facts = [raw_facts]
        if not isinstance(raw_facts, list):
            raise ValueError("covered_fact_ids must be a list")
        fact_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_facts
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        return cls(
            text=text,
            covered_story_ids=story_ids,
            cited_support_ids=support_ids,
            covered_fact_ids=fact_ids,
            summary_unit_ids=tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in _clean_str_list(raw.get("summary_unit_ids", []))
                    if str(value).strip()
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "text": self.text,
            "covered_story_ids": list(self.covered_story_ids),
            "cited_support_ids": list(self.cited_support_ids),
        }
        if self.covered_fact_ids:
            d["covered_fact_ids"] = list(self.covered_fact_ids)
        if self.summary_unit_ids:
            d["summary_unit_ids"] = list(self.summary_unit_ids)
        return d


@dataclass(frozen=True)
class DigestEditorialItemDraft:
    """A single scan-first editorial item within a narrative digest block."""

    headline: str = ""
    body: str = ""
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()
    claims: tuple[DigestClaimAtom, ...] = ()
    emoji: str = ""
    # Canonical composition-path identity and membership. Legacy drafts leave
    # these empty; composition drafts receive them from the frozen plan.
    item_id: str = ""
    composition_unit_id: str = ""
    covered_fact_ids: tuple[str, ...] = ()
    composition_unit_ids: tuple[str, ...] = ()
    source_item_ids: tuple[str, ...] = ()
    source_item_fact_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    composition_merge_id: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestEditorialItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("digest item must be a mapping")
        headline = str(raw.get("headline", "")).strip()
        body = str(raw.get("body", "")).strip()
        emoji = str(raw.get("emoji", "")).strip()
        raw_stories = raw.get("covered_story_ids", [])
        if isinstance(raw_stories, (str, int)):
            raw_stories = [raw_stories]
        if not isinstance(raw_stories, list):
            raise ValueError("covered_story_ids must be a list")
        story_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_stories
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        if not story_ids:
            # Fall back to single story_id if provided
            fallback_sid = str(raw.get("story_id", "")).strip()
            if fallback_sid:
                story_ids = (fallback_sid,)
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_claims = raw.get("claims", [])
        claims: list[DigestClaimAtom] = []
        if isinstance(raw_claims, list):
            for rc in raw_claims:
                if isinstance(rc, Mapping):
                    claims.append(DigestClaimAtom.from_dict(rc))

        # Union claim-level support IDs into item support_ids
        for c in claims:
            for s in c.cited_support_ids:
                if s and s not in support_ids:
                    support_ids = (*support_ids, s)

        # Sanitize headline to replace causal connectors with neutral phrasing
        clean_headline = (
            re.sub(r"\bиз-за\b", "при", headline, flags=re.IGNORECASE) if headline else ""
        )
        if clean_headline:
            clean_headline = re.sub(r'[«»"“„]', "", clean_headline)
            clean_headline = re.sub(
                r"^(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+горожан)[\s,:]*",
                "",
                clean_headline,
                flags=re.IGNORECASE,
            ).strip()
            if clean_headline:
                clean_headline = clean_headline[:1].upper() + clean_headline[1:]

        # Sanitize body: remove conversational assumption markers
        clean_body = body
        if clean_body:
            clean_body = re.sub(r"[«\"]по свету ноль[»\"]", "по свету ноль", clean_body)
            clean_body = re.sub(r"\s+вместо\s+220(?:\s*[вВвольт]+)?", "", clean_body)
            clean_body = re.sub(
                r"\bиз-за\s+(?:этого|чего|которых)\b", "при этом", clean_body, flags=re.IGNORECASE
            )
            clean_body = re.sub(r"\bиз-за\b", "при", clean_body, flags=re.IGNORECASE)
            clean_body = re.sub(r"«([^»]+)»", r"\1", clean_body)
            clean_body = re.sub(r'"([^"]+)"', r"\1", clean_body)
            if len(clean_body) > 1150:
                clean_body = clean_body[:1150].rsplit(" ", 1)[0].rstrip(".,;: ") + "."

            # Strip redundant headline repetition at start of body
            if clean_headline:
                clean_body = _strip_redundant_headline_from_body(clean_headline, clean_body)

            # Remove duplicate attribution in body if present multiple times
            att_matches = list(
                re.finditer(
                    r"\b(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+горожан)[\s,:]*",
                    clean_body,
                    flags=re.IGNORECASE,
                )
            )
            if len(att_matches) > 1:
                for m in reversed(att_matches[1:]):
                    clean_body = clean_body[: m.start()] + clean_body[m.end() :]
                clean_body = re.sub(
                    r"\.\s+([a-zа-я])", lambda x: ". " + x.group(1).upper(), clean_body
                )

            # Remove chat metadata and emoji spam
            clean_body = re.sub(
                r"(?:публикуют\s+)?сообщения\s+с\s+эмодзи[\w\s,]*[.]?",
                "",
                clean_body,
                flags=re.IGNORECASE,
            ).strip()
            clean_body = re.sub(
                r"\bсмайлик(?:ами|и)?\b", "", clean_body, flags=re.IGNORECASE
            ).strip()
            clean_body = re.sub(
                r"\b(?:в\s+местных\s+чатах|в\s+чате(?:\s+[А-Яа-я]+)?|в\s+местном\s+чате|в\s+городском\s+чате)\b",
                "в городе",
                clean_body,
                flags=re.IGNORECASE,
            )
            clean_body = re.sub(r"\s{2,}", " ", clean_body).strip()

        if clean_headline and clean_body:
            clean_headline, clean_body = _fix_redundant_headline_and_body(
                clean_headline, clean_body
            )

        clean_claims: list[DigestClaimAtom] = []
        for c in claims:
            c_text = re.sub(r"[«\"]по свету ноль[»\"]", "по свету ноль", c.text)
            c_text = re.sub(r"\s+вместо\s+220(?:\s*[вВвольт]+)?", "", c_text)
            clean_claims.append(replace(c, text=c_text))

        raw_item_facts = raw.get("covered_fact_ids", [])
        if isinstance(raw_item_facts, (str, int)):
            raw_item_facts = [raw_item_facts]
        if not isinstance(raw_item_facts, list):
            raise ValueError("covered_fact_ids must be a list")
        item_fact_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in raw_item_facts
                if value and isinstance(value, (str, int)) and str(value).strip()
            )
        )
        raw_composition_units = raw.get("composition_unit_ids", [])
        if isinstance(raw_composition_units, (str, int)):
            raw_composition_units = [raw_composition_units]
        if not isinstance(raw_composition_units, list):
            raise ValueError("composition_unit_ids must be a list")
        composition_unit_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in raw_composition_units
                if value and isinstance(value, (str, int)) and str(value).strip()
            )
        )
        singular_unit_id = str(raw.get("composition_unit_id", "")).strip()
        if singular_unit_id and not composition_unit_ids:
            composition_unit_ids = (singular_unit_id,)
        elif singular_unit_id and composition_unit_ids != (singular_unit_id,):
            raise ValueError("composition_unit_id conflicts with composition_unit_ids")
        raw_source_ids = raw.get("source_item_ids", [])
        if isinstance(raw_source_ids, (str, int)):
            raw_source_ids = [raw_source_ids]
        if not isinstance(raw_source_ids, list):
            raise ValueError("source_item_ids must be a list")
        source_item_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in raw_source_ids
                if value and isinstance(value, (str, int)) and str(value).strip()
            )
        )
        source_item_fact_ids: list[tuple[str, tuple[str, ...]]] = []
        raw_source_facts = raw.get("source_item_fact_ids", [])
        if not isinstance(raw_source_facts, list):
            raise ValueError("source_item_fact_ids must be a list")
        for source_fact in raw_source_facts:
            if not isinstance(source_fact, Mapping):
                raise ValueError("source_item_fact_ids entries must be objects")
            source_id = str(source_fact.get("item_id", "")).strip()
            source_facts = tuple(_clean_str_list(source_fact.get("covered_fact_ids", [])))
            if source_id:
                source_item_fact_ids.append((source_id, source_facts))
        if not clean_body or not story_ids or not support_ids:
            raise ValueError("digest editorial item requires body, stories and supports")
        return cls(
            headline=clean_headline,
            body=clean_body,
            covered_story_ids=story_ids,
            cited_support_ids=support_ids,
            claims=tuple(clean_claims),
            emoji=emoji,
            item_id=str(raw.get("item_id", "")).strip(),
            composition_unit_id=(composition_unit_ids[0] if len(composition_unit_ids) == 1 else ""),
            covered_fact_ids=item_fact_ids,
            composition_unit_ids=composition_unit_ids,
            source_item_ids=source_item_ids,
            source_item_fact_ids=tuple(source_item_fact_ids),
            composition_merge_id=str(raw.get("composition_merge_id", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "item_id": self.item_id,
            "composition_unit_ids": list(
                self.composition_unit_ids
                or ((self.composition_unit_id,) if self.composition_unit_id else ())
            ),
            "headline": self.headline,
            "body": self.body,
            "covered_story_ids": list(self.covered_story_ids),
            "cited_support_ids": list(self.cited_support_ids),
            "claims": [c.to_dict() for c in self.claims],
            "emoji": self.emoji,
        }
        if self.covered_fact_ids:
            result["covered_fact_ids"] = list(self.covered_fact_ids)
        if self.source_item_ids:
            result["source_item_ids"] = list(self.source_item_ids)
            result["source_item_fact_ids"] = [
                {"item_id": item_id, "covered_fact_ids": list(fact_ids)}
                for item_id, fact_ids in self.source_item_fact_ids
            ]
            result["composition_merge_id"] = self.composition_merge_id
        return result


@dataclass(frozen=True)
class DigestNarrativeBlockDraft:
    """A single rendered block in a narrative digest draft."""

    block_id: str
    items: tuple[DigestEditorialItemDraft, ...]


@dataclass(frozen=True)
class DigestNarrativeDraft:
    """Complete output draft from the single-call narrative digest writer."""

    blocks: tuple[DigestNarrativeBlockDraft, ...]
    situation_items: tuple[Any, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> DigestNarrativeDraft:
        """Parse structured narrative digest draft with strict structural validation."""
        if not isinstance(data, Mapping):
            raise ValueError("root must be a mapping")

        # Legacy situation_items are silently accepted but not stored — backward compat.

        raw_blocks = data.get("blocks")
        if raw_blocks is None:
            raise ValueError("missing 'blocks' list")
        if not isinstance(raw_blocks, list):
            raise ValueError("'blocks' must be a list")

        seen_block_ids: set[str] = set()
        block_drafts: list[DigestNarrativeBlockDraft] = []

        for b in raw_blocks:
            if not isinstance(b, Mapping):
                raise ValueError("block item must be a mapping")

            block_id = str(b.get("block_id") or "").strip()
            if not block_id:
                raise ValueError("missing or empty 'block_id'")
            if block_id in seen_block_ids:
                raise ValueError(f"duplicate block_id: {block_id}")
            seen_block_ids.add(block_id)

            raw_items = b.get("items")
            if raw_items is None or not isinstance(raw_items, list) or len(raw_items) == 0:
                raise ValueError(f"block {block_id} must contain at least one item")

            item_drafts: list[DigestEditorialItemDraft] = []
            for item_raw in raw_items:
                item_drafts.append(DigestEditorialItemDraft.from_dict(item_raw))

            block_drafts.append(
                DigestNarrativeBlockDraft(
                    block_id=block_id,
                    items=tuple(item_drafts),
                )
            )

        return cls(blocks=tuple(block_drafts), situation_items=())


def plan_digest_narrative_blocks(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[Any],
    max_cards_per_block: int = 6,
    presentation_plan: Any = None,
    use_topic_bundles: bool | None = None,
    edition_slug: str = "",
) -> DigestNarrativePlan:
    """Build immutable narrative blocks from classified story cards strictly preserving order."""
    if (
        presentation_plan is not None
        and getattr(presentation_plan, "composition", None) is not None
    ):
        return _composition_narrative_plan(
            cards=cards,
            rubrics=rubrics,
            presentation_plan=presentation_plan,
            edition_slug=edition_slug,
        )
    if not cards:
        return DigestNarrativePlan(blocks=())

    def _get_r_info(r: Any) -> tuple[str, str, bool]:
        if isinstance(r, Mapping):
            return (
                str(r.get("id", "")),
                str(r.get("title") or r.get("name") or ""),
                bool(r.get("fallback", False)),
            )
        return (
            str(getattr(r, "id", "")),
            str(getattr(r, "name", "")),
            bool(getattr(r, "fallback", False)),
        )

    presentations_by_id = {}
    raw_presentations: tuple[Any, ...] = ()
    if presentation_plan is not None:
        raw_presentations = tuple(
            getattr(presentation_plan, "story_presentations", ())
            or getattr(presentation_plan, "story_hints", ())
            or ()
        )
        # DigestPresentationPlan creates legacy per-story descriptors when callers
        # provide only story_ids. They are compatibility defaults, not editorial
        # merge decisions. Treat them as absent so compression units can synthesize
        # related Stories in deterministic/fallback mode.
        has_explicit_presentation_metadata = any(
            bool(getattr(p, "detail_support_ids", ()))
            or bool(getattr(p, "merge_group_id", ""))
            or getattr(p, "mode", "DETAIL_ONLY") != "DETAIL_ONLY"
            or bool(getattr(p, "city_situation_group_ids", ()))
            for p in raw_presentations
        )
        if has_explicit_presentation_metadata:
            presentations_by_id = {
                p.story_id: p for p in raw_presentations if getattr(p, "story_id", None)
            }

    dashboard_supports_by_story_map: dict[str, set[str]] = {}

    if presentation_plan is not None and getattr(presentation_plan, "city_situation", None):
        groups = getattr(presentation_plan.city_situation, "groups", ()) or ()
        for g in groups:
            for sid in getattr(g, "covered_story_ids", ()):
                dashboard_supports_by_story_map.setdefault(sid, set()).update(
                    getattr(g, "cited_support_ids", ())
                )

    rubric_infos = [_get_r_info(r) for r in rubrics]
    rubric_ids = [info[0] for info in rubric_infos if info[0]]
    fallback_info = next(
        (info for info in rubric_infos if info[2]),
        rubric_infos[0] if rubric_infos else ("other", "Другое", True),
    )
    fallback_id = fallback_info[0] if fallback_info[0] else "other"

    # Group cards by rubric, preserving rubric sequence
    cards_by_rubric: dict[str, list[StoryCard]] = {rid: [] for rid in rubric_ids}
    for card in cards:
        c_full = f"{card.topic} {card.summary}".casefold()
        rid = card.rubric_id if card.rubric_id in cards_by_rubric else fallback_id
        if "экватор" in c_full and "safety" in cards_by_rubric:
            rid = "safety"
        elif rid in ("other", fallback_id):
            from src.publication.digest_presentation import _canonical_topic_family

            t_key, _, _ = _canonical_topic_family(card, rid)
            if (
                t_key in ("electricity", "water", "gas", "heating")
                and "infrastructure" in cards_by_rubric
            ):
                rid = "infrastructure"
            elif t_key in ("connectivity",) and "communications" in cards_by_rubric:
                rid = "communications"
            elif t_key in ("banking", "civic_services") and "civic_services" in cards_by_rubric:
                rid = "civic_services"
            elif t_key in ("transport",) and "mobility" in cards_by_rubric:
                rid = "mobility"
            elif t_key in ("health",) and "health" in cards_by_rubric:
                rid = "health"
            elif t_key in ("social",) and "society" in cards_by_rubric:
                rid = "society"
        if rid not in cards_by_rubric:
            cards_by_rubric[rid] = []
        cards_by_rubric[rid].append(card)

    blocks: list[DigestNarrativeBlock] = []

    assigned_fact_ids: set[str] = set()
    has_explicit_merge_groups = any(
        bool(getattr(p, "merge_group_id", ""))
        and getattr(p, "merge_group_id", "") != getattr(p, "story_id", "")
        for p in raw_presentations
    )
    use_bundles = (
        use_topic_bundles
        if use_topic_bundles is not None
        else (
            (presentation_plan is not None or max_cards_per_block >= 20)
            and not (has_explicit_merge_groups and max_cards_per_block < 20)
        )
    )

    for rid, rname, _ in rubric_infos:
        if not rid:
            continue
        rubric_cards = cards_by_rubric.get(rid, [])
        if not rubric_cards:
            continue

        card_by_id = {c.id: c for c in rubric_cards}

        if use_bundles:
            from src.publication.digest_presentation import build_thematic_topic_bundles

            story_ids = tuple(c.id for c in rubric_cards)
            tentative_req_facts: list[RequiredDigestFact] = []
            if presentation_plan is not None and getattr(presentation_plan, "required_facts", None):
                story_id_set = set(story_ids)
                for rf in presentation_plan.required_facts:
                    if rf.fact_id in assigned_fact_ids:
                        continue
                    if bool(set(rf.story_ids) & story_id_set):
                        tentative_req_facts.append(rf)

            thematic_bundles = build_thematic_topic_bundles(
                rubric_cards,
                evidence=evidence,
                required_facts=tuple(tentative_req_facts),
                rubric_id=rid,
                edition_slug=edition_slug,
            )
            if thematic_bundles:
                for rf in tentative_req_facts:
                    assigned_fact_ids.add(rf.fact_id)
                block_req_facts = tentative_req_facts

                block_id = f"block:{rid}:0"
                req_story_groups = tuple(b.story_ids for b in thematic_bundles)

                block_support_ids: list[str] = []
                story_support_ids_map: list[tuple[str, tuple[str, ...]]] = []

                for tb in thematic_bundles:
                    for s in tb.support_ids:
                        if s not in block_support_ids:
                            block_support_ids.append(s)
                    for sid in tb.story_ids:
                        sups = list(tb.support_ids)
                        if sid in presentations_by_id:
                            for det_s in presentations_by_id[sid].detail_support_ids:
                                if det_s not in sups:
                                    sups.insert(0, det_s)
                                if det_s not in block_support_ids:
                                    block_support_ids.append(det_s)
                        if sid not in sups:
                            sups.append(sid)
                        if sid not in block_support_ids:
                            block_support_ids.append(sid)
                        c_card = card_by_id.get(sid)
                        if c_card:
                            if c_card.id not in sups:
                                sups.append(c_card.id)
                            if c_card.id not in block_support_ids:
                                block_support_ids.append(c_card.id)
                            c_summary_ref = f"{c_card.id}:summary"
                            if c_summary_ref not in sups:
                                sups.append(c_summary_ref)
                            if c_summary_ref not in block_support_ids:
                                block_support_ids.append(c_summary_ref)
                            c_topic_ref = f"{c_card.id}:topic"
                            if c_topic_ref not in sups:
                                sups.append(c_topic_ref)
                            if c_topic_ref not in block_support_ids:
                                block_support_ids.append(c_topic_ref)
                        story_support_ids_map.append((sid, tuple(sups)))

                notes: list[str] = []
                for tb in thematic_bundles:
                    for fact in tb.fact_ledger:
                        if fact not in notes:
                            notes.append(fact)
                if not notes:
                    for c in rubric_cards:
                        if c.summary and c.summary not in notes:
                            notes.append(c.summary)
                        elif c.topic and c.topic not in notes:
                            notes.append(c.topic)

                detail_supports = tuple(
                    (c.id, presentations_by_id[c.id].detail_support_ids)
                    for c in rubric_cards
                    if c.id in presentations_by_id and presentations_by_id[c.id].detail_support_ids
                )
                merge_groups = tuple(
                    (sid, tb.bundle_id) for tb in thematic_bundles for sid in tb.story_ids
                )
                detail_roles = tuple(
                    (c.id, getattr(presentations_by_id[c.id], "detail_role", "NORMAL"))
                    for c in rubric_cards
                    if c.id in presentations_by_id
                )
                pres_modes = tuple(
                    (c.id, getattr(presentations_by_id[c.id], "mode", "DETAIL_ONLY"))
                    for c in rubric_cards
                    if c.id in presentations_by_id
                )
                dash_supports = tuple(
                    (c.id, tuple(dashboard_supports_by_story_map.get(c.id, ())))
                    for c in rubric_cards
                    if c.id in dashboard_supports_by_story_map
                )

                blocks.append(
                    DigestNarrativeBlock(
                        block_id=block_id,
                        rubric_id=rid,
                        rubric_title=rname,
                        story_ids=story_ids,
                        support_ids=tuple(block_support_ids),
                        canonical_notes=tuple(notes),
                        required_facts=tuple(block_req_facts),
                        detail_support_ids_by_story=detail_supports,
                        merge_group_by_story=merge_groups,
                        detail_roles_by_story=detail_roles,
                        presentation_modes_by_story=pres_modes,
                        dashboard_support_ids_by_story=dash_supports,
                        required_story_groups=req_story_groups,
                        support_ids_by_story=tuple(story_support_ids_map),
                        topic_bundles=thematic_bundles,
                    )
                )
                continue

        # Partition cards using deterministic presentation compression units
        from src.publication.digest_presentation import build_digest_presentation_units

        units = build_digest_presentation_units(
            rubric_cards,
            presentation_plan=presentation_plan,
            max_synthesis_size=24,
            max_normal_size=8,
            max_brief_size=6,
            edition_slug=edition_slug,
        )
        card_by_id = {c.id: c for c in rubric_cards}

        # Pack units into blocks
        current_block_units: list[Any] = []
        current_card_count = 0
        block_units_list: list[list[Any]] = []

        for unit in units:
            unit_len = len(unit.story_ids)
            bound = max(max_cards_per_block, unit_len)
            if current_block_units and (current_card_count + unit_len > bound):
                block_units_list.append(current_block_units)
                current_block_units = [unit]
                current_card_count = unit_len
            else:
                current_block_units.append(unit)
                current_card_count += unit_len

        if current_block_units:
            block_units_list.append(current_block_units)

        for chunk_idx, block_units in enumerate(block_units_list):
            chunk = [card_by_id[sid] for u in block_units for sid in u.story_ids]
            block_id = f"block:{rid}:{chunk_idx}"
            story_ids = tuple(c.id for c in chunk)

            # If presentation_plan provides explicit merge_group_ids, build
            # required_story_groups from them (preserving first-seen order).
            # Otherwise fall back to unit-level grouping.
            if presentations_by_id:
                mg_seen: dict[str, list[str]] = {}
                for c in chunk:
                    mgid = getattr(presentations_by_id.get(c.id), "merge_group_id", None) or c.id
                    mg_seen.setdefault(mgid, []).append(c.id)
                req_story_groups = tuple(tuple(v) for v in mg_seen.values())
            else:
                req_story_groups = tuple(tuple(u.story_ids) for u in block_units)

            # Collect canonical notes from cards and track support ownership per story
            notes = []
            block_support_ids = []
            story_support_ids_map = []

            for c in chunk:
                story_sups: list[str] = []
                if c.summary:
                    notes.append(f"{c.topic}: {c.summary}")
                    if f"{c.id}:summary" not in story_sups:
                        story_sups.append(f"{c.id}:summary")
                elif c.topic:
                    notes.append(c.topic)
                for hf in c.hard_facts:
                    if hf.text and hf.text not in notes:
                        notes.append(hf.text)
                    for r in hf.source_refs:
                        if r not in story_sups:
                            story_sups.append(r)
                for co in c.community_observations:
                    if co.text and co.text not in notes:
                        notes.append(co.text)
                    for r in co.source_refs:
                        if r not in story_sups:
                            story_sups.append(r)
                for r in getattr(c, "representative_source_refs", ()):
                    if r not in story_sups:
                        story_sups.append(r)
                if c.id not in story_sups:
                    story_sups.append(c.id)

                if c.id in presentations_by_id:
                    for supp_id in presentations_by_id[c.id].detail_support_ids:
                        if supp_id not in story_sups:
                            story_sups.append(supp_id)

                # Extract numeric story ID if story:123
                num_sid: int | None = None
                if c.id.startswith("story:"):
                    raw_sid = c.id.split(":", 1)[1]
                    if raw_sid.isdigit():
                        num_sid = int(raw_sid)

                for eid, evi in evidence.items():
                    evi_sid = getattr(evi, "story_id", None)
                    if (
                        evi_sid == c.id
                        or str(evi_sid) == str(c.id)
                        or (num_sid is not None and evi_sid == num_sid)
                        or eid.startswith(f"{c.id}:")
                    ):
                        if (
                            getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                            and eid not in story_sups
                        ):
                            story_sups.append(eid)

                for sup in story_sups:
                    if sup not in block_support_ids:
                        block_support_ids.append(sup)
                story_support_ids_map.append((c.id, tuple(story_sups)))

            detail_supports = tuple(
                (c.id, presentations_by_id[c.id].detail_support_ids)
                for c in chunk
                if c.id in presentations_by_id and presentations_by_id[c.id].detail_support_ids
            )
            merge_groups = tuple(
                (c.id, presentations_by_id[c.id].merge_group_id)
                for c in chunk
                if c.id in presentations_by_id
            )
            detail_roles = tuple(
                (c.id, getattr(presentations_by_id[c.id], "detail_role", "NORMAL"))
                for c in chunk
                if c.id in presentations_by_id
            )
            pres_modes = tuple(
                (c.id, getattr(presentations_by_id[c.id], "mode", "DETAIL_ONLY"))
                for c in chunk
                if c.id in presentations_by_id
            )
            dash_supports = tuple(
                (c.id, tuple(dashboard_supports_by_story_map.get(c.id, ())))
                for c in chunk
                if c.id in dashboard_supports_by_story_map
            )

            # Assign required facts matching this block
            block_req_facts = []
            if presentation_plan is not None and getattr(presentation_plan, "required_facts", None):
                story_id_set = set(story_ids)
                for rf in presentation_plan.required_facts:
                    if rf.fact_id in assigned_fact_ids:
                        continue
                    if bool(set(rf.story_ids) & story_id_set):
                        block_req_facts.append(rf)
                        assigned_fact_ids.add(rf.fact_id)

            blocks.append(
                DigestNarrativeBlock(
                    block_id=block_id,
                    rubric_id=rid,
                    rubric_title=rname,
                    story_ids=story_ids,
                    support_ids=tuple(block_support_ids),
                    canonical_notes=tuple(notes),
                    required_facts=tuple(block_req_facts),
                    detail_support_ids_by_story=detail_supports,
                    merge_group_by_story=merge_groups,
                    detail_roles_by_story=detail_roles,
                    presentation_modes_by_story=pres_modes,
                    dashboard_support_ids_by_story=dash_supports,
                    required_story_groups=req_story_groups,
                    support_ids_by_story=tuple(story_support_ids_map),
                )
            )

    expected_fact_ids = (
        {rf.fact_id for rf in getattr(presentation_plan, "required_facts", ())}
        if presentation_plan is not None
        else set()
    )
    unassigned = expected_fact_ids - assigned_fact_ids
    if unassigned and presentation_plan is not None and blocks:
        for rf in presentation_plan.required_facts:
            if rf.fact_id in unassigned:
                target_block = next((b for b in blocks if b.rubric_id == rf.rubric_id), None)
                if target_block is None:
                    target_block = next(
                        (b for b in blocks if bool(set(rf.story_ids) & set(b.story_ids))), None
                    )
                if target_block is None:
                    target_block = blocks[0]
                idx = blocks.index(target_block)
                blocks[idx] = replace(
                    target_block,
                    required_facts=(*target_block.required_facts, rf),
                    support_ids=tuple(dict.fromkeys((*target_block.support_ids, *rf.support_ids))),
                )
                assigned_fact_ids.add(rf.fact_id)

    if assigned_fact_ids != expected_fact_ids:
        raise DigestCoverageInvariantError(
            f"UNASSIGNED_REQUIRED_FACTS: {expected_fact_ids - assigned_fact_ids}"
        )

    return DigestNarrativePlan(blocks=tuple(blocks))


DIGEST_ITEM_HEADLINE_MAX_CHARS = 140
DIGEST_ITEM_BODY_MAX_CHARS = 1200
DIGEST_SITUATION_BODY_MAX_CHARS = 360

_RECOMMENDATION_SENTENCE_PATTERN = re.compile(
    r"(?:^|\s+)(?:"
    r"(?:Стоит|Следует|Рекомендуется|Необходимо|Лучше|Нужно)\s+(?:заранее\s+)?(?:позаботиться|запастись|сделать\s+запас\w*|подготовить|подзарядить|иметь\s+в\s+виду|оставаться|воздержаться|не\s+выходить)[^.!?\n]*[.!?]"
    r"|Жителям\s+советуют\s+[^.!?\n]*[.!?]"
    r"|Постарайтесь\s+[^.!?\n]*[.!?]"
    r"|Запаситесь\s+[^.!?\n]*[.!?]"
    r"|Зарядите\s+[^.!?\n]*[.!?]"
    r"|Не\s+выходите\s+[^.!?\n]*[.!?]"
    r"|Воздержитесь\s+[^.!?\n]*[.!?]"
    r")",
    re.IGNORECASE,
)

_RECOMMENDATION_MODALITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:рекоменду(?:ется|ем|ют)?|совету(?:ют|ем)?|следует|необходимо|нужно|постарайтесь|запаситесь|зарядите|не\s+выходите|воздержи(?:тесь|тесь)?|просят\s+(?:жителей|горожан|не)|памятка|инструкция)\b",
        re.IGNORECASE,
    ),
)


_ADVICE_GENERIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "рекомендуется",
        "рекомендуем",
        "рекомендуют",
        "советуют",
        "советуем",
        "следует",
        "необходимо",
        "нужно",
        "постарайтесь",
        "пожалуйста",
        "заранее",
        "жителям",
        "горожанам",
        "гражданам",
        "людям",
        "населению",
        "сделать",
        "подготовить",
        "иметь",
        "быть",
        "также",
        "чтобы",
        "внимание",
        "памятка",
        "инструкция",
        "просьба",
        "просим",
        "время",
        "период",
        "случае",
        "момент",
        "сообщают",
        "передают",
    }
)


def _extract_advice_content_stems(sentence_text: str) -> set[str]:
    """Extract non-stopword content stems representing the subject/action of an advice sentence."""
    clean = sentence_text
    for pat in _RECOMMENDATION_MODALITY_PATTERNS:
        clean = pat.sub(" ", clean)
    words = [w for w in re.split(r"\W+", clean.lower()) if len(w) >= 3]
    stems: set[str] = set()
    for w in words:
        if w in _ADVICE_GENERIC_STOPWORDS:
            continue
        stem = re.sub(
            r"(?:овать|ывать|ивать|ение|ения|ению|нием|ами|ями|ов|ев|ей|ом|ем|ам|ям|ах|ях|ую|юю|ое|ее|ые|ие|ый|ий|ой|ая|яя|ть|ти|ся|сь)$",
            "",
            w,
        )
        if len(stem) >= 3 and stem not in _ADVICE_GENERIC_STOPWORDS:
            stems.add(stem)
    return stems


_CLAUSE_SPLIT_RE = re.compile(r"[,;]|\s+(?:а\s+также|также|и)\s+", re.IGNORECASE)


def _split_advice_actions(sentence_text: str) -> list[str]:
    """Split a recommendation sentence into discrete action clauses."""
    # Strip leading modality phrases to isolate actions
    clean = sentence_text
    for pat in _RECOMMENDATION_MODALITY_PATTERNS:
        clean = pat.sub(" ", clean)
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(clean) if c.strip()]
    return clauses if clauses else [sentence_text]


def find_unsupported_digest_recommendations(
    text: str,
    cited_supports: Sequence[str] | str,
) -> list[str]:
    """Find reader advice / calls-to-action that are not grounded in cited supports.

    Adversarial trust-boundary hardening:
    Operates at the action/clause level. If a recommendation bundles multiple actions
    (e.g., 'запастись водой и не выходить на улицу'), EACH distinct action clause
    with substantive content stems (>= 2 content stems) must be grounded in a cited support
    that possesses recommendation modality.
    """
    if not text or not cited_supports:
        return []
    if isinstance(cited_supports, str):
        support_list = [cited_supports]
    else:
        support_list = list(cited_supports)
    support_lowers = [s.lower() for s in support_list if s]

    violations: list[str] = []
    for match in _RECOMMENDATION_SENTENCE_PATTERN.finditer(text):
        matched_text = match.group(0).strip()
        action_clauses = _split_advice_actions(matched_text)

        # Check that EVERY substantive action clause has at least one supporting source
        sentence_has_unsupported_action = False
        for clause in action_clauses:
            clause_stems = _extract_advice_content_stems(clause)
            if len(clause_stems) < 2:
                # Small connective or empty clause; check if full sentence has stems
                clause_stems = _extract_advice_content_stems(matched_text)
            if not clause_stems:
                continue

            clause_supported = False
            for s_lower in support_lowers:
                has_modality = any(pat.search(s_lower) for pat in _RECOMMENDATION_MODALITY_PATTERNS)
                if not has_modality:
                    continue
                support_stems = _extract_advice_content_stems(s_lower)
                if len(clause_stems & support_stems) >= min(2, len(clause_stems)):
                    clause_supported = True
                    break

            if not clause_supported:
                sentence_has_unsupported_action = True
                break

        if sentence_has_unsupported_action:
            violations.append(matched_text)
    return violations


def strip_unsupported_recommendations(text: str, source_content: str) -> str:
    """Strip fabricated reader advice/calls-to-action unless explicitly supported by source content."""
    support_blocks = [
        b.lower().strip() for b in re.split(r"\n\s*\n|\n(?=[•\-\*])", source_content) if b.strip()
    ]
    if not support_blocks:
        support_blocks = [source_content.lower()]

    def _replace_if_unsupported(match: re.Match[str]) -> str:
        matched_text = match.group(0).strip()
        action_clauses = _split_advice_actions(matched_text)

        for clause in action_clauses:
            clause_stems = _extract_advice_content_stems(clause)
            if len(clause_stems) < 2:
                clause_stems = _extract_advice_content_stems(matched_text)
            if not clause_stems:
                continue

            clause_supported = False
            for b_lower in support_blocks:
                has_mod = any(pat.search(b_lower) for pat in _RECOMMENDATION_MODALITY_PATTERNS)
                if not has_mod:
                    continue
                b_stems = _extract_advice_content_stems(b_lower)
                if len(clause_stems & b_stems) >= min(2, len(clause_stems)):
                    clause_supported = True
                    break
            if not clause_supported:
                return ""
        return match.group(0)

    cleaned = _RECOMMENDATION_SENTENCE_PATTERN.sub(_replace_if_unsupported, text)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _validate_composition_membership(
    draft_block: DigestNarrativeBlockDraft,
    plan_block: DigestNarrativeBlock,
) -> list[str]:
    """Validate exact frozen unit/fact/story/support membership for one block."""
    errors: list[str] = []
    units = {str(unit.unit_id): unit for unit in plan_block.composition_units}
    records = {str(record.fact_id): record for record in plan_block.composition_fact_records}
    fact_to_unit = {
        str(fact_id): str(unit.unit_id)
        for unit in plan_block.composition_units
        for fact_id in unit.fact_ids
    }
    observed_facts: list[str] = []
    summary_counts: dict[str, int] = dict.fromkeys(units, 0)
    observed_story_ids: set[str] = set()
    item_ids: set[str] = set()
    merge_relations = {
        _same_fact_merge_id(relation.left_fact_id, relation.right_fact_id): relation
        for relation in plan_block.composition_relations
        if str(getattr(relation.kind, "value", relation.kind)) == "SAME_FACT"
    }

    for item in draft_block.items:
        prefix = f"COMPOSITION_ITEM:{plan_block.block_id}:{item.item_id or '<missing>'}"
        if not item.item_id or item.item_id in item_ids:
            errors.append(f"{prefix}:ITEM_ID_MISSING_OR_DUPLICATE")
        item_ids.add(item.item_id)
        unit_ids = tuple(
            item.composition_unit_ids
            or ((item.composition_unit_id,) if item.composition_unit_id else ())
        )
        if not unit_ids or len(unit_ids) != len(set(unit_ids)):
            errors.append(f"{prefix}:UNIT_IDS_MISSING_OR_DUPLICATE")
            continue
        if any(unit_id not in units for unit_id in unit_ids):
            errors.append(f"{prefix}:UNKNOWN_UNIT_ID")
            continue
        if any(units[unit_id].rubric_id != plan_block.rubric_id for unit_id in unit_ids):
            errors.append(f"{prefix}:CROSS_RUBRIC_ITEM")
        fact_ids = tuple(item.covered_fact_ids)
        if len(fact_ids) != len(set(fact_ids)) or any(fid not in fact_to_unit for fid in fact_ids):
            errors.append(f"{prefix}:UNKNOWN_OR_DUPLICATE_FACT_ID")
            continue
        fact_unit_ids = {fact_to_unit[fid] for fid in fact_ids}
        summary_unit_ids = {uid for uid in unit_ids if not units[uid].fact_ids}
        if set(unit_ids) != fact_unit_ids | summary_unit_ids:
            errors.append(f"{prefix}:UNIT_MEMBERSHIP_DOES_NOT_MATCH_FACTS")
        if not fact_ids and not summary_unit_ids:
            errors.append(f"{prefix}:NO_FACT_OR_SUMMARY_MEMBERSHIP")
        observed_facts.extend(fact_ids)
        for uid in summary_unit_ids:
            summary_counts[uid] += 1

        expected_stories: list[str] = []
        expected_supports: list[str] = []
        for fact_id in fact_ids:
            record = records.get(fact_id)
            if record is None:
                errors.append(f"{prefix}:FACT_RECORD_MISSING:{fact_id}")
                continue
            expected_stories.extend(str(sid) for sid in record.story_ids)
            expected_supports.extend(str(sid) for sid in record.support_ids)
        for uid in summary_unit_ids:
            expected_stories.extend(str(sid) for sid in units[uid].story_ids)
            expected_supports.extend(str(sid) for sid in units[uid].support_ids)
        expected_item_stories = tuple(dict.fromkeys(expected_stories))
        expected_item_supports = tuple(dict.fromkeys(expected_supports))
        actual_item_stories = tuple(str(sid) for sid in item.covered_story_ids)
        if len(actual_item_stories) != len(set(actual_item_stories)) or set(
            actual_item_stories
        ) != set(expected_item_stories):
            errors.append(f"{prefix}:DERIVED_STORY_MEMBERSHIP_MISMATCH")
        actual_item_supports = tuple(str(sid) for sid in item.cited_support_ids)
        if len(actual_item_supports) != len(set(actual_item_supports)) or set(
            actual_item_supports
        ) != set(expected_item_supports):
            errors.append(f"{prefix}:DERIVED_SUPPORT_MEMBERSHIP_MISMATCH")
        observed_story_ids.update(item.covered_story_ids)

        claims_facts: list[str] = []
        claims_summaries: list[str] = []
        for claim in item.claims:
            claim_fact_ids = tuple(claim.covered_fact_ids)
            claim_summary_ids = tuple(claim.summary_unit_ids)
            if bool(claim_fact_ids) == bool(claim_summary_ids):
                errors.append(f"{prefix}:CLAIM_MUST_MAP_FACTS_OR_SUMMARY")
                continue
            claim_stories: list[str] = []
            claim_supports: list[str] = []
            if claim_fact_ids:
                if not set(claim_fact_ids).issubset(set(fact_ids)):
                    errors.append(f"{prefix}:CLAIM_FACTS_OUTSIDE_ITEM")
                    continue
                for fact_id in claim_fact_ids:
                    record = records.get(fact_id)
                    if record:
                        claim_stories.extend(str(sid) for sid in record.story_ids)
                        claim_supports.extend(str(sid) for sid in record.support_ids)
                claims_facts.extend(claim_fact_ids)
            else:
                if not set(claim_summary_ids).issubset(summary_unit_ids):
                    errors.append(f"{prefix}:CLAIM_SUMMARY_OUTSIDE_ITEM")
                    continue
                for uid in claim_summary_ids:
                    claim_stories.extend(str(sid) for sid in units[uid].story_ids)
                    claim_supports.extend(str(sid) for sid in units[uid].support_ids)
                claims_summaries.extend(claim_summary_ids)
            if tuple(claim.covered_story_ids) != tuple(dict.fromkeys(claim_stories)):
                errors.append(f"{prefix}:CLAIM_STORY_MEMBERSHIP_MISMATCH")
            if tuple(claim.cited_support_ids) != tuple(dict.fromkeys(claim_supports)):
                errors.append(f"{prefix}:CLAIM_SUPPORT_MEMBERSHIP_MISMATCH")
        if len(claims_facts) != len(set(claims_facts)) or set(claims_facts) != set(fact_ids):
            errors.append(f"{prefix}:CLAIM_FACT_PARTITION_MISMATCH")
        if (
            len(claims_summaries) != len(set(claims_summaries))
            or set(claims_summaries) != summary_unit_ids
        ):
            errors.append(f"{prefix}:CLAIM_SUMMARY_PARTITION_MISMATCH")

        if item.source_item_ids:
            source_map = dict(item.source_item_fact_ids)
            if (
                not item.composition_merge_id
                or len(item.source_item_ids) < 2
                or len(item.source_item_ids) != len(set(item.source_item_ids))
                or set(source_map) != set(item.source_item_ids)
            ):
                errors.append(f"{prefix}:MERGE_SOURCE_MAP_INVALID")
                continue
            if any(not ids for ids in source_map.values()):
                errors.append(f"{prefix}:MERGE_SOURCE_WITHOUT_FACTS")
            flattened = [fid for ids in source_map.values() for fid in ids]
            if len(flattened) != len(set(flattened)) or set(flattened) != set(fact_ids):
                errors.append(f"{prefix}:MERGE_FACT_UNION_MISMATCH")
            # Every authorized merge must have at least one exact SAME_FACT edge
            # between source items, and those edges must connect all input items.
            owner = {fid: source_id for source_id, fids in source_map.items() for fid in fids}
            graph: dict[str, set[str]] = {source_id: set() for source_id in item.source_item_ids}
            for relation in merge_relations.values():
                left_owner = owner.get(str(relation.left_fact_id))
                right_owner = owner.get(str(relation.right_fact_id))
                if left_owner and right_owner and left_owner != right_owner:
                    graph[left_owner].add(right_owner)
                    graph[right_owner].add(left_owner)
            reached: set[str] = set()
            frontier = [item.source_item_ids[0]]
            while frontier:
                node = frontier.pop()
                if node in reached:
                    continue
                reached.add(node)
                frontier.extend(graph.get(node, ()))
            if reached != set(item.source_item_ids):
                errors.append(f"{prefix}:MERGE_SOURCE_GRAPH_NOT_CONNECTED")
            if item.item_id != f"item:{item.composition_merge_id}":
                errors.append(f"{prefix}:MERGED_ITEM_ID_MISMATCH")

    expected_facts = {str(fid) for unit in units.values() for fid in unit.fact_ids}
    if len(observed_facts) != len(set(observed_facts)) or set(observed_facts) != expected_facts:
        errors.append(f"COMPOSITION_FACT_PARTITION_MISMATCH:{plan_block.block_id}")
    for unit_id, unit in units.items():
        if not unit.fact_ids and summary_counts[unit_id] != 1:
            errors.append(f"COMPOSITION_SUMMARY_UNIT_COVERAGE_MISMATCH:{unit_id}")
    expected_story_ids = {str(sid) for unit in units.values() for sid in unit.story_ids}
    if observed_story_ids != expected_story_ids:
        errors.append(f"COMPOSITION_STORY_COVERAGE_MISMATCH:{plan_block.block_id}")
    return errors


def _composition_visible_risk_validation(
    *,
    item: DigestEditorialItemDraft,
    plan: DigestNarrativePlan,
    plan_block: DigestNarrativeBlock,
    support_map: Mapping[str, str],
) -> tuple[list[str], list[ConcreteClaim], list[str]]:
    """Check visible risk fields only when they can be bound to exact fact support.

    A failed binding is not evidence that the copy is false. It is recorded as
    NOT_EVALUATED and left for editorial review; this avoids turning incomplete
    metadata or a legitimate single-source report into a publication blocker.
    """
    from src.publication.digest_presentation import (
        _load_digest_geography_resolver,
        _resolved_geographic_scopes,
    )
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    violations: list[str] = []
    unsupported: list[ConcreteClaim] = []
    not_evaluated: list[str] = []
    records = {
        str(record.fact_id): record
        for record in plan_block.composition_fact_records
        if getattr(record, "fact_id", None)
    }
    # A location can resolve to multiple frozen facts (for example, more than
    # one report for the same place). They may share proof only when the
    # composition has explicitly sealed them as one SAME_FACT component.
    same_fact_parent = {fact_id: fact_id for fact_id in records}

    def same_fact_root(fact_id: str) -> str:
        parent = same_fact_parent.get(fact_id, fact_id)
        while parent != same_fact_parent.get(parent, parent):
            parent = same_fact_parent[parent]
        node = fact_id
        while node in same_fact_parent and same_fact_parent[node] != parent:
            next_node = same_fact_parent[node]
            same_fact_parent[node] = parent
            node = next_node
        return parent

    for relation in plan_block.composition_relations:
        if str(getattr(relation.kind, "value", relation.kind)) != "SAME_FACT":
            continue
        left = str(getattr(relation, "left_fact_id", ""))
        right = str(getattr(relation, "right_fact_id", ""))
        if left not in same_fact_parent or right not in same_fact_parent:
            continue
        left_root = same_fact_root(left)
        right_root = same_fact_root(right)
        if left_root != right_root:
            # Stable root selection makes the component deterministic.
            root, child = sorted((left_root, right_root))
            same_fact_parent[child] = root

    fact_ids = {
        str(fact_id)
        for claim in item.claims
        for fact_id in claim.covered_fact_ids
        if str(fact_id) in records
    }
    signatures = {
        (
            str(records[fact_id].canonical_subject),
            str(records[fact_id].canonical_service),
            str(records[fact_id].canonical_area),
            tuple(records[fact_id].canonical_place),
            str(records[fact_id].service_state),
            records[fact_id].effective_time,
        )
        for fact_id in fact_ids
    }
    resolver = _load_digest_geography_resolver(plan.edition_slug)
    visible_item_text = f"{item.headline} {item.body}"
    if re.search(
        r"(?i)\b(?:свет\w*|электр\w*|вод\w*|газ\w*|отоплен\w*|интернет\w*|связ\w*|автобус\w*|транспорт\w*)\b",
        visible_item_text,
    ):
        not_evaluated.append(
            f"VISIBLE_SERVICE_STATE_BINDING:{plan_block.block_id}:{item.item_id or 'item'}"
        )
    if re.search(
        r"(?i)(?:\b(?:сегодня|сейчас|завтра|вчера|к\s+вечеру|ожида\w*|планир\w*|обеща\w*|восстанов\w*|по\s+графику|по\s+плану|в\s+течение|до\s+конца)\b|\b(?:до|после|с|к)\s+\d{1,2}(?:[:.]\d{2})?\b)",
        visible_item_text,
    ):
        not_evaluated.append(
            f"VISIBLE_TEMPORAL_SCOPE_BINDING:{plan_block.block_id}:{item.item_id or 'item'}"
        )

    def record_geography(record: Any) -> tuple[set[str], set[str], bool]:
        places = {str(value) for value in (getattr(record, "canonical_place", ()) or ()) if value}
        areas = {str(getattr(record, "canonical_area", ""))} - {""}
        has_location = bool(places or areas)
        if resolver is not None and not places:
            record_location_text = " ".join(
                str(value)
                for value in (
                    getattr(record, "original_location", ""),
                    getattr(record, "text", ""),
                )
                if value
            )
            if record_location_text:
                record_annotation = resolver.resolve(record_location_text)
                places.update(
                    str(entity.entity_id)
                    for entity in getattr(record_annotation, "entities", ())
                    if getattr(entity, "kind", "") == "place"
                    and getattr(entity, "confidence", "") == "high"
                    and getattr(entity, "entity_id", "")
                )
                areas.update(_resolved_geographic_scopes(record_location_text, resolver))
                has_location = bool(places or areas)
        return places, areas, has_location

    record_geography_by_fact = {
        fact_id: record_geography(record) for fact_id, record in records.items()
    }

    def scoped_fact_ids(clause: str) -> tuple[set[str] | None, bool]:
        """Return exact location-matched facts, or None when no safe binding exists."""
        if resolver is None:
            return None, False
        annotation = resolver.resolve(clause)
        place_ids = {
            str(entity.entity_id)
            for entity in getattr(annotation, "entities", ())
            if getattr(entity, "kind", "") == "place"
            and getattr(entity, "confidence", "") == "high"
        }
        area_ids = set(_resolved_geographic_scopes(clause, resolver))
        if not place_ids and not area_ids:
            return None, False
        # Prefer a physical place to its parent area. Multiple places or areas
        # cannot be bound to one risk field without a reliable clause parser.
        if len(place_ids) > 1 or (not place_ids and len(area_ids) > 1):
            return None, True
        matching_fact_ids: set[str] = set()
        for fact_id in fact_ids:
            record_places, record_areas, _has_location = record_geography_by_fact[fact_id]
            if place_ids.intersection(record_places) or (
                not place_ids and area_ids.intersection(record_areas)
            ):
                matching_fact_ids.add(fact_id)
        return matching_fact_ids, True

    def add_not_evaluated(code: str) -> None:
        not_evaluated.append(f"{code}:{plan_block.block_id}:{item.item_id or 'item'}")

    for visible_text in (item.headline, item.body):
        for sentence in re.split(r"(?<=[.!?;])\s+", visible_text or ""):
            for clause in re.split(
                r"(?i)(?:,\s*|\s+)(?:тогда как|в то время как|при этом|зато|но|а)\s+",
                sentence,
            ):
                clause = clause.strip(" ,—-\t\n")
                if not clause:
                    continue
                visible_risks = extract_concrete_claims(clause)
                fact_scope, has_location = scoped_fact_ids(clause)
                if has_location and fact_scope == set():
                    # The prose names a resolved location, but the fact records
                    # do not give us a reliable matching fact. Do not infer that
                    # a writer invented it from missing/incomplete metadata.
                    add_not_evaluated("VISIBLE_PLACE_FACT_BINDING")
                    fact_scope = None
                if fact_scope is None:
                    if not has_location and len(signatures) == 1:
                        fact_scope = set(fact_ids)
                    else:
                        if visible_risks:
                            for risk in visible_risks:
                                add_not_evaluated(f"RISK_FIELD_FACT_BINDING:{risk.kind}")
                        if has_location or len(signatures) > 1:
                            add_not_evaluated("VISIBLE_PROSE_FACT_BINDING")
                        continue

                fact_components = {same_fact_root(fid) for fid in fact_scope}
                if len(fact_scope) > 1 and len(fact_components) > 1:
                    for risk in visible_risks:
                        add_not_evaluated(f"RISK_FIELD_FACT_BINDING:{risk.kind}")
                    add_not_evaluated("VISIBLE_PROSE_FACT_BINDING")
                    continue

                scoped_records = [records[fid] for fid in fact_scope if fid in records]
                # A single frozen fact can itself name several places. A
                # visible field cannot safely borrow that fact's supports for
                # just one place, so leave it for review rather than treating
                # the mixed location record as a precise binding.
                if visible_risks and any(
                    len(set(getattr(record, "canonical_place", ()) or ())) > 1
                    for record in scoped_records
                ):
                    for risk in visible_risks:
                        add_not_evaluated(f"RISK_FIELD_FACT_BINDING:{risk.kind}")
                    add_not_evaluated("VISIBLE_PLACE_FACT_BINDING")
                    continue
                scoped_signatures = {
                    (
                        str(record.canonical_subject),
                        str(record.canonical_service),
                        str(record.canonical_area),
                        tuple(record.canonical_place),
                        str(record.service_state),
                        record.effective_time,
                    )
                    for record in scoped_records
                }
                if len(scoped_signatures) > 1:
                    for risk in visible_risks:
                        add_not_evaluated(f"RISK_FIELD_FACT_BINDING:{risk.kind}")
                    add_not_evaluated("VISIBLE_PROSE_FACT_BINDING")
                    continue

                for risk in visible_risks:
                    supported = False
                    had_exact_support = False
                    for fact_id in fact_scope:
                        record = records.get(fact_id)
                        if record is None:
                            continue
                        exact_supports = [
                            support_map[support_id]
                            for support_id in (getattr(record, "support_ids", ()) or ())
                            if support_id in support_map
                        ]
                        if not exact_supports:
                            continue
                        had_exact_support = True
                        if not find_unsupported_claims(
                            risk.raw, exact_supports
                        ) and not find_unsupported_digest_relations(clause, exact_supports):
                            supported = True
                            break
                    if supported:
                        continue
                    if not had_exact_support:
                        add_not_evaluated(f"RISK_FIELD_SUPPORT_UNAVAILABLE:{risk.kind}")
                        continue
                    unsupported.append(risk)
                    violations.append(
                        f"UNSUPPORTED_CONCRETE_CLAIM: [{risk.kind}] '{risk.raw}' "
                        f"is not supported by its exact fact support in block {plan_block.block_id}"
                    )

    return violations, unsupported, list(dict.fromkeys(not_evaluated))


def validate_digest_narrative(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
    support_index: Mapping[str, str] | None = None,
    *,
    support_text_by_id: Mapping[str, str] | None = None,
    situation_plan: Any = None,
    allowed_context_terms: Sequence[str] = (),
    all_known_draft_supports: Sequence[str] = (),
) -> DigestNarrativeValidationResult:
    """Validate structured narrative digest draft strictly against deterministic plan and evidence."""
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    ctx_terms: Sequence[str] = tuple(allowed_context_terms) if allowed_context_terms else ()
    known_supports: Sequence[str] = (
        tuple(all_known_draft_supports) if all_known_draft_supports else ()
    )
    violations: list[str] = []
    unsupported_claims: list[Any] = []
    not_evaluated: list[str] = []
    support_map = support_index if support_index is not None else (support_text_by_id or {})

    plan_blocks_by_id = {b.block_id: b for b in plan.blocks}
    draft_block_ids = [b.block_id for b in draft.blocks]
    plan_block_ids = [b.block_id for b in plan.blocks]

    if len(draft.blocks) != len(plan.blocks):
        violations.append(
            f"BLOCK_SET_MISMATCH: expected {len(plan.blocks)} blocks, got {len(draft.blocks)}"
        )

    if draft_block_ids != plan_block_ids:
        violations.append(f"BLOCK_SET_MISMATCH: expected {plan_block_ids}, got {draft_block_ids}")

    for out_block in draft.blocks:
        plan_block = plan_blocks_by_id.get(out_block.block_id)
        if plan_block is None:
            violations.append(f"UNKNOWN_BLOCK_ID: {out_block.block_id}")
            continue

        composition_path = bool(plan_block.composition_units)
        if composition_path:
            violations.extend(_validate_composition_membership(out_block, plan_block))
        # The legacy path uses cross-draft terms to tolerate old schemas and
        # synthesized notes. Composition claims already have exact PUBLISH
        # supports derived from their fact IDs, so unrelated or CONTEXT text
        # must not act as a factual waiver here.
        factual_context_terms: Sequence[str] = () if composition_path else ctx_terms
        factual_known_supports: Sequence[str] = () if composition_path else known_supports

        allowed_supports = set(plan_block.support_ids)
        expected_story_ids = set(plan_block.story_ids)
        merge_group_map = dict(plan_block.merge_group_by_story)

        flat_story_ids = [sid for item in out_block.items for sid in item.covered_story_ids]
        if not composition_path and len(flat_story_ids) != len(set(flat_story_ids)):
            violations.append(f"DUPLICATE_STORY_COVERAGE: {out_block.block_id}")

        for sid in flat_story_ids:
            if sid not in expected_story_ids:
                violations.append(f"UNKNOWN_STORY_ID: {sid} in block {out_block.block_id}")

        if set(flat_story_ids) != expected_story_ids:
            violations.append(f"STORY_PARTITION_MISMATCH: {out_block.block_id}")

        allowed_by_story = dict(plan_block.support_ids_by_story)

        for item in out_block.items:
            for sid in item.covered_story_ids:
                story_allowed = set(allowed_by_story.get(sid, ()))
                if story_allowed and not (set(item.cited_support_ids) & story_allowed):
                    violations.append(
                        f"STORY_SUPPORT_MISSING: story {sid} in block {out_block.block_id}"
                    )

            # Require structured claims only when the block carries required_facts to cover
            if not item.claims and plan_block.required_facts:
                violations.append(f"ITEM_CLAIMS_MISSING: item in block {out_block.block_id}")

            if item.claims:
                claimed_story_ids = {sid for c in item.claims for sid in c.covered_story_ids}
                for sid in item.covered_story_ids:
                    if sid not in claimed_story_ids:
                        violations.append(
                            f"STORY_CLAIM_COVERAGE_MISSING:{sid} in block {out_block.block_id}"
                        )

                for claim in item.claims:
                    for sid in claim.covered_story_ids:
                        if sid not in item.covered_story_ids:
                            violations.append(
                                f"UNKNOWN_STORY_ID: {sid} in claim of block {out_block.block_id}"
                            )
                        story_allowed = set(allowed_by_story.get(sid, ()))
                        if story_allowed and not (set(claim.cited_support_ids) & story_allowed):
                            violations.append(
                                f"STORY_SUPPORT_MISSING: story {sid} in claim of block {out_block.block_id}"
                            )
                    if not claim.cited_support_ids:
                        violations.append(
                            f"CLAIM_WITHOUT_SUPPORT: claim '{claim.text[:30]}' in block {out_block.block_id} cites no supports"
                        )
                    for sup_id in claim.cited_support_ids:
                        if sup_id not in item.cited_support_ids:
                            violations.append(
                                f"CLAIM_SUPPORT_OUTSIDE_ITEM: {sup_id} in block {out_block.block_id}"
                            )
                        if sup_id not in allowed_supports and allowed_supports:
                            violations.append(
                                f"SUPPORT_OUTSIDE_BLOCK: {sup_id} not allowed in block {out_block.block_id}"
                            )
                        if sup_id not in support_map:
                            violations.append(
                                f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index in block {out_block.block_id}"
                            )

                    c_claim_supports = [
                        support_map[s] for s in claim.cited_support_ids if s in support_map
                    ]
                    for unc in find_unsupported_claims(
                        claim.text,
                        c_claim_supports,
                        allowed_context_terms=factual_context_terms,
                        all_known_draft_supports=factual_known_supports,
                    ):
                        unsupported_claims.append(unc)
                        violations.append(
                            f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in claim of block {out_block.block_id}"
                        )
                    for rel in find_unsupported_digest_relations(claim.text, c_claim_supports):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in claim of block {out_block.block_id}"
                        )
                    for rec in find_unsupported_digest_recommendations(
                        claim.text, c_claim_supports
                    ):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in claim of block {out_block.block_id}"
                        )

                    # Validate material fact references within the claim
                    block_req_facts_by_id = {rf.fact_id: rf for rf in plan_block.required_facts}
                    for fid in claim.covered_fact_ids:
                        if fid not in block_req_facts_by_id:
                            violations.append(
                                f"UNKNOWN_DIGEST_FACT_ID:{fid} in block {out_block.block_id}"
                            )
                        else:
                            rf = block_req_facts_by_id[fid]
                            rf_allowed_sups = set(rf.support_ids)
                            story_sups: set[str] = set()
                            for sid in rf.story_ids:
                                story_sups.update(allowed_by_story.get(sid, ()))
                            allowed_fact_sups = rf_allowed_sups | story_sups
                            if allowed_fact_sups and not (
                                set(claim.cited_support_ids) & allowed_fact_sups
                            ):
                                violations.append(
                                    f"DIGEST_FACT_SUPPORT_MISSING:{fid} in block {out_block.block_id}"
                                )

            if not composition_path and len(item.covered_story_ids) > 1 and merge_group_map:
                m_groups = {merge_group_map.get(sid, sid) for sid in item.covered_story_ids}
                if len(m_groups) > 1:
                    violations.append(f"UNRELATED_STORY_GROUPING: {out_block.block_id}")

            # DRILL_DOWN enforcement: items covering a DRILL_DOWN story must cite
            # at least one of that story's designated detail support IDs.
            drill_roles = dict(plan_block.detail_roles_by_story)
            detail_sups_map = dict(plan_block.detail_support_ids_by_story)
            for sid in item.covered_story_ids:
                if drill_roles.get(sid) == "DRILL_DOWN":
                    drill_sups = set(detail_sups_map.get(sid, ()))
                    if drill_sups and not (set(item.cited_support_ids) & drill_sups):
                        violations.append(
                            f"DRILL_DOWN_MISSING_DISTINCT_SUPPORT: story {sid} in block {out_block.block_id}"
                        )

            if item.headline:
                if len(item.headline) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                    violations.append(
                        f"HEADLINE_TOO_LONG: headline exceeds {DIGEST_ITEM_HEADLINE_MAX_CHARS} chars in block {out_block.block_id}"
                    )
                if _INTERNAL_LEAKAGE_RE.search(item.headline):
                    violations.append(
                        f"INTERNAL_ID_LEAK: found internal identifier in block {out_block.block_id}"
                    )

            if len(item.body) > DIGEST_ITEM_BODY_MAX_CHARS:
                violations.append(
                    f"BODY_TOO_LONG: body exceeds {DIGEST_ITEM_BODY_MAX_CHARS} chars in block {out_block.block_id}"
                )
            if _INTERNAL_LEAKAGE_RE.search(item.body):
                violations.append(
                    f"INTERNAL_ID_LEAK: found internal identifier in block {out_block.block_id}"
                )

            if not item.cited_support_ids:
                violations.append(
                    f"MISSING_SUPPORT_CITATION: item in block {out_block.block_id} cites no supports"
                )

            for sup_id in item.cited_support_ids:
                if sup_id not in allowed_supports and allowed_supports:
                    violations.append(
                        f"SUPPORT_OUTSIDE_BLOCK: {sup_id} not allowed in block {out_block.block_id}"
                    )
                if sup_id not in support_map:
                    violations.append(
                        f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index"
                    )

            # Validate visible copy. Composition claims have exact fact-derived
            # supports above; do not let the entire item support union prove a
            # detail about a different place/state. The narrow visible bridge
            # checks extractable fields against scoped evidence and labels
            # unresolved semantic binding as NOT_EVALUATED.
            if composition_path:
                risk_violations, risk_claims, risk_not_evaluated = (
                    _composition_visible_risk_validation(
                        item=item,
                        plan=plan,
                        plan_block=plan_block,
                        support_map=support_map,
                    )
                )
                violations.extend(risk_violations)
                unsupported_claims.extend(risk_claims)
                not_evaluated.extend(risk_not_evaluated)
                for claim in item.claims:
                    claim_supports = [
                        support_map[sid] for sid in claim.cited_support_ids if sid in support_map
                    ]
                    for rec in find_unsupported_digest_recommendations(claim.text, claim_supports):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in claim of block {out_block.block_id}"
                        )
            else:
                c_supports = [support_map[s] for s in item.cited_support_ids if s in support_map]
                if item.headline:
                    for unc in find_unsupported_claims(
                        item.headline,
                        c_supports,
                        allowed_context_terms=factual_context_terms,
                        all_known_draft_supports=factual_known_supports,
                    ):
                        unsupported_claims.append(unc)
                        violations.append(
                            f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in headline of block {out_block.block_id}"
                        )
                    for rel in find_unsupported_digest_relations(item.headline, c_supports):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in headline of block {out_block.block_id}"
                        )
                    for rec in find_unsupported_digest_recommendations(item.headline, c_supports):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in headline of block {out_block.block_id}"
                        )
                for unc in find_unsupported_claims(
                    item.body,
                    c_supports,
                    allowed_context_terms=factual_context_terms,
                    all_known_draft_supports=factual_known_supports,
                ):
                    unsupported_claims.append(unc)
                    violations.append(
                        f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in body of block {out_block.block_id}"
                    )
                for rel in find_unsupported_digest_relations(item.body, c_supports):
                    violations.append(
                        f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in body of block {out_block.block_id}"
                    )
                for rec in find_unsupported_digest_recommendations(item.body, c_supports):
                    violations.append(
                        f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in body of block {out_block.block_id}"
                    )

        # Block-level strict required material facts coverage check
        for rf in plan_block.required_facts:
            rf_covered = False
            rf_allowed_supports = set(rf.support_ids)
            story_sups = set()
            for sid in rf.story_ids:
                story_sups.update(allowed_by_story.get(sid, ()))
            allowed_fact_sups = rf_allowed_supports | story_sups

            for item in out_block.items:
                for c in item.claims:
                    claim_sups = set(c.cited_support_ids)
                    if rf_allowed_supports and (claim_sups & rf_allowed_supports):
                        if rf.fact_id in c.covered_fact_ids:
                            rf_covered = True
                            break
                    elif c.covered_fact_ids and rf.fact_id in c.covered_fact_ids:
                        if not allowed_fact_sups or (claim_sups & allowed_fact_sups):
                            rf_covered = True
                            break
                if rf_covered:
                    break
            if not rf_covered:
                violations.append(
                    f"DIGEST_FACT_COVERAGE_MISSING:{rf.fact_id} in block {out_block.block_id}"
                )

    is_valid = len(violations) == 0 and len(unsupported_claims) == 0
    return DigestNarrativeValidationResult(
        is_valid=is_valid,
        violations=tuple(violations),
        unsupported_claims=tuple(unsupported_claims),
        not_evaluated=tuple(dict.fromkeys(not_evaluated)),
    )


def _render_deterministic_digest_evidence(evi: PublicationEvidence) -> str:
    text = (evi.text or evi.source_text).strip()
    if evi.kind in {"community_report", "community_observation", "quote_assertion"}:
        if not text.casefold().startswith(("по сообщениям", "жители сообщают", "по словам")):
            if text:
                text = f"По сообщениям жителей, {text[:1].lower() + text[1:]}"
    return text.rstrip(". ") + "."


def build_deterministic_digest_draft(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[Any],
    presentation_plan: Any,
    allowed_context_terms: Sequence[str] = (),
    all_known_draft_supports: Sequence[str] = (),
    support_text_by_id: Mapping[str, str] | None = None,
) -> DigestNarrativeDraft:
    """Build a deterministic, provenance-bearing DigestNarrativeDraft from the presentation plan."""
    from src.publication.article_claims import find_unsupported_claims
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    support_map: dict[str, str] = (
        dict(support_text_by_id)
        if support_text_by_id is not None
        else build_digest_support_text_index(evidence=evidence, cards=cards)
    )

    ctx_terms: Sequence[str] = tuple(allowed_context_terms) if allowed_context_terms else ()
    known_supports: Sequence[str] = (
        tuple(all_known_draft_supports) if all_known_draft_supports else ()
    )
    detail_story_ids = set(getattr(presentation_plan, "detail_story_ids", ()))
    detail_cards = [c for c in cards if c.id in detail_story_ids]

    narrative_plan = plan_digest_narrative_blocks(
        cards=detail_cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=presentation_plan,
    )

    presentations_by_id = {
        p.story_id: p for p in getattr(presentation_plan, "story_presentations", ())
    }
    cards_by_id = {c.id: c for c in detail_cards}

    dashboard_supports_by_story: dict[str, set[str]] = {}
    if getattr(presentation_plan, "city_situation", None):
        for g in getattr(presentation_plan.city_situation, "groups", ()):
            for sid in getattr(g, "covered_story_ids", ()):
                dashboard_supports_by_story.setdefault(sid, set()).update(
                    getattr(g, "cited_support_ids", ())
                )

    block_drafts: list[DigestNarrativeBlockDraft] = []
    for plan_block in narrative_plan.blocks:
        item_drafts: list[DigestEditorialItemDraft] = []

        community_attribution_count = 0
        if getattr(plan_block, "topic_bundles", None):
            from src.publication.digest_presentation import (
                _clean_fact_sentence,
                _is_usable_fact_line,
            )

            for bundle in plan_block.topic_bundles:
                usable_facts: list[str] = []
                fact_keys: list[str] = []
                fact_token_sets: list[set[str]] = []
                fact_number_sets: list[set[str]] = []
                for fact in bundle.fact_ledger:
                    cleaned_fact = _clean_fact_sentence(fact)
                    for atom in re.split(r"(?<=[.!?])\s+", cleaned_fact):
                        if not _is_usable_fact_line(atom):
                            continue
                        atom = _clean_fact_sentence(atom)
                        fact_key = " ".join(re.findall(r"[\w-]+", atom.casefold()))
                        atom_tokens = set(re.findall(r"[\w-]+", atom.casefold()))
                        atom_numbers = set(re.findall(r"\d+", atom))
                        if not fact_key or any(
                            fact_key == previous
                            or fact_key in previous
                            or previous in fact_key
                            or (
                                atom_tokens
                                and len(atom_tokens & previous_tokens)
                                / min(len(atom_tokens), len(previous_tokens))
                                >= 0.78
                                and atom_numbers == previous_numbers
                            )
                            for previous, previous_tokens, previous_numbers in zip(
                                fact_keys,
                                fact_token_sets,
                                fact_number_sets,
                            )
                        ):
                            continue
                        fact_keys.append(fact_key)
                        fact_token_sets.append(atom_tokens)
                        fact_number_sets.append(atom_numbers)
                        usable_facts.append(atom)
                if not usable_facts:
                    for sid in bundle.story_ids:
                        c = cards_by_id.get(sid)
                        if c and c.summary and _is_usable_fact_line(c.summary):
                            usable_facts.append(_clean_fact_sentence(c.summary))
                        elif c and c.topic and _is_usable_fact_line(c.topic):
                            usable_facts.append(_clean_fact_sentence(c.topic))
                if not usable_facts:
                    for required_fact in bundle.required_facts:
                        cleaned_required = _clean_fact_sentence(required_fact.text)
                        if _is_usable_fact_line(cleaned_required):
                            usable_facts.append(cleaned_required)
                if not usable_facts:
                    # A bundle without a grounded reader-facing fact must not
                    # be padded with synthetic operational boilerplate.  Such
                    # a bundle should have been filtered before planning; keep
                    # the fallback fail-closed if a stale persisted revision
                    # still reaches this point.
                    logger.warning(
                        "Skipping digest bundle without usable grounded facts: %s",
                        bundle.bundle_id,
                    )
                    continue

                if len(bundle.story_ids) == 1:
                    c = cards_by_id.get(bundle.story_ids[0])
                    if (
                        c
                        and c.topic
                        and len(c.topic.strip()) >= 3
                        and not _GENERIC_DIGEST_TOPIC_RE.fullmatch(c.topic.strip())
                        and _is_usable_fact_line(c.topic)
                    ):
                        headline = c.topic.strip()
                    elif _GENERIC_DIGEST_TOPIC_RE.fullmatch(bundle.topic_label.strip()):
                        headline = _headline_from_digest_fact(usable_facts[0])
                    elif bundle.locations:
                        locs_text = ", ".join(bundle.locations[:3])
                        headline = f"{bundle.topic_label}: ситуация в районах {locs_text}"
                    else:
                        headline = _headline_from_digest_fact(usable_facts[0])
                elif bundle.locations:
                    locs_text = ", ".join(bundle.locations[:3])
                    headline = f"{bundle.topic_label}: ситуация в районах {locs_text}"
                else:
                    headline = (
                        _headline_from_digest_fact(usable_facts[0])
                        if _GENERIC_DIGEST_TOPIC_RE.fullmatch(bundle.topic_label.strip())
                        else f"{bundle.topic_label}: обзор сообщений"
                    )
                headline = headline.lstrip(" ,.-:;—")
                if headline and headline[0].islower():
                    headline = headline[0].upper() + headline[1:]
                headline = re.sub(
                    r"\bтакже\s+сообщается,\s+что\s+ранее\b",
                    "ранее сообщалось, что",
                    headline,
                    flags=re.IGNORECASE,
                )
                headline = re.sub(
                    r"\b(?:ранее\s+также|также\s+ранее)\b", "ранее", headline, flags=re.IGNORECASE
                )
                headline = re.sub(r"\bиз-за\b", "при", headline, flags=re.IGNORECASE)
                headline = re.sub(r"\bв\s+результате\b", "после", headline, flags=re.IGNORECASE)
                headline = re.sub(r"\bпо\s+причине\b", "при", headline, flags=re.IGNORECASE)
                headline = re.sub(r"\bвследствие\b", "после", headline, flags=re.IGNORECASE)
                if len(headline) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                    headline = (
                        headline[:DIGEST_ITEM_HEADLINE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(".:;, ")
                    )

                body_sentences: list[str] = []
                current_len = 0
                for f in usable_facts[:6]:
                    cf = _clean_fact_sentence(f)
                    cf = re.sub(r"\bиз-за\b", "при", cf, flags=re.IGNORECASE)
                    cf = re.sub(r"\bв\s+результате\b", "после", cf, flags=re.IGNORECASE)
                    cf = re.sub(r"\bпо\s+причине\b", "при", cf, flags=re.IGNORECASE)
                    cf = re.sub(r"\bвследствие\b", "после", cf, flags=re.IGNORECASE)
                    if cf and cf not in body_sentences:
                        if current_len + len(cf) + 2 > DIGEST_ITEM_BODY_MAX_CHARS:
                            break
                        body_sentences.append(cf)
                        current_len += len(cf) + 2
                if not body_sentences:
                    body_sentences = [
                        f"{bundle.topic_label} в городе остаётся на контроле городских служб."
                    ]

                is_community = any(
                    getattr(evidence.get(s), "kind", "")
                    in {"community_report", "community_observation", "quote_assertion"}
                    or getattr(evidence.get(s), "source_role", "") in {"citizen", "community"}
                    for s in bundle.support_ids
                    if s in evidence
                )
                first_sent = body_sentences[0]
                if is_community:
                    has_prior_attribution = (
                        any(
                            first_sent.casefold().startswith(p)
                            for p in (
                                "по сообщениям",
                                "жители сообщают",
                                "по словам",
                                "как сообщают",
                                "как отмечают",
                                "по информации горожан",
                            )
                        )
                        or "сообщают" in first_sent.casefold()
                        or "отмечают" in first_sent.casefold()
                    )
                    if not has_prior_attribution and community_attribution_count == 0:
                        attributions = [
                            "По сообщениям жителей",
                            "По словам горожан",
                            "Жители сообщают",
                            "Очевидцы отмечают",
                        ]
                        att = attributions[community_attribution_count % len(attributions)]
                        first_sent = f"{att}, {first_sent[:1].lower() + first_sent[1:]}"
                        community_attribution_count += 1
                else:
                    if not first_sent.casefold().startswith(
                        ("по информации", "по данным", "согласно")
                    ):
                        first_sent = f"По информации коммунальных служб, {first_sent[:1].lower() + first_sent[1:]}"
                body_sentences[0] = first_sent

                chosen_sups: list[str] = []
                for sid in bundle.story_ids:
                    pres = presentations_by_id.get(sid)
                    if pres and pres.detail_support_ids:
                        for s in pres.detail_support_ids:
                            if s in support_map and s not in chosen_sups:
                                chosen_sups.append(s)

                bundle_req_facts = [
                    rf
                    for rf in plan_block.required_facts
                    if bool(set(rf.story_ids) & set(bundle.story_ids))
                ]
                for rf in bundle_req_facts:
                    for s in rf.support_ids:
                        if s in support_map and s not in chosen_sups:
                            chosen_sups.append(s)

                has_explicit_detail_supports = any(
                    bool(
                        presentations_by_id.get(sid) and presentations_by_id[sid].detail_support_ids
                    )
                    for sid in bundle.story_ids
                )
                if len(bundle.story_ids) > 1 or not has_explicit_detail_supports:
                    for s in bundle.support_ids:
                        if s in support_map and s not in chosen_sups:
                            chosen_sups.append(s)

                if not chosen_sups:
                    chosen_sups = (
                        list(bundle.support_ids[:2])
                        if bundle.support_ids
                        else [bundle.story_ids[0]]
                    )

                c_sups_texts = [support_map[s] for s in chosen_sups if s in support_map]
                if find_unsupported_claims(
                    headline,
                    c_sups_texts,
                    allowed_context_terms=ctx_terms,
                    all_known_draft_supports=known_supports,
                ):
                    if bundle.locations:
                        locs_text = ", ".join(bundle.locations[:3])
                        headline = f"{bundle.topic_label}: ситуация в районах {locs_text}"
                    else:
                        headline = (
                            _headline_from_digest_fact(usable_facts[0])
                            if usable_facts
                            else f"{bundle.topic_label}: ситуация в городе"
                        )

                valid_body_sentences = []
                for s in body_sentences:
                    if not find_unsupported_claims(
                        s,
                        c_sups_texts,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    ):
                        valid_body_sentences.append(s)
                if not valid_body_sentences:
                    valid_body_sentences = [usable_facts[0]]
                body_text = " ".join(valid_body_sentences)
                if len(body_text) > DIGEST_ITEM_BODY_MAX_CHARS:
                    body_text = (
                        body_text[:DIGEST_ITEM_BODY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(".:;, ")
                        + "."
                    )
                body_text = _deduplicate_digest_attribution(body_text)
                if _DIGEST_ATTRIBUTION_RE.search(body_text):
                    headline = _strip_leading_digest_attribution(headline)

                headline, body_text = _fix_redundant_headline_and_body(
                    headline, body_text, bundle.topic_label
                )

                base_claim_text = _clean_fact_sentence(usable_facts[0])
                base_claim_text = re.sub(r"\bиз-за\b", "при", base_claim_text, flags=re.IGNORECASE)
                if find_unsupported_claims(
                    base_claim_text,
                    c_sups_texts,
                    allowed_context_terms=ctx_terms,
                    all_known_draft_supports=known_supports,
                ):
                    base_claim_text = f"{bundle.topic_label} остаётся на контроле городских служб."

                item_claims: list[DigestClaimAtom] = [
                    DigestClaimAtom(
                        text=base_claim_text,
                        covered_story_ids=tuple(bundle.story_ids),
                        cited_support_ids=tuple(chosen_sups),
                        covered_fact_ids=(),
                    )
                ]
                for extra_fact in usable_facts[1:3]:
                    ef_text = _clean_fact_sentence(extra_fact)
                    ef_text = re.sub(r"\bиз-за\b", "при", ef_text, flags=re.IGNORECASE)
                    if not find_unsupported_claims(
                        ef_text,
                        c_sups_texts,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    ):
                        item_claims.append(
                            DigestClaimAtom(
                                text=ef_text,
                                covered_story_ids=tuple(bundle.story_ids),
                                cited_support_ids=tuple(chosen_sups),
                                covered_fact_ids=(),
                            )
                        )
                for rf in bundle_req_facts:
                    rf_text = _clean_fact_sentence(rf.text or base_claim_text)
                    rf_text = re.sub(r'["«»“„\']', "", rf_text)
                    rf_text = re.sub(r"\bиз-за\b", "при", rf_text, flags=re.IGNORECASE)
                    allowed_fact_sups = set(rf.support_ids)
                    for sid in rf.story_ids:
                        pres = presentations_by_id.get(sid)
                        if pres and pres.detail_support_ids:
                            allowed_fact_sups.update(pres.detail_support_ids)
                        if sid in support_map:
                            allowed_fact_sups.add(sid)
                    rf_sups = [s for s in rf.support_ids if s in support_map]
                    for sid in rf.story_ids:
                        if sid in support_map and sid not in rf_sups:
                            rf_sups.append(sid)
                    if not rf_sups:
                        rf_sups = [s for s in allowed_fact_sups if s in support_map]
                    if not rf_sups:
                        rf_sups = list(chosen_sups)
                    for s in rf_sups:
                        if s not in chosen_sups:
                            chosen_sups.append(s)

                    rf_sups_texts = [support_map[s] for s in rf_sups if s in support_map]
                    if find_unsupported_claims(
                        rf_text,
                        rf_sups_texts,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    ):
                        rf_text = base_claim_text

                    item_claims.append(
                        DigestClaimAtom(
                            text=rf_text,
                            covered_story_ids=tuple(set(rf.story_ids) & set(bundle.story_ids))
                            or tuple(bundle.story_ids[:1]),
                            cited_support_ids=tuple(rf_sups),
                            covered_fact_ids=(rf.fact_id,),
                        )
                    )

                item_drafts.append(
                    DigestEditorialItemDraft(
                        headline=headline,
                        body=body_text,
                        emoji=bundle.emoji,
                        covered_story_ids=tuple(bundle.story_ids),
                        cited_support_ids=tuple(chosen_sups),
                        claims=tuple(item_claims),
                    )
                )

            if item_drafts:
                block_drafts.append(
                    DigestNarrativeBlockDraft(
                        block_id=plan_block.block_id,
                        items=tuple(item_drafts),
                    )
                )
            continue

        groups_to_cover = (
            plan_block.required_story_groups
            if plan_block.required_story_groups
            else tuple((sid,) for sid in plan_block.story_ids)
        )
        for story_group in groups_to_cover:
            group_chosen_supports: list[str] = []
            group_rendered_sentences: list[str] = []
            group_support_texts: list[str] = []
            lead_topic: str = ""
            sid_chosen_supports: dict[str, list[str]] = {}
            sid_support_texts: dict[str, list[str]] = {}

            for sid in story_group:
                pres = presentations_by_id.get(sid)
                card = cards_by_id.get(sid)
                if not card:
                    continue
                if not lead_topic and card.topic:
                    lead_topic = card.topic.strip()

                dash_supp_ids = dashboard_supports_by_story.get(sid, set())
                eligible_supports: list[str] = []

                if pres and pres.detail_support_ids:
                    for supp_id in pres.detail_support_ids:
                        if supp_id not in dash_supp_ids and (supp_id in evidence or supp_id):
                            eligible_supports.append(supp_id)

                if not eligible_supports:
                    num_sid: int | None = None
                    if sid.startswith("story:"):
                        num_part = sid.split(":", 1)[1]
                        if num_part.isdigit():
                            num_sid = int(num_part)

                    for eid, evi in evidence.items():
                        evi_sid = getattr(evi, "story_id", None)
                        if (
                            (evi_sid is not None and num_sid is not None and evi_sid == num_sid)
                            or eid.startswith(f"{sid}:")
                            or getattr(evi, "story_id", None) == sid
                        ):
                            if (
                                getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                                and getattr(evi, "kind", "") != "resident_question"
                                and eid not in dash_supp_ids
                            ):
                                eligible_supports.append(eid)

                if not eligible_supports:
                    if card.summary and f"{card.id}:summary" not in dash_supp_ids:
                        eligible_supports.append(f"{card.id}:summary")
                    for r in getattr(card, "representative_source_refs", ()):
                        if r not in dash_supp_ids and r not in eligible_supports:
                            eligible_supports.append(r)
                    for hf in getattr(card, "hard_facts", ()):
                        for r in getattr(hf, "source_refs", ()):
                            if r not in dash_supp_ids and r not in eligible_supports:
                                eligible_supports.append(r)
                    for co in getattr(card, "community_observations", ()):
                        for r in getattr(co, "source_refs", ()):
                            if r not in dash_supp_ids and r not in eligible_supports:
                                eligible_supports.append(r)
                    if not eligible_supports and card.id not in dash_supp_ids:
                        eligible_supports.append(card.id)

                # Prioritize supports belonging to required facts for this story
                story_req_facts = [rf for rf in plan_block.required_facts if sid in rf.story_ids]
                for rf in story_req_facts:
                    for s_id in rf.support_ids:
                        if s_id not in dash_supp_ids and s_id not in eligible_supports:
                            eligible_supports.append(s_id)

                if not eligible_supports:
                    raise ValueError(f"no deterministic detail support for {sid}")

                per_story_cap = 2 if len(story_group) == 1 else 1
                chosen_supports = eligible_supports[:per_story_cap]
                # Ensure at least one support for each required fact is in chosen_supports
                for rf in story_req_facts:
                    matching = [s for s in rf.support_ids if s in eligible_supports]
                    if matching and not any(s in chosen_supports for s in matching):
                        chosen_supports.append(matching[0])

                sid_chosen_supports[sid] = []
                sid_support_texts[sid] = []

                for s in chosen_supports:
                    text = ""
                    kind = "established_fact"
                    actual_sup_id = s
                    if s in support_map:
                        text = _sanitize_digest_support_text(support_map[s])
                        if s in evidence:
                            kind = getattr(evidence[s], "kind", "established_fact")
                    elif s in evidence:
                        text = _sanitize_digest_support_text(
                            evidence[s].text or evidence[s].source_text
                        )
                        kind = getattr(evidence[s], "kind", "established_fact")
                    elif s == f"{card.id}:summary" or s in getattr(
                        card, "representative_source_refs", ()
                    ):
                        if card.summary:
                            text = _sanitize_digest_support_text(card.summary)
                        elif card.topic:
                            text = _sanitize_digest_support_text(card.topic)
                    else:
                        for hf in getattr(card, "hard_facts", ()):
                            if s in getattr(hf, "source_refs", ()) or s == getattr(hf, "text", ""):
                                text = _sanitize_digest_support_text(hf.text)
                                kind = "established_fact"
                                break
                        if not text:
                            for co in getattr(card, "community_observations", ()):
                                if s in getattr(co, "source_refs", ()) or s == getattr(
                                    co, "text", ""
                                ):
                                    text = _sanitize_digest_support_text(co.text)
                                    kind = "community_report"
                                    break
                        if not text:
                            if card.summary:
                                text = _sanitize_digest_support_text(card.summary)
                                actual_sup_id = f"{card.id}:summary"
                            elif card.topic:
                                text = _sanitize_digest_support_text(card.topic)
                                actual_sup_id = card.id

                    if not text:
                        for rf in story_req_facts:
                            if s in rf.support_ids and rf.text:
                                text = _sanitize_digest_support_text(rf.text)
                                break

                    if text:
                        group_chosen_supports.append(actual_sup_id)
                        group_support_texts.append(text)
                        sid_chosen_supports[sid].append(actual_sup_id)
                        sid_support_texts[sid].append(text)
                        if kind in {"community_report", "community_observation", "quote_assertion"}:
                            if not text.casefold().startswith(
                                ("по сообщениям", "жители сообщают", "по словам")
                            ):
                                if not group_rendered_sentences:
                                    text = f"По сообщениям жителей, {text[:1].lower() + text[1:]}"
                        group_rendered_sentences.append(text.rstrip(". ") + ".")

            topic = lead_topic
            topic_claims = (
                find_unsupported_claims(
                    topic,
                    group_support_texts,
                    allowed_context_terms=ctx_terms,
                    all_known_draft_supports=known_supports,
                )
                if topic
                else []
            )
            topic_relations = (
                find_unsupported_digest_relations(topic, group_support_texts) if topic else []
            )
            if (
                topic
                and not topic_claims
                and not topic_relations
                and len(topic) <= DIGEST_ITEM_HEADLINE_MAX_CHARS
            ):
                headline = topic
            elif group_rendered_sentences:
                chosen_sent = None
                for sent in group_rendered_sentences:
                    cand = sent.rstrip(". ")
                    if len(cand) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                        truncated = cand[:DIGEST_ITEM_HEADLINE_MAX_CHARS]
                        for sep in [". ", "! ", "? ", "; ", ", ", " — ", " - "]:
                            if sep in truncated:
                                parts = truncated.rsplit(sep, 1)
                                if len(parts[0].strip()) >= 20:
                                    truncated = parts[0].strip()
                                    break
                        else:
                            if " " in truncated:
                                truncated = truncated.rsplit(" ", 1)[0].strip()
                        cand = truncated.rstrip(".:;, ")
                    c_claims = find_unsupported_claims(
                        cand,
                        group_support_texts,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    )
                    c_rels = find_unsupported_digest_relations(cand, group_support_texts)
                    if not c_claims and not c_rels:
                        chosen_sent = cand
                        break
                if not chosen_sent:
                    chosen_sent = (
                        group_rendered_sentences[0]
                        .rstrip(". ")[:DIGEST_ITEM_HEADLINE_MAX_CHARS]
                        .rstrip(".:;, ")
                    )
                headline = chosen_sent
            else:
                headline = (
                    topic[:DIGEST_ITEM_HEADLINE_MAX_CHARS] if topic else story_group[0]
                ).rstrip(".:;, ")

            body_text = " ".join(group_rendered_sentences)
            if len(body_text) > DIGEST_ITEM_BODY_MAX_CHARS:
                body_text = (
                    body_text[:DIGEST_ITEM_BODY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(".:;, ") + "."
                )
            body_text = _deduplicate_digest_attribution(body_text)
            if _DIGEST_ATTRIBUTION_RE.search(body_text):
                headline = _strip_leading_digest_attribution(headline)

            item_claims = []
            for sid in story_group:
                c_sups = sid_chosen_supports.get(sid, [])
                c_texts = sid_support_texts.get(sid, [])
                if not c_sups:
                    continue
                story_req_facts = [rf for rf in plan_block.required_facts if sid in rf.story_ids]
                if story_req_facts:
                    for rf in story_req_facts:
                        best_s = None
                        for s_id in c_sups:
                            if s_id in rf.support_ids:
                                best_s = s_id
                                break
                        if best_s is None:
                            best_s = c_sups[0]
                        best_t = _sanitize_digest_support_text(
                            support_map.get(best_s) or (rf.text if rf.text else "")
                        )
                        clean_c_text = re.sub(r"\bиз-за\b", "при", best_t, flags=re.IGNORECASE)
                        item_claims.append(
                            DigestClaimAtom(
                                text=clean_c_text,
                                covered_story_ids=(sid,),
                                cited_support_ids=(best_s,),
                                covered_fact_ids=(rf.fact_id,),
                            )
                        )
                else:
                    best_s = c_sups[0]
                    best_t = _sanitize_digest_support_text(support_map.get(best_s) or c_texts[0])
                    clean_c_text = re.sub(r"\bиз-за\b", "при", best_t, flags=re.IGNORECASE)
                    item_claims.append(
                        DigestClaimAtom(
                            text=clean_c_text,
                            covered_story_ids=(sid,),
                            cited_support_ids=(best_s,),
                            covered_fact_ids=(),
                        )
                    )

            # Infer emoji if not set
            legacy_item_emoji = ""
            comb_l = f"{headline} {body_text}".casefold()
            if any(w in comb_l for w in ("вод", "водопровод", "водоканал", "водоснабжен")):
                legacy_item_emoji = "💧"
            elif any(
                w in comb_l for w in ("свет", "электрич", "напряжен", "подстанц", "обрыв", "лэп")
            ):
                legacy_item_emoji = "⚡️"
            elif any(w in comb_l for w in ("газ", "газоснабжен", "газопровод")):
                legacy_item_emoji = "💨"
            elif any(w in comb_l for w in ("отоплен", "тепло")):
                legacy_item_emoji = "♨️"
            elif any(w in comb_l for w in ("связь", "интернет", "провайдер", "мобильн")):
                legacy_item_emoji = "🌐"
            elif any(w in comb_l for w in ("транспорт", "автобус", "маршрутк")):
                legacy_item_emoji = "🚌"
            elif any(w in comb_l for w in ("больниц", "поликлиник", "аптек", "врач")):
                legacy_item_emoji = "🏥"
            elif any(w in comb_l for w in ("взрыв", "обстрел", "пожар", "сирен")):
                legacy_item_emoji = "💥"

            item_drafts.append(
                DigestEditorialItemDraft(
                    headline=headline,
                    body=body_text,
                    emoji=legacy_item_emoji,
                    covered_story_ids=tuple(story_group),
                    cited_support_ids=tuple(group_chosen_supports),
                    claims=tuple(item_claims),
                )
            )

        if item_drafts:
            block_drafts.append(
                DigestNarrativeBlockDraft(
                    block_id=plan_block.block_id,
                    items=tuple(item_drafts),
                )
            )

    return DigestNarrativeDraft(blocks=tuple(block_drafts), situation_items=())


def format_digest_date_ru(snapshot_at: dt.datetime) -> str:
    """Format date in Russian: e.g. 04 сентября 2026."""
    months_ru = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{snapshot_at.day:02d} {months_ru[snapshot_at.month - 1]} {snapshot_at.year}"


DIGEST_PROMPT_TEMPLATE = """Вы — старший редактор регионального издания, готовящий ежедневный вечерний Telegram-дайджест города {city} за {date}.

ВАША ГЛАВНАЯ ЦЕЛЬ:
Сформировать структурированный СПИСОК ПУНКТОВ (bulleted list) новостей по рубрикам на основе проверенных городских сообщений за последние 24 часа.
Дайджест — это НЕ связный рассказ, НЕ статья и НЕ сплошной текст! Это удобный, быстрый для сканирования перечень отдельных пунктов, где каждая отдельная новость или событие оформлена своим пунктом с эмодзи в начале.

СТРУКТУРА ДАЙДЖЕСТА:
Дайджест · {date}

[Тематические рубрики дня из доступного списка:
В фокусе внимания
Коммунальная обстановка
Безопасность и чрезвычайные ситуации
Связь и интернет
Транспорт и дороги
Медицина и здоровье
Социальная помощь
Городская среда и бизнес
Полезные контакты
Другое]

ВЫВОДИТЕ ТОЛЬКО те рубрики, по которым сегодня есть реальные новости. Пустые рубрики НЕ выводить!
Рубрику «В фокусе внимания» выводить в самом начале ТОЛЬКО если есть главное резонансное событие дня с прямым влиянием на весь город (масштабная авария, ЧП, крупный инцидент).

ФОРМАТ ЗАГОЛОВКА РУБРИКИ:
- Чистый текст названия рубрики на отдельной строке.
- БЕЗ эмодзи в заголовке рубрики! (Эмодзи ставятся в начале каждого пункта, а не в названии рубрики).
- БЕЗ звездочек ** и символов #.
- БЕЗ разделителей (---, ***, ___). Рубрики и пункты разделяются ТОЛЬКО одной пустой строкой.

ФОРМАТ ПУНКТОВ ВНУТРИ РУБРИКИ:
Каждое отдельное событие, новость или происшествие оформляется КАК ОТДЕЛЬНЫЙ ПУНКТ СПИСКА (абзац), отделенный от соседних пустой строкой.
Каждый пункт начинается с подходящего тематического эмодзи и ОБЯЗАТЕЛЬНОГО жирного заголовка темы:
- Формат с заголовком (основной для всех новостей): [Эмодзи] **Краткий заголовок темы:** [Фактическое раскрытие сути в 1-3 предложениях с конкретикой: улицы, районы, цифры, статус, комментарии служб, решения жителей].
- Формат без заголовка (допустим ТОЛЬКО для кратких оперативных фиксаций звуков стрельбы/взрывов или работы ПВО): [Эмодзи] [Фактическое раскрытие сути происшествия в 1-2 предложениях].

ПРИМЕР СТРУКТУРЫ (ВНИМАНИЕ: ЭТО ТОЛЬКО ШАБЛОН ОФОРМЛЕНИЯ! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО КОПИРОВАТЬ ЭТОТ ТЕКСТ В ДАЙДЖЕСТ):
Дайджест · {date}

Коммунальная обстановка

⚡️ **Критическая ситуация с электроснабжением и низкое напряжение:** [Фактическое раскрытие сути в 1-3 предложениях с конкретикой: улицы, районы, графики, комментарии служб из предоставленных материалов дня].

💧 **Слабое давление в водопроводной сети:** [Фактическое раскрытие сути в 1-3 предложениях из материалов дня].

Безопасность и чрезвычайные ситуации

🔥 **Авария на электроподстанции и пожар:** [Фактическое раскрытие сути происшествия из материалов дня].

🛡 [Краткое оперативное сообщение о звуках взрывов или работе ПВО без заголовка в 1-2 предложениях].

ПРАВИЛА И СТИЛЬ:
1. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ И КОПИРОВАНИЕ ШАБЛОНА:
   - ЗАПРЕЩЕНО выдумывать новости, детали, провайдеров или копировать текст из шаблона структуры.
   - Пишите ИСКЛЮЧИТЕЛЬНО на основе фактов из блока «МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА».
   - Если по какой-то рубрике (например, «Связь и интернет», «Транспорт», «Медицина») в материалах дня нет фактов — такую рубрику выводить КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
2. ПОЛНЫЙ ОХВАТ ЗНАЧИМЫХ СОБЫТИЙ И СИНТЕЗ СВЯЗАННЫХ СООБЩЕНИЙ:
   - Отражайте существенные локальные события, практические изменения и полезные для жителей факты из переданных материалов дня.
   - Связанные сообщения объединяйте в один плотный пункт; одна карточка не обязана становиться отдельным пунктом.
   - Не превращайте дайджест в каталог организаций, магазинов, ветеринаров, маршрутов или контактов. Несколько точек одной услуги или темы объединяйте, оставляя полезную для жителя конкретику.
   - Сохраняйте важные улицы, районы, даты, время и статусы, когда они объясняют событие или практическое последствие, но не переносите справочный и рекламный payload целиком.
   - География фактов: каждое состояние привязывайте к своему указанному району или улице. Сравнивайте доступность услуг под общим названием района только если источники относятся к одной и той же территории. Если в сообщениях названы разные районы, передавайте их отдельными короткими предложениями без общего географического ярлыка; высота местности сама по себе не означает принадлежность к одному району.
3. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА СПЛОШНОЙ ТЕКСТ / «РАССКАЗ»:
   - ЗАПРЕЩЕНО сливать разные события (например, свет, воду, запах газа, безопасность, больницы) в один общий абзац или связный рассказ.
   - Каждая отдельная тема/событие — это ОТДЕЛЬНЫЙ ПУНКТ списка со своим эмодзи.
4. Журналистский стиль и местная топонимика:
   - Чистый, энергичный русский язык хроники. Точный и грамотный перевод сообщений на украинском языке (названия памятников и ориентиров переводить строго на русский язык; «ліхтарі» переводить как «фонари», а не «лихтари»).
   - СТРОГО соблюдайте правила местной микрогеографии и предлогов, указанные в блоке топонимических правил редакции.
5. Сохраняйте микродетали: точные улицы, микрорайоны, графики подачи, номера маршрутов, цены, важные решения жителей.
6. Очистка от рекламы, справочных каталогов и чат-флуда: категорически исключайте коммерческие объявления, полные прайс-листы, телефоны и контактные списки, booking URL, перечни маршрутов, объявления об услугах и бытовой чат-флуд («все живые», пустые реплики). Текущий факт доступности услуги можно сохранить кратко, если он важен для городской жизни. Вопросы жителей не превращайте в утверждения и не оформляйте как новости о том, что жители спрашивают.
7. Разнообразная естественная атрибуция: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО начинать каждое предложение с «По сообщениям жителей...». КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО упоминать источники («в чате Бердянска», «в местных чатах», «в каналах», «в соцсетях»)! Не вскрываем свои источники. Используйте естественные и разнообразные обороты («По словам горожан...», «Очевидцы отмечают...», «Жители сообщают...», «Как отмечают горожане...») либо пишите сразу от сути события.
8. Ограничение длины и редакционный приоритет:
   - Итоговый текст дайджеста должен составлять от 2500 до 3700 знаков, если материала достаточно (жесткий лимит Telegram — 4096 символов).
   - Выводите рубрики со значимыми локальными темами. Приоритет — полезные события и практическая информация, а не механическое включение каждой карточки, точки, контакта или рекламного объявления.{toponym_rules}
9. СТРОГО НЕЙТРАЛЬНАЯ ТЕРМИНОЛОГИЯ И УВАЖИТЕЛЬНЫЙ ТОН:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать конфликтные, политизированные, оценочные или враждебные ярлыки («оккупанты», «оккупационная администрация», «оккупационные власти», «захватчики» и т.п.).
   - Всегда используйте строго нейтральные городские и институциональные формулировки: «городская администрация», «местные власти», «представители администрации», «муниципальные службы» либо пишите в нейтрально-деловом ключе («по официальным сообщениям», «согласно заявлению администрации города»).
10. СОДЕРЖАТЕЛЬНОСТЬ И КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ СОВЕТОВ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать от себя или дописывать назидательные житейские советы читателям («стоит заранее позаботиться о запасах воды», «рекомендуется зарядить пауэрбанки», «следует воздержаться от поездок»). Передавайте ТОЛЬКО факты из материалов дня. Если в исходных сообщениях нет прямого совета или инструкции от служб/жителей — выдумывать советы запрещено.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО публиковать бессодержательные рекомендации без объяснения причин (например, писать «советуют обновить приложение» без указания того, какая именно проблема, сбой или ошибка возникли в старой версии). Если конкретная техническая причина совета в материалах дня не указана — исключайте такой совет.
11. ЗАПРЕТ НА ВЫДУМЫВАНИЕ БУДУЩИХ ОТКЛЮЧЕНИЙ И ДОДУМЫВАНИЕ ДАТ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО интерпретировать обрывочные реплики жителей в чатах (например, «отключение с 9», «без света с первого») как анонсы предстоящих отключений в будущем!
   - Анонсировать будущие отключения (графики, предупреждения) разрешено ТОЛЬКО при наличии официального сообщения коммунальных служб (РЭС, Горсвет, Горгаз, Водоканал) или администрации города.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО додумывать месяц к одиночным цифрам (реплика «с 9» в контексте спора о блэкауте в августе не является отключением 9 сентября!).
12. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВВОДНЫЕ РЕПЛИКИ И ПРЕАМБУЛЫ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ любые вступительные фразы от первого лица или приветствия (например, «Вот ежедневный дайджест...», «Здравствуйте!», «Ниже представлен...»).
   - Вывод должен начинаться СТРОГО со строки заголовка («Дайджест · {date}») либо сразу с названия первой рубрики, без каких-либо вводных слов или мета-комментариев.

МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА:
{content}

ОБЯЗАТЕЛЬНЫЕ ИТОГОВЫЕ ТРЕБОВАНИЯ К ДАЙДЖЕСТУ:
1. Выведите тематические рубрики, в которых есть значимые локальные темы из материалов.
2. Отразите все существенные локальные события и практические факты. Связанные сообщения синтезируйте; не превращайте результат в справочник предприятий, услуг, контактов и маршрутов.
3. СТРОГО соблюдайте правила местной топонимики и названий ориентиров из блока правил редакции выше.
4. Начните вывод СТРОГО с заголовка «Дайджест · {date}» или первой рубрики.
"""

DIGEST_CONDENSE_PROMPT_TEMPLATE = """Вы — выпускающий редактор регионального Telegram-канала города {city}.
Перед вами черновик вечернего дайджеста за {date}, который превышает допустимый лимит одного сообщения Telegram ({current_len} знаков при лимите {max_chars} знаков).

ВАША ЗАДАЧА:
Отредактировать и уплотнить текст так, чтобы его итоговая длина не превышала {target_chars} знаков, сохранив существенные локальные темы, практические факты и полезные микродетали.

ПРАВИЛА РЕДАКТУРЫ И КОМПРЕССИИ:
1. Сохраняйте структуру СПИСКА ПУНКТОВ: дайджест должен оставаться списком отдельных пунктов с эмодзи по рубрикам. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО превращать пункты в сплошной связный рассказ или статью!
2. Сохраняйте существенные события, рубрики и полезные локальные детали, но удаляйте рекламный и справочный payload: каталоги организаций, полные списки магазинов/ветеринаров, телефоны, контакты, booking URL, прайс-листы и перечни маршрутов. Вопросы жителей не превращайте в новости и утверждения.

3. Уплотняйте синтаксис внутри пунктов: убирайте многословие, вводные конструкции («следует отметить, что», «как стало известно из сообщений»), пространные рассуждения и повторы.
4. Сохраняйте ВСЕ микродетали: названия улиц, номера домов, время, цены, имена, учреждения, номера статей КоАП, марки генераторов.
5. Объединяйте сложноподчиненные предложения в краткие, энергичные фразы.
   - Сообщения из разных районов оставляйте в отдельных коротких фразах. Не называйте их одним районом и не превращайте физическую высоту местности в общий географический ярлык.
6. Сохраните формат Telegram: чистые названия рубрик (без эмодзи в заголовке, без ** и ##), разделение ТОЛЬКО пустой строкой. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ разделители (---, ***) и лишняя разметка (**, ##).
7. Верните ТОЛЬКО готовый отредактированный текст без вступительных или заключительных реплик.
8. СТРОГО НЕЙТРАЛЬНАЯ ТЕРМИНОЛОГИЯ: используйте только нейтральные формулировки органов власти («городская администрация», «местные власти»), категорически исключая конфликтные или враждебные ярлыки («оккупанты» и т.п.).
9. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ДОБАВЛЕНИЕ СОВЕТОВ: запрещено добавлять от себя назидательные советы или житейские рекомендации читателям («стоит позаботиться...», «рекомендуется запастись...»).

ЧЕРНОВИК ДАЙДЖЕСТА ДЛЯ КОМПРЕССИИ:
{draft_text}
"""


def _clean_markdown_fence(text: str | None) -> str:
    if not text:
        return ""
    clean = text.strip()
    if clean.startswith("```markdown"):
        clean = clean[len("```markdown") :].strip()
    elif clean.startswith("```"):
        clean = clean[3:].strip()
    if clean.endswith("```"):
        clean = clean[:-3].strip()
    return clean


_TERMINOLOGY_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Prepositional/adverbial phrases with "по данным" / "сообщили в"
    (
        re.compile(
            r"\bпо\s+данным\s+оккупационн(?:ой|ых)\s+(?:администрации|властей)\b",
            re.IGNORECASE,
        ),
        "по данным городской администрации",
    ),
    (
        re.compile(
            r"\bсообщили\s+в\s+оккупационн(?:ой|ых)\s+(?:администрации|властях)\b",
            re.IGNORECASE,
        ),
        "сообщили в городской администрации",
    ),
    (
        re.compile(
            r"\bзаявили\s+в\s+оккупационн(?:ой|ых)\s+(?:администрации|властях)\b",
            re.IGNORECASE,
        ),
        "заявили в городской администрации",
    ),
    # Adjective + noun: администрация
    (
        re.compile(r"\bоккупационн(?:ая)\s+администраци(?:я)\b", re.IGNORECASE),
        "городская администрация",
    ),
    (
        re.compile(r"\bоккупационн(?:ой|ою)\s+администраци(?:ей|ею)\b", re.IGNORECASE),
        "городской администрацией",
    ),
    (
        re.compile(r"\bоккупационн(?:ой)\s+администраци(?:и)\b", re.IGNORECASE),
        "городской администрации",
    ),
    (
        re.compile(r"\bоккупационн(?:ую)\s+администраци(?:ю)\b", re.IGNORECASE),
        "городскую администрацию",
    ),
    # Adjective + noun: власти
    (re.compile(r"\bоккупационн(?:ые)\s+власт(?:и)\b", re.IGNORECASE), "местные власти"),
    (re.compile(r"\bоккупационн(?:ых)\s+власт(?:ей)\b", re.IGNORECASE), "местных властей"),
    (re.compile(r"\bоккупационн(?:ым)\s+власт(?:ям)\b", re.IGNORECASE), "местным властям"),
    (re.compile(r"\bоккупационн(?:ыми)\s+власт(?:ями)\b", re.IGNORECASE), "местными властями"),
    (re.compile(r"\bоккупационн(?:ых)\s+власт(?:ях)\b", re.IGNORECASE), "местных властях"),
    # Adjective + noun: структуры / службы
    (
        re.compile(r"\bоккупационн(?:ые)\s+(?:структуры|службы)\b", re.IGNORECASE),
        "городские службы",
    ),
    (re.compile(r"\bоккупационн(?:ых)\s+(?:структур|служб)\b", re.IGNORECASE), "городских служб"),
    (
        re.compile(r"\bоккупационн(?:ым)\s+(?:структурам|службам)\b", re.IGNORECASE),
        "городским службам",
    ),
    (
        re.compile(r"\bоккупационн(?:ыми)\s+(?:структурами|службами)\b", re.IGNORECASE),
        "городскими службами",
    ),
    # Adjective + noun: комендатура
    (
        re.compile(r"\bоккупационн(?:ая)\s+комендатур(?:а)\b", re.IGNORECASE),
        "городская комендатура",
    ),
    (
        re.compile(r"\bоккупационн(?:ой)\s+комендатур(?:е|ы|ой)\b", re.IGNORECASE),
        "городской комендатуре",
    ),
    # Adjective + noun: режим
    (
        re.compile(r"\bоккупационн(?:ый|ого|ому|ым|ом)\s+режим(?:а|у|ом|е)?\b", re.IGNORECASE),
        "городская администрация",
    ),
    # Noun standalone: оккупанты / захватчики
    (re.compile(r"\b(?:оккупант(?:ы)|захватчик(?:и))\b", re.IGNORECASE), "местные власти"),
    (re.compile(r"\b(?:оккупант(?:ов)|захватчик(?:ов))\b", re.IGNORECASE), "местных властей"),
    (re.compile(r"\b(?:оккупант(?:ам)|захватчик(?:ам))\b", re.IGNORECASE), "местным властям"),
    (re.compile(r"\b(?:оккупант(?:ами)|захватчик(?:ами))\b", re.IGNORECASE), "местными властями"),
    (re.compile(r"\b(?:оккупант(?:ах)|захватчик(?:ах))\b", re.IGNORECASE), "местных властях"),
    (
        re.compile(r"\b(?:оккупант(?:а)|захватчик(?:а))\b", re.IGNORECASE),
        "представителя администрации",
    ),
    # City with occupation adjective
    (
        re.compile(r"\bв\s+оккупированн(?:ом|ым)\s+Бердянск(?:е)?\b", re.IGNORECASE),
        "в Бердянске",
    ),
    (
        re.compile(r"\bоккупированн(?:ый|ого|ому|ым|ом)\s+Бердянск(?:а|у|ом|е)?\b", re.IGNORECASE),
        "Бердянск",
    ),
)


def sanitize_digest_terminology(text: str) -> str:
    """Normalize politically hostile/offensive labels to neutral municipal formulations."""
    if not text:
        return ""

    res = text
    for pattern, repl in _TERMINOLOGY_REPLACEMENTS:

        def _sub(match: re.Match[str], replacement: str = repl) -> str:
            orig = match.group(0)
            if orig and orig[0].isupper():
                return replacement[0].upper() + replacement[1:]
            return replacement[0].lower() + replacement[1:]

        res = pattern.sub(_sub, res)
    return res


def enforce_telegram_single_message_limit(text: str, max_chars: int = 3900) -> str:
    """Deterministic fallback: trims text along structural paragraph boundaries if still over limit."""
    if len(text) <= max_chars:
        return text
    paragraphs = text.split("\n\n")
    acc: list[str] = []
    cur_len = 0
    for p in paragraphs:
        added_len = len(p) + (2 if acc else 0)
        if cur_len + added_len <= max_chars:
            acc.append(p)
            cur_len += added_len
        else:
            break
    res = "\n\n".join(acc).strip()
    res_lines = res.splitlines()
    if res_lines and not any(
        sym in res_lines[-1]
        for sym in (
            "*",
            "•",
            "⚡",
            "💧",
            "💥",
            "🔥",
            "🚌",
            "🌐",
            "🏥",
            "🏧",
            "🏛",
            "📦",
            "💼",
            "📌",
            ":",
        )
    ):
        res = "\n".join(res_lines[:-1]).strip()
    return res


def parse_journalistic_markdown_to_draft(
    markdown_text: str,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence] | None = None,
    custom_rubrics: Sequence[Any] | None = None,
) -> DigestNarrativeDraft:
    """Parse raw journalistic Telegram Markdown text into a structured DigestNarrativeDraft."""
    lines = markdown_text.splitlines()
    blocks: list[DigestNarrativeBlockDraft] = []
    current_items: list[DigestEditorialItemDraft] = []
    current_rubric_id = "general"
    block_counter = 0

    card_tokens: dict[str, set[str]] = {}
    for c in cards:
        toks = set(re.findall(r"[\w-]+", (c.topic or "").lower()))
        for f in getattr(c, "hard_facts", ()):
            toks |= set(re.findall(r"[\w-]+", (f.text or "").lower()))
        for o in getattr(c, "community_observations", ()):
            toks |= set(re.findall(r"[\w-]+", (o.text or "").lower()))
        card_tokens[c.id] = {t for t in toks if len(t) >= 3}

    story_supports: dict[str, list[str]] = {}
    if evidence:
        for evid, ev in evidence.items():
            sid = (
                f"story:{ev.story_id}"
                if not str(ev.story_id).startswith("story:")
                else str(ev.story_id)
            )
            story_supports.setdefault(sid, []).append(evid)

    def _flush() -> None:
        nonlocal block_counter, current_items
        if current_items:
            blocks.append(
                DigestNarrativeBlockDraft(
                    block_id=f"block:{current_rubric_id}:{block_counter}",
                    items=tuple(current_items),
                )
            )
            block_counter += 1
            current_items = []

    _EMOJI_PATTERN = r"[\U00010000-\U0010ffff\u200d\u2300-\u27bf\ufe0f]"
    re_leading_marker = re.compile(rf"^(?:[•\-\*]\s*)?(?:(?:{_EMOJI_PATTERN})+\s*)?")
    re_bold_headline = re.compile(r"^\*\*([^*]+)\*\*[:.]?\s*(.*)$")

    dynamic_rubrics: list[tuple[str, str, set[str]]] = []
    dynamic_keywords: list[str] = []
    if custom_rubrics:
        for cr in custom_rubrics:
            r_name = getattr(cr, "name", None) or (
                cr.get("name") if isinstance(cr, dict) else str(cr)
            )
            r_id = (
                getattr(cr, "id", None)
                or (cr.get("id") if isinstance(cr, dict) else None)
                or getattr(cr, "key", None)
                or (cr.get("key") if isinstance(cr, dict) else None)
            )
            clean_name = re.sub(r"[^\w\s-]", "", str(r_name)).strip().lower()
            if not r_id:
                slug = re.sub(r"[^\w]+", "_", clean_name).strip("_")
                r_id = slug or "custom"
            words = {w for w in clean_name.split() if len(w) >= 3}
            if clean_name:
                dynamic_rubrics.append((str(r_id), clean_name, words))
                dynamic_keywords.extend(words)

    rubric_keywords = (
        "коммунальн",
        "инфраструктур",
        "жкх",
        "электроснабжен",
        "водоснабжен",
        "безопасн",
        "тревог",
        "чп",
        "обстрел",
        "пво",
        "сирен",
        "взрыв",
        "социальн",
        "выплат",
        "пенси",
        "пособи",
        "связь",
        "интернет",
        "транспорт",
        "дорог",
        "медицин",
        "здоров",
        "больниц",
        "аптек",
        "образов",
        "школ",
        "культур",
        "городск",
        "сред",
        "благоустрой",
        "быт",
        "другое",
        "в фокусе",
        "фокус",
        "полезные контакты",
        "контакт",
        *dynamic_keywords,
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        clean_hdr = re.sub(r"[^\w\s-]", "", stripped).lower()
        if clean_hdr.startswith(("дайджест", "дайдджест")):
            continue

        is_bullet = stripped.startswith(("•", "-", "*•", "* -"))
        is_header = not is_bullet and (
            stripped.startswith(("##", "###"))
            or (
                any(k in clean_hdr for k in rubric_keywords)
                and len(stripped) <= 60
                and not (":" in stripped and len(stripped) > 35)
            )
        )

        if is_header:
            _flush()
            matched_custom = False
            for r_id, clean_name, words in dynamic_rubrics:
                if (
                    clean_name in clean_hdr
                    or clean_hdr in clean_name
                    or (words and words.issubset(set(clean_hdr.split())))
                ):
                    current_rubric_id = r_id
                    matched_custom = True
                    break
            if not matched_custom:
                if any(k in clean_hdr for k in ("в фокусе", "фокус")):
                    current_rubric_id = "focus"
                elif any(k in clean_hdr for k in ("жкх", "коммун", "электр", "вода", "энерг")):
                    current_rubric_id = "infrastructure"
                elif any(
                    k in clean_hdr
                    for k in ("безопасн", "тревог", "чп", "обстрел", "пво", "сирен", "взрыв")
                ):
                    current_rubric_id = "safety"
                elif any(k in clean_hdr for k in ("социальн", "выплат", "пенси", "пособи")):
                    current_rubric_id = "social"
                elif any(k in clean_hdr for k in ("транспорт", "дорог")):
                    current_rubric_id = "transport"
                elif any(k in clean_hdr for k in ("связь", "интернет")):
                    current_rubric_id = "communications"
                elif any(k in clean_hdr for k in ("медицин", "здоров", "больниц", "аптек")):
                    current_rubric_id = "health"
                elif any(k in clean_hdr for k in ("образов", "школ", "культур")):
                    current_rubric_id = "education"
                elif any(
                    k in clean_hdr
                    for k in ("город", "сред", "благоустрой", "быт", "торгов", "бизнес")
                ):
                    current_rubric_id = "urban_life"
                elif any(k in clean_hdr for k in ("контакт", "служеб")):
                    current_rubric_id = "contacts"
                elif any(k in clean_hdr for k in ("друг", "проч", "остальн", "разн")):
                    current_rubric_id = "general"
                else:
                    slug = re.sub(r"\s+", "_", clean_hdr[:30]).strip("_")
                    current_rubric_id = slug if slug else "general"
            continue

        m_marker = re_leading_marker.match(stripped)
        marker_emoji = m_marker.group(0).strip() if m_marker else ""
        rest = stripped[m_marker.end() :].strip() if m_marker else stripped
        rest = rest.lstrip("\ufe0f").strip()

        headline = ""
        body = ""
        m_bold = re_bold_headline.match(rest)
        if m_bold:
            headline = m_bold.group(1).strip().rstrip(".:;, ")
            body = m_bold.group(2).strip()
        elif (
            ":" in rest
            and len(rest.split(":", 1)[0]) <= 80
            and not rest.split(":", 1)[0].startswith("http")
        ):
            h, b = rest.split(":", 1)
            headline = h.strip().rstrip(".:;, ")
            body = b.strip()
        else:
            parts = re.split(r"[.!?]\s+", rest, maxsplit=1)
            headline = parts[0][:60].strip().rstrip(".:;, ")
            body = rest

        if headline or body:
            if not headline:
                headline = body[:50].strip()
            if not body:
                body = headline

            item_toks = {
                t for t in re.findall(r"[\w-]+", (headline + " " + body).lower()) if len(t) >= 3
            }
            scored = sorted(
                [
                    (len(item_toks & ctoks), cid)
                    for cid, ctoks in card_tokens.items()
                    if (item_toks & ctoks)
                ],
                reverse=True,
            )
            covered_sids = [cid for _, cid in scored[:4]] or ([cards[0].id] if cards else [])
            cited_sups: list[str] = []
            for cid in covered_sids:
                cited_sups.extend(story_supports.get(cid, []))
            current_items.append(
                DigestEditorialItemDraft(
                    headline=headline,
                    body=body,
                    covered_story_ids=tuple(covered_sids),
                    cited_support_ids=tuple(cited_sups[:5]),
                    emoji=marker_emoji,
                )
            )
    _flush()
    return DigestNarrativeDraft(blocks=tuple(blocks), situation_items=())


def _publish_support_texts(
    evidence: Mapping[str, PublicationEvidence],
) -> dict[str, tuple[str, ...]]:
    """Index only exact PUBLISH evidence references for canonical writer input."""
    output: dict[str, list[str]] = {}
    for evidence_id, item in evidence.items():
        if str(getattr(item, "publication_use", "")) != "PUBLISH":
            continue
        text = str(getattr(item, "text", "") or getattr(item, "source_text", "") or "").strip()
        if not text:
            continue
        refs = {str(evidence_id)}
        for name in ("evidence_id", "source_ref"):
            value = str(getattr(item, name, "") or "").strip()
            if value:
                refs.add(value)
        fragment_id = str(getattr(item, "fragment_id", "") or "").strip()
        if fragment_id:
            refs.add(f"fragment:{fragment_id}")
        for ref in refs:
            values = output.setdefault(ref, [])
            if text not in values:
                values.append(text)
    return {ref: tuple(values) for ref, values in output.items()}


def _composition_writer_payload(
    *,
    plan: DigestNarrativePlan,
    evidence: Mapping[str, PublicationEvidence],
    cards: Sequence[StoryCard],
) -> list[dict[str, Any]]:
    """Build canonical input without introducing evidence outside frozen units."""
    publish_texts = _publish_support_texts(evidence)
    cards_by_id = {str(getattr(card, "id", "")): card for card in cards}
    payload: list[dict[str, Any]] = []
    for block in plan.blocks:
        unit_rows: list[dict[str, Any]] = []
        record_by_fact = {
            str(getattr(record, "fact_id", "")): record for record in block.composition_fact_records
        }
        fact_by_id = {str(getattr(fact, "fact_id", "")): fact for fact in block.required_facts}
        for unit in block.composition_units:
            unit_id = str(unit.unit_id)
            fact_ids = tuple(str(fid) for fid in unit.fact_ids)
            support_rows: list[dict[str, Any]] = []
            for support_id in unit.support_ids:
                support_id = str(support_id)
                texts = publish_texts.get(support_id, ())
                if not texts and not fact_ids:
                    # Summary-only units may cite the exact card summary when
                    # its stable summary ref is explicitly in the frozen unit.
                    for story_id in unit.story_ids:
                        card = cards_by_id.get(str(story_id))
                        if card and support_id == f"{story_id}:summary" and card.summary:
                            texts = (str(card.summary),)
                            break
                        if card and support_id == f"{story_id}:topic" and card.topic:
                            texts = (str(card.topic),)
                            break
                if not texts:
                    raise DigestCoverageInvariantError(
                        f"DIGEST_COMPOSITION_SUPPORT_TEXT_MISSING:{unit_id}:{support_id}"
                    )
                evidence_item = evidence.get(support_id)
                support_rows.append(
                    {
                        "support_id": support_id,
                        "texts": list(texts),
                        "evidence_kind": str(getattr(evidence_item, "kind", "")),
                        "source_role": str(getattr(evidence_item, "source_role", "")),
                    }
                )

            facts_payload: list[dict[str, Any]] = []
            for fact_id in fact_ids:
                record = record_by_fact.get(fact_id)
                fact = fact_by_id.get(fact_id)
                if record is None or fact is None:
                    raise DigestCoverageInvariantError(
                        f"DIGEST_COMPOSITION_FACT_CONTEXT_MISSING:{unit_id}:{fact_id}"
                    )
                fact_support_ids = tuple(str(sid) for sid in record.support_ids)
                if not fact_support_ids or not set(fact_support_ids).issubset(
                    set(unit.support_ids)
                ):
                    raise DigestCoverageInvariantError(
                        f"DIGEST_COMPOSITION_FACT_SUPPORT_MISMATCH:{unit_id}:{fact_id}"
                    )
                facts_payload.append(
                    {
                        "fact_id": fact_id,
                        "text": str(fact.text),
                        "story_ids": list(record.story_ids),
                        "support_ids": list(fact_support_ids),
                        "original_location": str(getattr(record, "original_location", "")),
                        "canonical_area": str(getattr(record, "canonical_area", "")),
                        "canonical_place": list(getattr(record, "canonical_place", ()) or ()),
                        "effective_time": record.effective_time.isoformat()
                        if record.effective_time
                        else None,
                        "observed_time": record.observed_time.isoformat()
                        if record.observed_time
                        else None,
                        "service_state": str(record.service_state),
                        "epistemic_kind": str(record.epistemic_kind),
                        "source_publication_time": (
                            record.source_publication_time.isoformat()
                            if record.source_publication_time
                            else None
                        ),
                    }
                )
            unit_rows.append(
                {
                    "composition_unit_id": unit_id,
                    "allowed_fact_ids": list(fact_ids),
                    "summary_only_story_ids": list(unit.story_ids) if not fact_ids else [],
                    "facts": facts_payload,
                    "supports": support_rows,
                    "allowed_same_fact_relations": [
                        {
                            "merge_id": _same_fact_merge_id(
                                relation.left_fact_id, relation.right_fact_id
                            ),
                            "left_fact_id": relation.left_fact_id,
                            "right_fact_id": relation.right_fact_id,
                        }
                        for relation in unit.allowed_relations
                        if str(getattr(relation.kind, "value", relation.kind)) == "SAME_FACT"
                    ],
                }
            )
        payload.append(
            {
                "block_id": block.block_id,
                "rubric_id": block.rubric_id,
                "rubric_title": block.rubric_title,
                "composition_units": unit_rows,
            }
        )
    return payload


def _same_fact_merge_id(left_fact_id: str, right_fact_id: str) -> str:
    """Return a stable opaque ID for one unordered SAME_FACT relation."""
    endpoints = sorted((str(left_fact_id), str(right_fact_id)))
    digest = hashlib.sha256((endpoints[0] + "\0" + endpoints[1]).encode()).hexdigest()[:20]
    return f"same_fact:{digest}"


def _same_fact_group_merge_id(source_item_ids: Sequence[str], relation_ids: Sequence[str]) -> str:
    """Return an auditable stable authorization ID for a connected SAME_FACT graph."""
    items = "|".join(sorted(str(item_id) for item_id in source_item_ids))
    relations = "|".join(sorted(str(relation_id) for relation_id in relation_ids))
    digest = hashlib.sha256((items + "\0" + relations).encode()).hexdigest()[:20]
    return f"same_fact_group:{digest}"


def _parse_composition_writer_output(
    parsed: Any,
    *,
    plan: DigestNarrativePlan,
) -> DigestNarrativeDraft:
    """Fail closed on absent, unknown, duplicated, or widened composition membership."""
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("blocks"), list):
        raise ValueError("composition draft must contain a blocks list")
    raw_blocks = parsed["blocks"]
    expected_block_ids = [block.block_id for block in plan.blocks]
    received_block_ids = [
        str(block.get("block_id", "")).strip() if isinstance(block, Mapping) else ""
        for block in raw_blocks
    ]
    if received_block_ids != expected_block_ids:
        raise ValueError(
            f"composition block set/order mismatch: expected {expected_block_ids}, got {received_block_ids}"
        )

    blocks: list[DigestNarrativeBlockDraft] = []
    global_item_ids: set[str] = set()
    for block, raw_block in zip(plan.blocks, raw_blocks, strict=True):
        if not isinstance(raw_block, Mapping) or not isinstance(raw_block.get("items"), list):
            raise ValueError(f"composition block {block.block_id} must contain items")
        units = {str(unit.unit_id): unit for unit in block.composition_units}
        fact_records = {str(record.fact_id): record for record in block.composition_fact_records}
        fact_to_unit = {
            str(fact_id): str(unit.unit_id)
            for unit in block.composition_units
            for fact_id in unit.fact_ids
        }
        used_facts_by_unit: dict[str, list[str]] = {unit_id: [] for unit_id in units}
        used_summary_by_unit: dict[str, int] = dict.fromkeys(units, 0)
        items: list[DigestEditorialItemDraft] = []
        for item_index, raw_item in enumerate(raw_block["items"]):
            if not isinstance(raw_item, Mapping):
                raise ValueError(f"{block.block_id}.items[{item_index}] must be an object")
            raw_unit_ids = raw_item.get("composition_unit_ids")
            if not isinstance(raw_unit_ids, list) or not raw_unit_ids:
                raise ValueError("composition item must name one or more composition_unit_ids")
            unit_ids = tuple(str(unit_id).strip() for unit_id in raw_unit_ids)
            if len(unit_ids) != len(set(unit_ids)):
                raise ValueError("composition item contains duplicate unit IDs")
            item_units = [units.get(unit_id) for unit_id in unit_ids]
            if any(unit is None for unit in item_units):
                raise ValueError(f"unknown composition_unit_id in {block.block_id}: {unit_ids!r}")
            if any(
                str(unit.rubric_id) != block.rubric_id for unit in item_units if unit is not None
            ):
                raise ValueError("composition items may combine only units from the same rubric")
            if "covered_story_ids" in raw_item or "cited_support_ids" in raw_item:
                raise ValueError(
                    "writer may not author Story or support membership on composition path"
                )
            raw_fact_ids = raw_item.get("covered_fact_ids")
            if not isinstance(raw_fact_ids, list):
                raise ValueError("composition item covered_fact_ids must be a list")
            fact_ids = tuple(str(fid).strip() for fid in raw_fact_ids)
            if len(fact_ids) != len(set(fact_ids)):
                raise ValueError(f"duplicate item fact membership in {unit_ids}")
            if any(fid not in fact_to_unit for fid in fact_ids):
                raise ValueError(f"unknown fact ID in composition item {unit_ids}")
            item_fact_owner_ids = {fact_to_unit[fid] for fid in fact_ids}
            item_summary_unit_ids = {unit_id for unit_id in unit_ids if not units[unit_id].fact_ids}
            exact_item_unit_ids = item_fact_owner_ids | item_summary_unit_ids
            if set(unit_ids) != exact_item_unit_ids:
                raise ValueError(
                    f"composition unit IDs do not match item facts/summary membership: {unit_ids}"
                )
            for fact_id in fact_ids:
                used_facts_by_unit[fact_to_unit[fact_id]].append(fact_id)
            for unit_id in item_summary_unit_ids:
                used_summary_by_unit[unit_id] += 1
                if used_summary_by_unit[unit_id] > 1:
                    raise ValueError(f"summary-only unit {unit_id} is represented more than once")

            stories_for_item: list[str] = []
            supports_for_item: list[str] = []
            claims_raw = raw_item.get("claims")
            if not isinstance(claims_raw, list) or not claims_raw:
                raise ValueError(f"composition item {unit_ids} must contain claims")
            claims: list[DigestClaimAtom] = []
            claim_fact_ids: list[str] = []
            claim_summary_unit_ids: list[str] = []
            for raw_claim in claims_raw:
                if (
                    not isinstance(raw_claim, Mapping)
                    or "cited_support_ids" in raw_claim
                    or "covered_story_ids" in raw_claim
                ):
                    raise ValueError(
                        "writer may not author claim Story/support membership on composition path"
                    )
                text = str(raw_claim.get("text", "")).strip()
                if not text:
                    raise ValueError(f"empty composition claim in units {unit_ids}")
                raw_claim_facts = raw_claim.get("covered_fact_ids")
                if not isinstance(raw_claim_facts, list):
                    raise ValueError("composition claim covered_fact_ids must be a list")
                claim_facts = tuple(str(fid).strip() for fid in raw_claim_facts)
                raw_claim_summary_units = raw_claim.get("summary_unit_ids", [])
                if not isinstance(raw_claim_summary_units, list):
                    raise ValueError("composition claim summary_unit_ids must be a list")
                claim_summary_units = tuple(str(uid).strip() for uid in raw_claim_summary_units)
                if len(claim_facts) != len(set(claim_facts)) or not set(claim_facts).issubset(
                    set(fact_ids)
                ):
                    raise ValueError(f"claim fact membership outside item {unit_ids}")
                if len(claim_summary_units) != len(set(claim_summary_units)) or not set(
                    claim_summary_units
                ).issubset(item_summary_unit_ids):
                    raise ValueError(f"claim summary membership outside item {unit_ids}")
                if bool(claim_facts) == bool(claim_summary_units):
                    raise ValueError(
                        "each composition claim must cover facts OR summary units, not neither/both"
                    )
                claim_stories: list[str] = []
                claim_supports: list[str] = []
                if claim_facts:
                    for fact_id in claim_facts:
                        record = fact_records.get(fact_id)
                        if record is None:
                            raise ValueError(f"missing provenance record for {fact_id}")
                        claim_stories.extend(str(sid) for sid in record.story_ids)
                        claim_supports.extend(str(sid) for sid in record.support_ids)
                else:
                    for summary_unit_id in claim_summary_units:
                        unit = units[summary_unit_id]
                        claim_stories.extend(str(sid) for sid in unit.story_ids)
                        claim_supports.extend(str(sid) for sid in unit.support_ids)
                claim_story_ids = tuple(dict.fromkeys(claim_stories))
                claim_support_ids = tuple(dict.fromkeys(claim_supports))
                if not claim_story_ids or not claim_support_ids:
                    raise ValueError(f"claim in {unit_ids} has no derived provenance")
                claims.append(
                    DigestClaimAtom(
                        text=text,
                        covered_story_ids=claim_story_ids,
                        cited_support_ids=claim_support_ids,
                        covered_fact_ids=claim_facts,
                        summary_unit_ids=claim_summary_units,
                    )
                )
                claim_fact_ids.extend(claim_facts)
                claim_summary_unit_ids.extend(claim_summary_units)
                stories_for_item.extend(claim_story_ids)
                supports_for_item.extend(claim_support_ids)
            if len(claim_fact_ids) != len(set(claim_fact_ids)) or set(claim_fact_ids) != set(
                fact_ids
            ):
                raise ValueError(f"claims do not exactly partition item facts in {unit_ids}")
            if (
                len(claim_summary_unit_ids) != len(set(claim_summary_unit_ids))
                or set(claim_summary_unit_ids) != item_summary_unit_ids
            ):
                raise ValueError(f"claims do not exactly partition summary units in {unit_ids}")
            story_ids = tuple(dict.fromkeys(stories_for_item))
            support_ids = tuple(dict.fromkeys(supports_for_item))
            allowed_story_ids = {
                str(sid) for unit_id in unit_ids for sid in units[unit_id].story_ids
            }
            allowed_support_ids = {
                str(sid) for unit_id in unit_ids for sid in units[unit_id].support_ids
            }
            if not set(story_ids).issubset(allowed_story_ids):
                raise ValueError(f"derived Stories outside composition units {unit_ids}")
            if not set(support_ids).issubset(allowed_support_ids):
                raise ValueError(f"derived supports outside composition units {unit_ids}")
            stable_members = "|".join(
                (*sorted(unit_ids), *sorted(fact_ids), *sorted(item_summary_unit_ids))
            )
            item_hash = hashlib.sha256(stable_members.encode()).hexdigest()[:16]
            item_id = f"item:{item_hash}"
            if item_id in global_item_ids:
                raise ValueError(f"duplicate stable item ID: {item_id}")
            global_item_ids.add(item_id)
            item_data = {
                "item_id": item_id,
                "composition_unit_ids": list(unit_ids),
                "covered_fact_ids": list(fact_ids),
                "headline": raw_item.get("headline", ""),
                "body": raw_item.get("body", ""),
                "emoji": raw_item.get("emoji", ""),
                "covered_story_ids": list(story_ids),
                "cited_support_ids": list(support_ids),
                "claims": [
                    {
                        "text": claim.text,
                        "covered_story_ids": list(claim.covered_story_ids),
                        "cited_support_ids": list(claim.cited_support_ids),
                        "covered_fact_ids": list(claim.covered_fact_ids),
                        "summary_unit_ids": list(claim.summary_unit_ids),
                    }
                    for claim in claims
                ],
            }
            items.append(DigestEditorialItemDraft.from_dict(item_data))

        for unit_id, unit in units.items():
            if unit.fact_ids:
                expected = {str(fid) for fid in unit.fact_ids}
                actual_list = used_facts_by_unit[unit_id]
                if len(actual_list) != len(set(actual_list)) or set(actual_list) != expected:
                    raise ValueError(
                        f"composition unit fact partition mismatch for {unit_id}: "
                        f"missing={sorted(expected - set(actual_list))}"
                    )
            elif used_summary_by_unit[unit_id] != 1:
                raise ValueError(f"summary-only composition unit missing item: {unit_id}")
        blocks.append(DigestNarrativeBlockDraft(block_id=block.block_id, items=tuple(items)))
    return DigestNarrativeDraft(blocks=tuple(blocks), situation_items=())


class DigestNarrativeWriter:
    """Single-call narrative digest writer synthesizing flowing prose across rubric blocks."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def _generate_composition_draft(
        self,
        *,
        plan: DigestNarrativePlan,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence],
        language: str,
        max_output_tokens: int,
        model: str | None,
    ) -> DigestNarrativeDraft:
        """Write only against frozen composition-unit membership."""
        import json

        from src.publication.narrative_contract import build_digest_narrative_contract

        blocks_payload = _composition_writer_payload(
            plan=plan,
            evidence=evidence,
            cards=cards,
        )
        schema_desc = (
            '{"blocks":[{"block_id":"exact input block_id","items":[{'
            '"composition_unit_ids":["one or more exact unit IDs from this same-rubric block"],'
            '"covered_fact_ids":["exact facts this item covers; empty only when all named units are summary-only"],'
            '"emoji":"optional short semantic emoji",'
            '"headline":"short specific reader headline",'
            '"body":"cohesive concise prose",'
            '"claims":[{"text":"one grounded proposition",'
            '"covered_fact_ids":["exact fact IDs supporting this claim"],'
            '"summary_unit_ids":["for a summary-only claim, exact summary-only unit IDs it represents"]}]'
            "}]}]}"
        )
        system_prompt = (
            f"You are a careful local-news editor writing a scan-first digest in {language}.\n"
            "Write fluent, natural prose from the supplied frozen composition plan.\n"
            "COMPOSITION CONTRACT:\n"
            "- Every item names one or more exact composition_unit_ids from this block; units in an item must belong to this same rubric. Do not invent, shorten, or infer IDs. The units define which material an item may represent; they do not require one visible item each.\n"
            "- Across the whole block, every allowed fact ID must occur in exactly one item's covered_fact_ids. Items may weave compatible same-rubric units together or split a unit when that makes its places or situations clearer. No fact may be omitted, duplicated, or moved outside its unit.\n"
            "- Every summary-only unit must appear in exactly one item's composition_unit_ids and exactly one claim's summary_unit_ids. Summary-only units may be woven together when that reads naturally; keep each distinct report recognizable.\n"
            "- Every factual claim must list the exact fact ID or IDs it expresses. Claims must partition each item's facts exactly once. A summary-only claim lists no covered_fact_ids and names its exact summary_unit_ids. Do not write factual claims outside listed facts.\n"
            "- Do not output covered_story_ids or cited_support_ids. Python derives both from the frozen fact-to-evidence map.\n"
            "- Use only facts and PUBLISH supports provided for that unit. A single PUBLISH community report is publishable: preserve its reported/uncertain status with natural attribution; do not demand corroboration or official confirmation. Never upgrade it to an established or official fact.\n"
            "- Keep a concrete local location attached to its own fact. Do not infer proximity, a shared district, a cause, or a city-wide condition. When one item weaves units from different locations, mention each named place in its own clause or sentence; a shared rubric is not a shared neighborhood. Use only explicitly supported localized contrasts.\n"
            "- Avoid chat/forum language, filler, generic status phrases, advice, and invented context. Use a specific headline and readable body; do not repeat the headline verbatim.\n\n"
            f"{build_digest_narrative_contract(output_language=language)}\n\n"
            "Return only valid JSON matching this schema; include every input block in the same order:\n"
            f"{schema_desc}"
        )
        user_prompt = json.dumps({"blocks": blocks_payload}, ensure_ascii=False, indent=2)
        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "reasoning_effort": "none",
            "thinking": False,
            "max_tokens": max_output_tokens or 4096,
        }
        if model:
            chat_kwargs["model"] = model
        raw_response = await self._provider.chat_completion(**chat_kwargs)
        cleaned = (raw_response or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        first_brace, last_brace = cleaned.find("{"), cleaned.rfind("}")
        if first_brace < 0 or last_brace <= first_brace:
            raise ValueError("composition writer response did not contain a JSON object")
        parsed = json.loads(cleaned[first_brace : last_brace + 1])
        return _parse_composition_writer_output(parsed, plan=plan)

    async def generate_journalistic_digest(
        self,
        *,
        city: str,
        date_str: str,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence] | None = None,
        custom_rubrics: Sequence[Any] | None = None,
        model: str | None = None,
        max_chars: int = 3900,
        target_chars: int = 3500,
    ) -> tuple[str, DigestNarrativeDraft]:
        thematic_groups: dict[str, list[str]] = {
            "Коммунальная обстановка": [],
            "Связь и интернет": [],
            "Городская среда и бизнес": [],
            "Социальная помощь": [],
            "Безопасность и чрезвычайные ситуации": [],
            "Транспорт": [],
            "Здравоохранение": [],
            "Образование и культура": [],
            "Другое": [],
        }
        from src.domain.edition_geography import (
            normalize_edition_toponyms,
            resolve_edition_geography,
        )

        geo_ctx = resolve_edition_geography(city.lower(), city)
        cards_text_blocks: list[str] = []

        for c in cards:
            if not c.topic:
                continue
            clean_topic = normalize_edition_toponyms(
                sanitize_digest_terminology(c.topic), edition_slug=geo_ctx.slug
            )
            clean_summary = (
                normalize_edition_toponyms(
                    sanitize_digest_terminology(c.summary), edition_slug=geo_ctx.slug
                )
                if c.summary
                else ""
            )
            facts = [
                sanitize_digest_terminology(f.text) for f in getattr(c, "hard_facts", ()) if f.text
            ]
            obs = [
                sanitize_digest_terminology(o.text)
                for o in getattr(c, "community_observations", ())
                if o.text
            ]
            useful = [
                sanitize_digest_terminology(u.text)
                for u in getattr(c, "useful_details", ())
                if u.text
            ]
            all_details = facts + useful + obs
            details_list: list[str] = []
            if clean_summary:
                details_list.append(clean_summary)
            details_list.extend(all_details[:4])
            details_str = "; ".join(dict.fromkeys(details_list)) if details_list else ""
            block = f"- [{clean_topic}] {details_str}"
            cards_text_blocks.append(block)

            rubric_id = (getattr(c, "rubric_id", "") or getattr(c, "category", "")).lower()
            tags = {t.lower() for t in getattr(c, "tags", ())}
            text_l = f"{clean_topic.lower()} {clean_summary.lower()}"

            if (
                rubric_id == "urban_life"
                or any(k in tags for k in ("магазин", "торговля", "бизнес", "предприятие"))
                or any(
                    k in text_l
                    for k in (
                        "магазин",
                        "супермаркет",
                        "торгов",
                        "бизнес",
                        "предприяти",
                        "вывоз товара",
                        "закрыти",
                    )
                )
            ):
                thematic_groups["Городская среда и бизнес"].append(block)
            elif (
                rubric_id == "communications"
                or any(k in tags for k in ("интернет", "связь", "провайдер", "мобильная"))
                or any(k in text_l for k in ("интернет", "связь", "провайдер", "мобильн"))
            ):
                thematic_groups["Связь и интернет"].append(block)
            elif (
                rubric_id == "social"
                or any(k in tags for k in ("помощь", "социальн", "пенсия", "выплаты"))
                or any(
                    k in text_l for k in ("социальн", "пожилая", "выплат", "пенси", "гуманитарн")
                )
            ):
                thematic_groups["Социальная помощь"].append(block)
            elif (
                rubric_id == "safety"
                or any(
                    k in tags
                    for k in (
                        "безопасность",
                        "вспышка",
                        "взрыв",
                        "обстрел",
                        "сирена",
                        "чп",
                        "пожар",
                    )
                )
                or any(
                    k in text_l
                    for k in ("безопасн", "вспышка", "взрыв", "сирена", "обстрел", "пожар")
                )
            ):
                thematic_groups["Безопасность и чрезвычайные ситуации"].append(block)
            elif (
                rubric_id == "transport"
                or any(k in tags for k in ("транспорт", "автобус", "маршрутка", "поезд", "дорога"))
                or any(k in text_l for k in ("транспорт", "автобус", "маршрут", "дорог"))
            ):
                thematic_groups["Транспорт"].append(block)
            elif (
                rubric_id == "health"
                or any(
                    k in tags for k in ("здравоохранение", "медицина", "больница", "аптека", "врач")
                )
                or any(k in text_l for k in ("больниц", "поликлиник", "аптек", "врач", "медицин"))
            ):
                thematic_groups["Здравоохранение"].append(block)
            elif (
                rubric_id == "education"
                or any(k in tags for k in ("образование", "школа", "детсад", "спорт", "культура"))
                or any(k in text_l for k in ("школ", "детсад", "секц", "спорт", "обучени"))
            ):
                thematic_groups["Образование и культура"].append(block)
            elif (
                rubric_id == "infrastructure"
                or any(
                    k in tags
                    for k in (
                        "электроэнергия",
                        "свет",
                        "электричество",
                        "вода",
                        "водоснабжение",
                        "подстанция",
                        "напряжение",
                        "газ",
                        "отопление",
                    )
                )
                or any(
                    k in text_l
                    for k in (
                        "свет",
                        "электро",
                        "напряжен",
                        "вода",
                        "подстанция",
                        "водопровод",
                        "газоснабжен",
                        "отоплен",
                    )
                )
            ):
                thematic_groups["Коммунальная обстановка"].append(block)
            else:
                thematic_groups["Другое"].append(block)

        grouped_blocks = []
        for g_name, g_items in thematic_groups.items():
            if g_items:
                grouped_blocks.append(f"[{g_name.upper()}]\n" + "\n".join(g_items))

        content_for_llm = (
            "\n\n".join(grouped_blocks) if grouped_blocks else "\n".join(cards_text_blocks[:45])
        )

        toponym_section = ""
        if geo_ctx.toponym_rules:
            toponym_section = "\n\nВАЖНЫЕ МЕСТНЫЕ ТОПОНИМЫ И РАЗЛИЧЕНИЕ СУЩНОСТЕЙ:\n" + "\n".join(
                f"- {r}" for r in geo_ctx.toponym_rules
            )

        prompt = DIGEST_PROMPT_TEMPLATE.format(
            city=city,
            date=date_str,
            content=content_for_llm,
            toponym_rules=toponym_section,
        )

        logger.info("Digest Pass 1: Generating full-text journalistic draft...")
        raw_response = await self._provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
        )
        clean_draft = _clean_markdown_fence(raw_response)

        # Pass 2: Conditional AI Editorial Condensation
        if len(clean_draft) > max_chars:
            logger.info(
                "Digest draft (%d chars) exceeds limit (%d chars). Running Pass 2: AI Editorial Condenser...",
                len(clean_draft),
                max_chars,
            )
            condense_prompt = DIGEST_CONDENSE_PROMPT_TEMPLATE.format(
                city=city,
                date=date_str,
                current_len=len(clean_draft),
                max_chars=max_chars,
                target_chars=target_chars,
                draft_text=clean_draft,
            )
            raw_condensed = await self._provider.chat_completion(
                messages=[{"role": "user", "content": condense_prompt}],
                model=model,
            )
            clean_draft = _clean_markdown_fence(raw_condensed)

        # Final deterministic safety net
        if len(clean_draft) > max_chars:
            clean_draft = enforce_telegram_single_message_limit(clean_draft, max_chars=max_chars)

        # Normalize known local toponym errors for this edition
        clean_draft = normalize_edition_toponyms(clean_draft, edition_slug=geo_ctx.slug)

        # Enforce neutral administrative terminology (fail-safe against hostile labels)
        clean_draft = sanitize_digest_terminology(clean_draft)

        # Strip ungrounded reader recommendations / unsolicited advice
        clean_draft = strip_unsupported_recommendations(clean_draft, content_for_llm)

        # Strip redundant leading title header or conversational preamble if generated in body
        clean_draft = re.sub(
            r"^\s*[*_#\s]*дайд[жд]*ест[^\n]*[*_#\s]*\n+",
            "",
            clean_draft,
            flags=re.IGNORECASE,
        ).strip()
        clean_draft = re.sub(
            r"^\s*(?:Вот\s+(?:ежедневный\s+)?дайджест[^\n]*\n+|Ниже\s+(?:представлен|следует)[^\n]*\n+|Здравствуйте[^\n]*\n+)+",
            "",
            clean_draft,
            flags=re.IGNORECASE,
        ).strip()

        # 1. Strip all horizontal dividers / markdown rules (---, ***, ___)
        clean_draft = re.sub(r"(?m)^[^\S\r\n]*[-*_]{3,}[^\S\r\n]*$", "", clean_draft)

        # 2. Strip markdown header hashes (#, ##, ###)
        clean_draft = re.sub(r"(?m)^[^\S\r\n]*#{1,6}[^\S\r\n]+", "", clean_draft)

        # 3. Strip standalone bold/italic markup around rubric titles (**Header**, *Header*)
        clean_draft = re.sub(
            r"(?m)^[^\S\r\n]*\*\*[^\S\r\n]*([^\n*]+?)[^\S\r\n]*\*\*[^\S\r\n]*$",
            r"\1",
            clean_draft,
        )
        clean_draft = re.sub(
            r"(?m)^[^\S\r\n]*\*[^\S\r\n]*([^\n*]+?)[^\S\r\n]*\*[^\S\r\n]*$",
            r"\1",
            clean_draft,
        )

        # 4. Collapse multiple blank lines
        clean_draft = re.sub(r"\n{3,}", "\n\n", clean_draft).strip()

        draft = parse_journalistic_markdown_to_draft(
            clean_draft,
            cards=cards,
            evidence=evidence,
            custom_rubrics=custom_rubrics,
        )
        return clean_draft, draft

    async def generate_narrative_draft(
        self,
        *,
        plan: DigestNarrativePlan,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence],
        language: str = "Russian",
        max_output_tokens: int = 4096,
        model: str | None = None,
        situation_rollup: Any | None = None,
        situation_plan: Any | None = None,
    ) -> DigestNarrativeDraft:
        """Synthesize structured narrative draft in exactly one LLM call."""
        import json

        from src.publication.narrative_contract import build_digest_narrative_contract

        if plan.blocks and any(getattr(block, "composition_units", ()) for block in plan.blocks):
            return await self._generate_composition_draft(
                plan=plan,
                cards=cards,
                evidence=evidence,
                language=language,
                max_output_tokens=max_output_tokens,
                model=model,
            )

        has_topic_bundles = any(getattr(b, "topic_bundles", None) for b in plan.blocks)

        blocks_payload = []
        for b in plan.blocks:
            if getattr(b, "topic_bundles", None):
                bundles_payload = []
                for tb in b.topic_bundles:
                    req_facts_payload = [
                        {
                            "fact_id": rf.fact_id,
                            "text": rf.text,
                            "story_ids": list(rf.story_ids),
                        }
                        for rf in tb.required_facts
                    ]
                    bundles_payload.append(
                        {
                            "bundle_id": tb.bundle_id,
                            "topic": tb.topic_label,
                            "emoji": tb.emoji,
                            "locations": list(tb.locations),
                            "geographic_groups": [
                                group.to_dict() for group in tb.geographic_groups
                            ],
                            "unresolved_geography_story_ids": list(
                                tb.unresolved_geography_story_ids
                            ),
                            "unresolved_geography_facts": [
                                fact.to_dict() for fact in tb.unresolved_geography_facts
                            ],
                            "states": list(getattr(tb, "states", ())),
                            "epistemic_status": getattr(
                                tb, "epistemic_status", "сообщения жителей"
                            ),
                            "fact_ledger": list(tb.fact_ledger),
                            "required_facts": req_facts_payload,
                        }
                    )

                block_dict: dict[str, Any] = {
                    "block_id": b.block_id,
                    "rubric_id": b.rubric_id,
                    "rubric_title": b.rubric_title,
                    "topic_bundles": bundles_payload,
                }
                blocks_payload.append(block_dict)
            else:
                supports_payload = []
                for sid in b.support_ids:
                    if sid in evidence:
                        evi = evidence[sid]
                        supports_payload.append(
                            {
                                "id": sid,
                                "text": evi.text,
                                "role": evi.source_role,
                                "evidence_kind": evi.kind,
                                "publication_use": evi.publication_use,
                            }
                        )

                block_dict = {
                    "block_id": b.block_id,
                    "rubric_id": b.rubric_id,
                    "rubric_title": b.rubric_title,
                    "story_ids": list(b.story_ids),
                    "required_story_groups": [list(grp) for grp in b.required_story_groups],
                    "canonical_notes": list(b.canonical_notes),
                    "supports": supports_payload,
                }
                if b.detail_support_ids_by_story:
                    block_dict["detail_support_hints"] = [
                        {"story_id": sid, "detail_support_ids": list(sids)}
                        for sid, sids in b.detail_support_ids_by_story
                    ]
                if b.merge_group_by_story:
                    block_dict["merge_group_hints"] = [
                        {"story_id": sid, "merge_group_id": mgid}
                        for sid, mgid in b.merge_group_by_story
                    ]
                blocks_payload.append(block_dict)

        situation_payload = []
        if situation_plan is not None and getattr(situation_plan, "groups", None):
            for grp in situation_plan.groups:
                grp_supports = []
                all_grp_refs = tuple(
                    dict.fromkeys(
                        list(getattr(grp, "source_refs", ()))
                        + list(getattr(grp, "cited_support_ids", ()))
                    )
                )
                for ref in all_grp_refs:
                    if ref in evidence:
                        evi = evidence[ref]
                        grp_supports.append(
                            {
                                "id": ref,
                                "text": evi.text,
                                "role": evi.source_role,
                                "evidence_kind": evi.kind,
                                "publication_use": evi.publication_use,
                            }
                        )
                req_facts_payload = [
                    {
                        "fact_id": rf.fact_id,
                        "text": rf.text,
                        "support_ids": list(rf.support_ids),
                    }
                    for rf in getattr(grp, "required_facts", ())
                ]
                situation_payload.append(
                    {
                        "group_id": grp.group_id,
                        "label": grp.subject_label,
                        "state": grp.state,
                        "facts": list(grp.all_detail_lines or grp.detail_lines),
                        "required_facts": req_facts_payload,
                        "allowed_support_ids": list(all_grp_refs),
                        "supports": grp_supports,
                    }
                )

        narrative_contract = build_digest_narrative_contract(output_language=language)

        if has_topic_bundles:
            schema_desc = (
                "{\n"
                '  "items": [\n'
                "    {\n"
                '      "bundle_id": "string (must match input bundle_id exactly)",\n'
                '      "emoji": "string (thematic semantic emoji from bundle)",\n'
                '      "headline": "string (bold mini-summary answering what happened)",\n'
                '      "body": "string (cohesive 2-4 sentence narrative covering what happened, micro-locations in parentheses, explanations, and practical consequences)",\n'
                '      "covered_fact_ids": ["all fact IDs from required_facts reflected in the body; every required fact must be covered"]\n'
                "    }\n"
                "  ]\n"
                "}\n"
            )

            system_prompt = (
                "You are a professional regional newsroom editor and journalist.\n"
                "Your task is to write a cohesive, scan-first, and strictly factual daily news digest in Russian.\n\n"
                "EDITORIAL AND LANGUAGE RULES:\n"
                "- Write in professional Russian regional news style matching top Telegram channels.\n"
                "- Single Telegram post budget: The total rendered digest must fit into a single Telegram message (target 2500–3700 characters, technical maximum 4000 characters). Keep paragraphs dense, informative, and free of filler words. Each item must strictly consist of 2-3 concise sentences (target 180–280 characters, maximum 400 characters). Summarize locations compactly in parentheses, e.g. (на ул. Ленина, Шевченко, в Колонии).\n"
                "- Never output bullet points ('•') or dashes ('—') at the beginning of items.\n"
                "- For each topic bundle in 'topic_bundles', write ONE cohesive editorial item in 'items' (or up to TWO if the bundle covers distinct locations or situations that are clearer as separate items).\n"
                "- Geographic grouping is evidence-bound: each 'geographic_groups' entry contains source-backed 'facts' for its 'area_id'. A story_id may appear in more than one group; that means distinct facts from the same Story belong to different places. Use each group's 'area_name' only for that group's listed facts, not for every fact or every story in the bundle. Never use one area's name as an umbrella for facts in another group or in 'unresolved_geography_facts'. Topographic elevation does not make separately named areas the same neighborhood.\n"
                "- Use a localized contrast only for facts in the same 'area_id'. State facts from different geographic groups in separate short sentences; keep a separate sentence for each named area's group, even when the reports belong to one Story. Split into separate reader items when that reads more clearly. Never compress distant or differently named areas into one sentence with a shared district label. Preserve explicit street or place wording when its broader area is unresolved.\n"
                "- Set 'bundle_id' to the bundle's input 'bundle_id'.\n"
                "- Set 'emoji' using the bundle's emoji.\n"
                "- Headline and storytelling: headline must be a concise, informative theme header answering what occurred (e.g. 'Запах газа на улицах города', 'Ремонт магистральных интернет-сетей', 'Обновление квитанций за коммунальные услуги'). Never write generic headlines like 'текущая обстановка' or 'обзор сообщений'. Never place attribution phrases in the headline ('сообщается', 'по информации', 'по словам').\n"
                "- Craft rich, 2-3 sentence journalistic paragraphs following a cohesive storytelling structure:\n"
                "  1. What occurred + concrete micro-locations/districts/streets (in parentheses if listing multiple).\n"
                "  2. Current state, contrast, or cause (from 'states' or 'fact_ledger').\n"
                "  3. Practical consequences for residents only when explicitly present in the source facts; never give advice or recommendations.\n"
                "- Localized contrast synthesis is valid only when the reports name the same place or canonical area. Keep each report attached to its own street or area. Separate different named neighborhoods into short factual sentences; never use one area's name as an umbrella for other locations, and never infer common area from elevation or terrain. Do not make mutually contradictory assertions about the same place and time.\n"
                "- If source facts or notes are in Ukrainian, accurately translate and paraphrase them into Russian.\n"
                "- Cover every entry in 'required_facts' in the item's body and list every covered fact_id in 'covered_fact_ids'. Do not omit a required fact; synthesize related facts and localized contrasts compactly instead of repeating them.\n"
                "- In thematic items, never repeat the headline in the first sentence of the body text.\n"
                "- Never chain repetitive transitional phrases like 'Также... Ранее также...'.\n"
                "- State facts directly. NEVER invent or infer unverified causal relations or mechanisms (strictly avoid causal phrases like 'из-за чего', 'по причине', 'вследствие', 'в результате', 'привело к'). State temporal sequence ('после...', 'в ходе...') or describe observed facts directly.\n"
                '- Avoid direct quotes in quotation marks («...» or "..."). Always prefer smooth indirect speech and paraphrasing in Russian regional news style.\n'
                "- Never invent resident advice, recommendations, or procedural tips (e.g. 'жителям советуют', 'рекомендуется') unless that specific instruction is explicitly stated in the source facts.\n"
                "- Never output meta-commentary like 'Новых сообщений не поступало' or 'тихий день'. Focus strictly on concrete reported facts.\n"
                "- Natural, varied journalistic attribution: Do NOT repeat the phrase 'По сообщениям жителей' across items. Vary attribution naturally ('горожане отмечают', 'по словам жителей', 'в районе зафиксировали', 'жители сообщают') or state established civic events directly ('Провайдер приступил к работам', 'Вступил в силу запрет', 'Над городом фиксировались'). Never start consecutive items with the same attribution opening. Never place attribution in the headline.\n"
                "- Filter out chat noise: do NOT mention chat polls, stickers, reactions, greetings, or off-topic conversational chatter.\n"
                "- NEVER mention chat sources or social channels: forbidden phrases include 'в чатах', 'в городских чатах', 'в каналах', 'участники чата', 'в пабликах', 'перекличка'. Always translate into natural journalistic language: 'жители сообщают', 'в городе отмечают', 'по сообщениям горожан' or state facts directly.\n"
                "- Brand and facility recognition: commercial names such as «Семья», «Экватор», «Улей», «Мера», «Грация», «Зеркальный» are retail stores, commercial brands, or shopping centers, NOT human families or natural phenomena. Never refer to a store «Семья» as human families ('семьи пострадали' -> 'магазин «Семья» получил повреждения').\n"
                "- Single incident consolidation: all reports relating to the same incident, strike, or facility (e.g. night strike on ТРЦ «Экватор», fire localization, warehouse damage, and affected tenant stores) belong in ONE cohesive editorial item. Never split stages or tenant stores of the same incident into separate bullet points.\n"
                "- Questions are context, not answers or standalone factual reports; never turn them into meta-news or operational status. Report source-supported civic facts and service states faithfully. An eligible single-source community PUBLISH report remains publishable with honest attribution; do not require corroboration or official confirmation.\n"
                "- Do NOT invent compound Frankenstein headlines merging unrelated topics, separate facilities, or distinct businesses (e.g. NEVER write 'X пострадал, где купить Y'). Keep separate businesses, different facilities, and unrelated incidents distinct.\n"
                "- Rich local detail: preserve concrete micro-locations (districts, streets, landmarks), contrasts between neighborhoods, specific durations, equipment, and practical resident consequences from 'fact_ledger'. Do not flatten concrete lived reality into vague generic summaries.\n\n"
                f"{narrative_contract}\n\n"
                "OUTPUT FORMAT REQUIREMENTS:\n"
                "Return ONLY valid JSON strictly matching this schema:\n"
                f"{schema_desc}"
            )
        else:
            situation_schema = ""
            if situation_payload:
                situation_schema = (
                    '  "situation_items": [\n'
                    "    {\n"
                    '      "group_id": "string (must match input group_id exactly)",\n'
                    "      \"emoji\": \"string (semantic emoji, e.g. '⚡️' for power, '💧' for water, '💨' for gas, '🚌' for transport)\",\n"
                    '      "label": "string (subject label matching input group label, e.g. \'Электроснабжение\')",\n'
                    '      "body": "string (1-3 sentences of cohesive editorial prose synthesizing the operational facts)",\n'
                    '      "cited_support_ids": ["string (support IDs cited)"],\n'
                    '      "claims": [\n'
                    "        {\n"
                    '          "text": "string (atomic factual claim in Russian)",\n'
                    '          "covered_fact_ids": ["string (fact IDs this claim covers)"],\n'
                    '          "covered_story_ids": ["string (story IDs this claim covers)"],\n'
                    '          "cited_support_ids": ["string (support IDs supporting this claim)"]\n'
                    "        }\n"
                    "      ]\n"
                    "    }\n"
                    "  ],\n"
                )
            else:
                situation_schema = '  "situation_items": [],\n'

            schema_desc = (
                "{\n"
                f"{situation_schema}"
                '  "blocks": [\n'
                "    {\n"
                '      "block_id": "string (must match input block_id exactly)",\n'
                '      "items": [\n'
                "        {\n"
                "          \"emoji\": \"string (thematic semantic emoji, e.g. '⚡️', '💨', '💥', '🛡', '🌐', '🚌', '🏢', '🚫', '📚', '📌')\",\n"
                '          "headline": "string (bold mini-summary answer to what happened)",\n'
                '          "body": "string (cohesive 2-4 sentence narrative covering what happened, micro-locations in parentheses, explanations, and practical consequences)",\n'
                '          "covered_story_ids": ["string (story IDs covered)"],\n'
                '          "cited_support_ids": ["string (support IDs cited)"],\n'
                '          "claims": [\n'
                "            {\n"
                '              "text": "string (atomic factual claim in Russian)",\n'
                '              "covered_story_ids": ["string (story IDs this claim covers)"],\n'
                '              "cited_support_ids": ["string (support IDs supporting this claim)"]\n'
                "            }\n"
                "          ]\n"
                "        }\n"
                "      ]\n"
                "    }\n"
                "  ]\n"
                "}\n"
            )

            system_prompt = (
                "You are a professional regional newsroom editor and journalist.\n"  # noqa: S608
                "Your task is to write a cohesive, scan-first, and strictly factual daily news digest in Russian.\n\n"
                "EDITORIAL AND LANGUAGE RULES:\n"
                "- Write in professional Russian regional news style matching top Telegram channels.\n"
                "- Never output bullet points ('•') or dashes ('—') at the beginning of items.\n"
                "- For each item, select an accurate thematic semantic emoji (e.g. '⚡️', '💨', '💥', '🛡', '🌐', '🚌', '🏢', '🚫', '📚', '📌') in 'emoji'.\n"
                "- Craft rich, 2-3 sentence journalistic paragraphs following a cohesive storytelling structure:\n"
                "  1. What occurred + concrete micro-locations/districts/streets (in parentheses if listing multiple).\n"
                "  2. Cause or official/specialist explanation (if supported in evidence, e.g. technical works, scheduled maintenance, odorant markers).\n"
                "  3. Practical consequences for residents only when explicitly present in the evidence; never give advice or recommendations.\n"
                "- Geographic accuracy: keep every claim attached to the location in its cited support. A localized contrast is allowed only for claims naming the same place or canonical area. When support names different areas, express their conditions in separate short sentences; do not assign one area's name to the others or infer shared geography from elevation or terrain. Leave unresolved locations at the source's level of detail.\n"
                "- If source facts or notes are in Ukrainian, accurately translate and paraphrase them into Russian.\n"
                "- If 'situation_groups' are provided, synthesize each operational group in 'situation_items'. Every required fact in 'required_facts' must be covered in 'claims' and reflected in the narrative body. Use natural chronology and geographical clarity (e.g. outages, low voltage, and restored sections). Never invent ungrounded numbers or causes. Cite the exact support IDs.\n"
                "- In thematic 'blocks', never repeat the headline in the first sentence of the body text.\n"
                "- Never chain repetitive transitional phrases like 'Также... Ранее также...'.\n"
                "- State facts directly. NEVER invent or infer unverified causal relations or mechanisms (using phrases like 'из-за чего', 'по причине', 'вследствие', 'в результате') unless that causal relation is explicitly stated in the source evidence.\n"
                "- Attribution: Attribute source role naturally ('По сообщениям жителей', 'По данным коммунальных служб') at most once per item. Never place attribution in the headline.\n"
                "- NEVER mention chat sources or social channels: forbidden phrases include 'в чатах', 'в городских чатах', 'в каналах', 'участники чата', 'в пабликах', 'перекличка'. Always translate into natural journalistic language: 'жители сообщают', 'в городе отмечают', 'по сообщениям горожан' or state facts directly.\n"
                "- Do NOT invent compound Frankenstein headlines merging unrelated topics, separate facilities, or distinct businesses (e.g. NEVER write 'X пострадал, где купить Y'). Keep separate businesses, different facilities, and unrelated incidents distinct.\n"
                "- Claims must be short atomic factual statements supported by cited_support_ids.\n\n"
                f"{narrative_contract}\n\n"
                "OUTPUT FORMAT REQUIREMENTS:\n"
                "Return ONLY valid JSON strictly matching this schema:\n"
                f"{schema_desc}"
            )

        user_dict: dict[str, Any] = {"blocks": blocks_payload}
        if situation_payload:
            user_dict["situation_groups"] = situation_payload
        user_prompt = json.dumps(user_dict, ensure_ascii=False, indent=2)

        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "reasoning_effort": "none",
            "thinking": False,
        }
        if model:
            chat_kwargs["model"] = model
        if max_output_tokens:
            chat_kwargs["max_tokens"] = max_output_tokens

        raw_response = await self._provider.chat_completion(**chat_kwargs)

        cleaned = (raw_response or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        if "{" in cleaned and "}" in cleaned:
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        try:
            parsed = json.loads(cleaned)
        except Exception as err:
            raise ValueError(
                f"Failed to decode LLM response as JSON: {err}. Raw was: {raw_response[:200]!r}"
            ) from err

        # If top-level "items" was returned (the new minimal schema), wrap into blocks
        if (
            has_topic_bundles
            and isinstance(parsed, dict)
            and isinstance(parsed.get("items"), list)
            and not parsed.get("blocks")
        ):
            from src.domain.service_taxonomy import detect_service_families, map_family_to_rubric

            bundle_to_block_id: dict[str, str] = {}
            rubric_to_block_id: dict[str, str] = {}
            fact_to_block_id: dict[str, str] = {}
            fact_to_bundle_id: dict[str, str] = {}
            story_to_block_id: dict[str, str] = {}
            story_to_bundle_id: dict[str, str] = {}
            topic_key_to_bundle: dict[tuple[str, str], tuple[str, str]] = {}
            global_topic_key_to_bundle: dict[str, tuple[str, str]] = {}

            for pb in plan.blocks:
                rubric_to_block_id[pb.rubric_id] = pb.block_id
                for tb in getattr(pb, "topic_bundles", ()):
                    bundle_to_block_id[tb.bundle_id] = pb.block_id
                    t_key = getattr(tb, "topic_key", "")
                    if t_key:
                        topic_key_to_bundle[(pb.rubric_id, t_key)] = (pb.block_id, tb.bundle_id)
                        global_topic_key_to_bundle.setdefault(t_key, (pb.block_id, tb.bundle_id))
                    for s_id in getattr(tb, "story_ids", ()):
                        story_to_block_id[str(s_id)] = pb.block_id
                        story_to_bundle_id[str(s_id)] = tb.bundle_id
                    for rf in getattr(tb, "required_facts", ()):
                        fact_to_block_id[str(rf.fact_id)] = pb.block_id
                        fact_to_bundle_id[str(rf.fact_id)] = tb.bundle_id
                for s_id in getattr(pb, "story_ids", ()):
                    story_to_block_id.setdefault(str(s_id), pb.block_id)
                for rf in getattr(pb, "required_facts", ()):
                    fact_to_block_id.setdefault(str(rf.fact_id), pb.block_id)

            blocks_map: dict[str, list[dict[str, Any]]] = {}
            for item_index, it in enumerate(parsed["items"]):
                if not isinstance(it, dict):
                    raise ValueError(f"items[{item_index}] must be an object")
                bid = str(it.get("bundle_id") or "").strip()
                if not bid:
                    raise ValueError(f"items[{item_index}] is missing bundle_id")

                target_block_id = bundle_to_block_id.get(bid)
                target_bundle_id = bid if target_block_id else None

                # 1. Resilient fallback matching for shortened prefix or extension of a known bundle_id
                # (e.g. model omitted :fact:... suffix: "bundle:economy:economy_general" for "bundle:economy:economy_general:fact:...")
                if not target_block_id:
                    for tb_bid, blk_id in bundle_to_block_id.items():
                        if tb_bid.startswith(bid) or bid.startswith(tb_bid):
                            target_block_id = blk_id
                            target_bundle_id = tb_bid
                            break

                # 2. Match via covered_fact_ids (ground truth provenance link to bundle and block)
                if not target_block_id:
                    fids = [
                        str(x).strip()
                        for x in (it.get("covered_fact_ids") or it.get("fact_ids") or [])
                        if x
                    ]
                    for fid in fids:
                        if fid in fact_to_block_id:
                            target_block_id = fact_to_block_id[fid]
                            target_bundle_id = fact_to_bundle_id.get(fid)
                            break

                # 3. Match via covered_story_ids / story_ids
                if not target_block_id:
                    sids = [
                        str(x).strip()
                        for x in (it.get("covered_story_ids") or it.get("story_ids") or [])
                        if x
                    ]
                    for sid in sids:
                        if sid in story_to_block_id:
                            target_block_id = story_to_block_id[sid]
                            target_bundle_id = story_to_bundle_id.get(sid)
                            break

                # 4. Match by semantic topic or service family tokens in bid
                # (e.g. "bundle:infrastructure:internet" -> "internet" is connectivity -> rubric "communications"
                #  or "bundle:infrastructure:roads" -> "roads" is transport)
                if not target_block_id and bid:
                    bid_tokens = [p.casefold() for p in re.split(r"[:_\W]+", bid) if p]
                    _SYNONYM_TO_TOPIC = {
                        "internet": "connectivity",
                        "telecom": "connectivity",
                        "wifi": "connectivity",
                        "cellular": "connectivity",
                        "mobile": "connectivity",
                        "network": "connectivity",
                        "power": "electricity",
                        "blackout": "electricity",
                        "light": "electricity",
                        "energy": "electricity",
                        "aqueduct": "water",
                        "water": "water",
                        "heat": "heating",
                        "heating": "heating",
                        "gas": "gas",
                        "transport": "transport",
                        "roads": "transport",
                        "road": "transport",
                        "traffic": "transport",
                        "street": "transport",
                        "streets": "transport",
                        "bus": "transport",
                        "strike": "strikes",
                        "strikes": "strikes",
                        "attack": "strikes",
                        "explosion": "strikes",
                        "fire": "fire",
                        "fires": "fire",
                        "emergency": "safety",
                        "safety": "safety",
                        "bank": "banking",
                        "banks": "banking",
                        "banking": "banking",
                        "cash": "banking",
                        "pension": "social",
                        "aid": "social",
                        "social": "social",
                        "society": "social",
                        "hospital": "health",
                        "medicine": "health",
                        "health": "health",
                        "school": "education",
                        "education": "education",
                        "market": "economy",
                        "business": "economy",
                        "trade": "economy",
                        "economy": "economy",
                    }
                    candidate_topics = [
                        _SYNONYM_TO_TOPIC.get(t, t)
                        for t in bid_tokens
                        if t not in ("bundle", "fact", "item", "unknown")
                    ]
                    # Check rubric-qualified matches first (e.g. infrastructure + transport)
                    for c_top in candidate_topics:
                        for (r_id, t_k), (b_id, tb_id) in topic_key_to_bundle.items():
                            if (t_k == c_top or c_top in t_k or t_k in c_top) and (
                                r_id in bid_tokens
                            ):
                                target_block_id = b_id
                                target_bundle_id = tb_id
                                break
                        if target_block_id:
                            break

                    # Then check global topic match
                    if not target_block_id:
                        for c_top in candidate_topics:
                            if c_top in global_topic_key_to_bundle:
                                target_block_id, target_bundle_id = global_topic_key_to_bundle[
                                    c_top
                                ]
                                break

                    if not target_block_id:
                        detected_families = detect_service_families(" ".join(bid_tokens))
                        for fam in detected_families:
                            mapped_rid = map_family_to_rubric(fam)
                            if mapped_rid and mapped_rid in rubric_to_block_id:
                                target_block_id = rubric_to_block_id[mapped_rid]
                                for tb_bid, blk_id in bundle_to_block_id.items():
                                    if blk_id == target_block_id:
                                        target_bundle_id = tb_bid
                                        break
                                break

                # 5. Match by content semantics using canonical topic family classifier
                if not target_block_id:
                    item_text = f"{it.get('headline', '')} {it.get('body', '')}".strip()
                    if item_text:
                        from src.publication.digest_presentation import _canonical_topic_family

                        class _TextProxyCard:
                            def __init__(self, text: str) -> None:
                                self.topic = text
                                self.summary = ""
                                self.category = ""
                                self.tags = ()

                        c_family, _, _ = _canonical_topic_family(_TextProxyCard(item_text))
                        if c_family in global_topic_key_to_bundle:
                            target_block_id, target_bundle_id = global_topic_key_to_bundle[c_family]
                        elif not target_block_id:
                            detected_families = detect_service_families(item_text)
                            for fam in detected_families:
                                mapped_rid = map_family_to_rubric(fam)
                                if mapped_rid and mapped_rid in rubric_to_block_id:
                                    for (r_id, t_k), (b_id, tb_id) in topic_key_to_bundle.items():
                                        if r_id == mapped_rid and (
                                            t_k == fam
                                            or (fam == "telecom" and t_k == "connectivity")
                                            or (fam == "power" and t_k == "electricity")
                                        ):
                                            target_block_id = b_id
                                            target_bundle_id = tb_id
                                            break
                                    if target_block_id:
                                        break

                if not target_block_id:
                    raise ValueError(f"unknown bundle_id: {bid}")

                if target_bundle_id:
                    it["bundle_id"] = target_bundle_id

                blocks_map.setdefault(target_block_id, []).append(it)

            parsed["blocks"] = [
                {"block_id": b_id, "items": items_list} for b_id, items_list in blocks_map.items()
            ]

        # Consolidate any duplicate block_ids produced by LLM and normalize strictly against plan.blocks
        if isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
            merged_blocks: list[Any] = []
            block_by_id: dict[str, dict[str, Any]] = {}

            for b in parsed["blocks"]:
                if isinstance(b, dict) and b.get("block_id"):
                    bid = str(b["block_id"]).strip()
                    if bid in block_by_id:
                        existing_items = block_by_id[bid].setdefault("items", [])
                        new_items = b.get("items", [])
                        if isinstance(existing_items, list) and isinstance(new_items, list):
                            existing_items.extend(new_items)
                    else:
                        block_by_id[bid] = b
                        merged_blocks.append(b)
                else:
                    merged_blocks.append(b)

            # Ensure all plan blocks exist and are strictly ordered
            final_blocks: list[dict[str, Any]] = []

            for plan_block in plan.blocks:
                b_raw = block_by_id.get(plan_block.block_id)
                if b_raw is None:
                    # Find by rubric if block_id format differed
                    b_raw = next(
                        (
                            b
                            for bid, b in block_by_id.items()
                            if bid.startswith(f"block:{plan_block.rubric_id}:")
                        ),
                        None,
                    )
                if b_raw is None:
                    b_raw = {"block_id": plan_block.block_id, "items": []}
                else:
                    b_raw["block_id"] = plan_block.block_id

                # If block has topic_bundles, align items to topic_bundles
                if getattr(plan_block, "topic_bundles", None) and isinstance(
                    b_raw.get("items"), list
                ):
                    raw_items = b_raw["items"]
                    norm_items: list[dict[str, Any]] = []
                    assigned_bundle_ids: set[str] = set()

                    for it in raw_items:
                        if not isinstance(it, dict):
                            continue
                        it_bid = str(it.get("bundle_id") or "").strip()
                        it_sids = {str(x) for x in (it.get("covered_story_ids") or [])}
                        matched_tb = None

                        # 1. Match by bundle_id (exact or prefix/suffix)
                        if it_bid:
                            for tb in plan_block.topic_bundles:
                                if tb.bundle_id not in assigned_bundle_ids and (
                                    tb.bundle_id == it_bid
                                    or tb.bundle_id.startswith(it_bid)
                                    or it_bid.startswith(tb.bundle_id)
                                ):
                                    matched_tb = tb
                                    break

                        # 2. Match by covered_fact_ids, story_ids, or topic_key
                        if matched_tb is None:
                            it_fids = {
                                str(x).strip() for x in (it.get("covered_fact_ids") or []) if x
                            }
                            for tb in plan_block.topic_bundles:
                                if tb.bundle_id not in assigned_bundle_ids:
                                    tb_fids = {
                                        str(rf.fact_id).strip()
                                        for rf in getattr(tb, "required_facts", ())
                                    }
                                    if (
                                        bool(it_fids & tb_fids)
                                        or bool(it_sids & set(tb.story_ids))
                                        or tb.bundle_id in it_sids
                                        or tb.topic_key in it_sids
                                    ):
                                        matched_tb = tb
                                        break

                        # 3. Match by topic label substring
                        if matched_tb is None:
                            it_text = (it.get("headline", "") + " " + it.get("body", "")).casefold()
                            for tb in plan_block.topic_bundles:
                                if tb.bundle_id not in assigned_bundle_ids:
                                    if (
                                        tb.topic_label.casefold() in it_text
                                        or tb.topic_key.casefold() in it_text
                                    ):
                                        matched_tb = tb
                                        break

                        # 4. Fallback: match to first unassigned bundle in this block
                        if matched_tb is None:
                            for tb in plan_block.topic_bundles:
                                if tb.bundle_id not in assigned_bundle_ids:
                                    matched_tb = tb
                                    break

                        if matched_tb:
                            assigned_bundle_ids.add(matched_tb.bundle_id)
                            it["covered_story_ids"] = list(matched_tb.story_ids)
                            if not it.get("emoji"):
                                it["emoji"] = matched_tb.emoji

                            # Sanitize headline
                            headline = str(it.get("headline", "")).strip()
                            clean_headline = (
                                re.sub(r"\bиз-за\b", "при", headline, flags=re.IGNORECASE)
                                if headline
                                else ""
                            )
                            clean_headline = re.sub(
                                r"\bв\s+результате\b", "после", clean_headline, flags=re.IGNORECASE
                            )
                            clean_headline = re.sub(
                                r"\bпо\s+причине\b", "при", clean_headline, flags=re.IGNORECASE
                            )
                            if clean_headline:
                                clean_headline = re.sub(
                                    r"^(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+горожан)[\s,:]*",
                                    "",
                                    clean_headline,
                                    flags=re.IGNORECASE,
                                ).strip()
                                if clean_headline:
                                    clean_headline = clean_headline[:1].upper() + clean_headline[1:]
                            it["headline"] = (
                                clean_headline or f"{matched_tb.topic_label}: ситуация в городе"
                            )

                            # Sanitize body
                            body = str(it.get("body", "")).strip()
                            clean_body = body
                            if clean_body:
                                clean_body = re.sub(
                                    r"[«\"]по свету ноль[»\"]", "по свету ноль", clean_body
                                )
                                clean_body = re.sub(
                                    r"\s+вместо\s+220(?:\s*[вВвольт]+)?", "", clean_body
                                )

                                # Strip redundant headline repetition at start of body
                                if clean_headline:
                                    clean_body = _strip_redundant_headline_from_body(
                                        clean_headline, clean_body
                                    )

                                # Remove duplicate attribution in body if present multiple times
                                att_matches = list(
                                    re.finditer(
                                        r"\b(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+горожан)[\s,:]*",
                                        clean_body,
                                        flags=re.IGNORECASE,
                                    )
                                )
                                if len(att_matches) > 1:
                                    for m in reversed(att_matches[1:]):
                                        clean_body = clean_body[: m.start()] + clean_body[m.end() :]
                                    clean_body = re.sub(
                                        r"\.\s+([a-zа-я])",
                                        lambda x: ". " + x.group(1).upper(),
                                        clean_body,
                                    )

                                # Remove chat metadata and emoji spam
                                clean_body = re.sub(
                                    r"(?:публикуют\s+)?сообщения\s+с\s+эмодзи[\w\s,]*[.]?",
                                    "",
                                    clean_body,
                                    flags=re.IGNORECASE,
                                ).strip()
                                clean_body = re.sub(
                                    r"\bсмайлик(?:ами|и)?\b", "", clean_body, flags=re.IGNORECASE
                                ).strip()
                                clean_body = re.sub(
                                    r"\b(?:в\s+местных\s+чатах|в\s+чате(?:\s+[А-Яа-я]+)?|в\s+местном\s+чате|в\s+городском\s+чате)\b",
                                    "в городе",
                                    clean_body,
                                    flags=re.IGNORECASE,
                                )
                                clean_body = re.sub(
                                    r"\bиз-за\s+(?:этого|чего|которых)\b",
                                    "при этом",
                                    clean_body,
                                    flags=re.IGNORECASE,
                                )
                                clean_body = re.sub(
                                    r"\bиз-за\b", "при", clean_body, flags=re.IGNORECASE
                                )
                                clean_body = re.sub(
                                    r"\bв\s+результате\b", "после", clean_body, flags=re.IGNORECASE
                                )
                                clean_body = re.sub(
                                    r"\bпо\s+причине\b", "при", clean_body, flags=re.IGNORECASE
                                )
                                clean_body = re.sub(
                                    r"\bвследствие\b", "при", clean_body, flags=re.IGNORECASE
                                )
                                clean_body = re.sub(
                                    r"\b(?:жителям|горожанам)?\s*советуют\s+ехать\b",
                                    "выезжают",
                                    clean_body,
                                    flags=re.IGNORECASE,
                                )
                                clean_body = re.sub(
                                    r"\b(?:жителям|горожанам)\s+советуют\b",
                                    "в городе отмечают",
                                    clean_body,
                                    flags=re.IGNORECASE,
                                )
                                clean_body = re.sub(r"«([^»]+)»", r"\1", clean_body)
                                clean_body = re.sub(r'"([^"]+)"', r"\1", clean_body)
                                if len(clean_body) > 1150:
                                    clean_body = (
                                        clean_body[:1150].rsplit(" ", 1)[0].rstrip(".,;: ") + "."
                                    )
                                clean_body = re.sub(r"\s{2,}", " ", clean_body).strip()
                            clean_it_headline = it.get("headline", "")
                            clean_it_body = (
                                clean_body
                                or f"{matched_tb.topic_label} в городе остаётся на контроле городских служб."
                            )
                            clean_it_headline, clean_it_body = _fix_redundant_headline_and_body(
                                clean_it_headline,
                                clean_it_body,
                                matched_tb.topic_label if matched_tb else "",
                            )
                            clean_it_headline, clean_it_body = _fix_duplicated_attribution(
                                clean_it_headline,
                                clean_it_body,
                            )
                            it["headline"] = clean_it_headline
                            it["body"] = clean_it_body

                            # Allowed block supports
                            allowed_block_supports = set(plan_block.support_ids)
                            base_sups = [
                                s for s in matched_tb.support_ids if s in allowed_block_supports
                            ]
                            for rf in matched_tb.required_facts:
                                for s in rf.support_ids:
                                    if s in allowed_block_supports and s not in base_sups:
                                        base_sups.append(s)
                            if not base_sups:
                                base_sups = list(matched_tb.support_ids[:2]) or list(
                                    matched_tb.story_ids[:1]
                                )

                            base_claim_text = (
                                matched_tb.fact_ledger[0]
                                if matched_tb.fact_ledger
                                else (
                                    it["headline"]
                                    or f"{matched_tb.topic_label}: обстановка остаётся стабильной."
                                )
                            )
                            base_claim_text = re.sub(r'["«»\']', "", base_claim_text)
                            base_claim_text = re.sub(
                                r"\bиз-за\b", "при", base_claim_text, flags=re.IGNORECASE
                            )
                            base_claim_text = re.sub(
                                r"\bв\s+результате\b", "после", base_claim_text, flags=re.IGNORECASE
                            )
                            base_claim_text = re.sub(
                                r"\bпо\s+причине\b", "при", base_claim_text, flags=re.IGNORECASE
                            )

                            claims: list[dict[str, Any]] = [
                                {
                                    "text": base_claim_text,
                                    "covered_story_ids": list(matched_tb.story_ids),
                                    "cited_support_ids": base_sups,
                                    "covered_fact_ids": [],
                                }
                            ]

                            # Bind claims for facts explicitly covered by the model or reflected in text
                            known_fact_ids = {rf.fact_id for rf in matched_tb.required_facts}
                            raw_covered_fids = {
                                str(fid).strip()
                                for fid in (it.get("covered_fact_ids") or [])
                                if str(fid).strip()
                            }
                            covered_fids = {
                                fid for fid in raw_covered_fids if fid in known_fact_ids
                            }
                            norm_covered_fids = {
                                fid.replace("ё", "е").lower() for fid in raw_covered_fids
                            }
                            norm_body = clean_body.replace("ё", "е").lower()

                            _STOP_WORDS = {
                                "бердянск",
                                "ул",
                                "улица",
                                "район",
                                "часть",
                                "город",
                                "г",
                                "в",
                                "на",
                                "по",
                                "с",
                                "у",
                                "не",
                                "нет",
                                "ее",
                                "его",
                                "их",
                                "уже",
                            }

                            for rf in matched_tb.required_facts:
                                norm_rf_id = rf.fact_id.replace("ё", "е").lower()
                                is_covered = (
                                    rf.fact_id in covered_fids or norm_rf_id in norm_covered_fids
                                )
                                rf_tokens = set(norm_rf_id.split("_")) - _STOP_WORDS
                                if not is_covered and raw_covered_fids:
                                    if rf_tokens and any(
                                        rf_tokens
                                        == (
                                            set(cf.replace("ё", "е").lower().split("_"))
                                            - _STOP_WORDS
                                        )
                                        or rf_tokens.issubset(
                                            set(cf.replace("ё", "е").lower().split("_"))
                                            - _STOP_WORDS
                                        )
                                        or (
                                            set(cf.replace("ё", "е").lower().split("_"))
                                            - _STOP_WORDS
                                        ).issubset(rf_tokens)
                                        for cf in raw_covered_fids
                                    ):
                                        is_covered = True
                                    elif rf_tokens and any(
                                        tok in norm_body for tok in rf_tokens if len(tok) >= 3
                                    ):
                                        is_covered = True
                                elif not is_covered:
                                    if rf_tokens and any(
                                        tok in norm_body for tok in rf_tokens if len(tok) >= 3
                                    ):
                                        is_covered = True

                                # Designated item synthesized for matched_tb covers all required facts of this bundle
                                if not is_covered and matched_tb.bundle_id == bid:
                                    is_covered = True

                                if not is_covered:
                                    continue
                                rf_text = rf.text or base_claim_text
                                rf_text = re.sub(r'["«»“„\']', "", rf_text)
                                rf_text = re.sub(r"\bиз-за\b", "при", rf_text, flags=re.IGNORECASE)
                                rf_text = re.sub(
                                    r"\bв\s+результате\b", "после", rf_text, flags=re.IGNORECASE
                                )
                                rf_text = re.sub(
                                    r"\bпо\s+причине\b", "при", rf_text, flags=re.IGNORECASE
                                )
                                rf_text = re.sub(
                                    r"\bвследствие\b", "после", rf_text, flags=re.IGNORECASE
                                )
                                allowed_fact_sups = set(rf.support_ids)
                                story_sups_map = dict(plan_block.support_ids_by_story)
                                for sid in rf.story_ids:
                                    allowed_fact_sups.update(story_sups_map.get(sid, ()))
                                rf_sups = [s for s in rf.support_ids if s in allowed_block_supports]
                                for sid in rf.story_ids:
                                    if sid in allowed_block_supports and sid not in rf_sups:
                                        rf_sups.append(sid)
                                if not (set(rf_sups) & allowed_fact_sups):
                                    rf_sups = [
                                        s for s in allowed_fact_sups if s in allowed_block_supports
                                    ]
                                rf_sids = list(
                                    set(rf.story_ids) & set(matched_tb.story_ids)
                                ) or list(matched_tb.story_ids)
                                claims.append(
                                    {
                                        "text": rf_text,
                                        "covered_story_ids": rf_sids,
                                        "cited_support_ids": rf_sups,
                                        "covered_fact_ids": [rf.fact_id],
                                    }
                                )

                            it["claims"] = claims
                            claim_sups = [
                                s
                                for c in claims
                                for s in _clean_str_list(c.get("cited_support_ids"))
                            ]
                            cur_sups = _clean_str_list(it.get("cited_support_ids"))
                            it["cited_support_ids"] = list(
                                dict.fromkeys(base_sups + cur_sups + claim_sups)
                            )
                            norm_items.append(it)
                        else:
                            # Drop surplus unmapped item in bundle-based blocks to prevent ungrounded validation failures
                            logger.info(
                                "Ignoring surplus unmapped item in block %s: %s",
                                plan_block.block_id,
                                it.get("headline", ""),
                            )

                    # If any bundle in plan_block was missed entirely by LLM, synthesize it
                    for tb in plan_block.topic_bundles:
                        if tb.bundle_id not in assigned_bundle_ids:
                            clean_text = (
                                tb.fact_ledger[0]
                                if tb.fact_ledger
                                else f"{tb.topic_label}: обстановка остаётся стабильной."
                            )
                            clean_text = re.sub(r'["«»“„\']', "", clean_text)
                            clean_text = re.sub(
                                r"\bиз-за\b", "при", clean_text, flags=re.IGNORECASE
                            )
                            clean_text = re.sub(
                                r"\bв\s+результате\b", "после", clean_text, flags=re.IGNORECASE
                            )
                            clean_text = re.sub(
                                r"\bпо\s+причине\b", "при", clean_text, flags=re.IGNORECASE
                            )
                            clean_text = re.sub(
                                r"\bвследствие\b", "после", clean_text, flags=re.IGNORECASE
                            )
                            sups = list(tb.support_ids) if tb.support_ids else [tb.story_ids[0]]
                            req_claims = []
                            for rf in tb.required_facts:
                                rf_text = rf.text or clean_text
                                rf_text = re.sub(r'["«»“„\']', "", rf_text)
                                rf_text = re.sub(r"\bиз-за\b", "при", rf_text, flags=re.IGNORECASE)
                                rf_text = re.sub(
                                    r"\bв\s+результате\b", "после", rf_text, flags=re.IGNORECASE
                                )
                                rf_text = re.sub(
                                    r"\bпо\s+причине\b", "при", rf_text, flags=re.IGNORECASE
                                )
                                rf_text = re.sub(
                                    r"\bвследствие\b", "после", rf_text, flags=re.IGNORECASE
                                )
                                req_claims.append(
                                    {
                                        "text": rf_text,
                                        "covered_story_ids": list(
                                            set(rf.story_ids) & set(tb.story_ids)
                                        )
                                        or list(tb.story_ids[:1]),
                                        "cited_support_ids": list(rf.support_ids),
                                        "covered_fact_ids": [rf.fact_id],
                                    }
                                )
                            base_claim = {
                                "text": clean_text,
                                "covered_story_ids": list(tb.story_ids),
                                "cited_support_ids": sups,
                                "covered_fact_ids": [],
                            }
                            hl_cand = (
                                _headline_from_digest_fact(clean_text)
                                if clean_text
                                else f"{tb.topic_label}: ситуация в городе"
                            )
                            hl_cand = re.sub(r"\bиз-за\b", "при", hl_cand, flags=re.IGNORECASE)
                            hl_cand = re.sub(
                                r"\bв\s+результате\b", "после", hl_cand, flags=re.IGNORECASE
                            )
                            hl_cand = re.sub(
                                r"\bпо\s+причине\b", "при", hl_cand, flags=re.IGNORECASE
                            )
                            hl_cand = re.sub(
                                r"\bвследствие\b", "после", hl_cand, flags=re.IGNORECASE
                            )
                            if len(hl_cand) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                                hl_cand = (
                                    hl_cand[:DIGEST_ITEM_HEADLINE_MAX_CHARS]
                                    .rsplit(" ", 1)[0]
                                    .rstrip(".:;, ")
                                )
                            norm_items.append(
                                {
                                    "headline": hl_cand or f"{tb.topic_label}: ситуация в городе",
                                    "body": f"{clean_text[:1].upper() + clean_text[1:]}",
                                    "emoji": tb.emoji,
                                    "covered_story_ids": list(tb.story_ids),
                                    "cited_support_ids": sups,
                                    "claims": [base_claim] + req_claims,
                                }
                            )

                    b_raw["items"] = norm_items

                final_blocks.append(b_raw)

            parsed["blocks"] = final_blocks

        return DigestNarrativeDraft.from_dict(parsed)


def build_digest_support_text_index(
    *,
    evidence: Mapping[str, PublicationEvidence],
    cards: Sequence[StoryCard],
    frozen_input: Any | None = None,
) -> dict[str, str]:
    """Build unified mapping from support IDs and synthesized card IDs to exact support texts."""
    index: dict[str, str] = {}

    # 1. Primary PUBLISH evidence and its canonical source/fragment aliases.
    for eid, evi in evidence.items():
        if getattr(evi, "publication_use", "") != "PUBLISH":
            continue
        if getattr(evi, "text", None):
            index[eid] = evi.text
        elif getattr(evi, "source_text", None):
            index[eid] = evi.source_text
        text = str(getattr(evi, "text", "") or getattr(evi, "source_text", "") or "").strip()
        if text:
            for ref in (
                str(getattr(evi, "evidence_id", "") or "").strip(),
                str(getattr(evi, "source_ref", "") or "").strip(),
                (
                    f"fragment:{evi.fragment_id}"
                    if getattr(evi, "fragment_id", None) is not None
                    else ""
                ),
            ):
                if ref and ref not in index:
                    index[ref] = text

    # 2. Frozen input writer records if present
    if frozen_input is not None and getattr(frozen_input, "writer_bundle", None):
        records = getattr(frozen_input.writer_bundle, "records", {})
        if isinstance(records, dict):
            for ref, rec in records.items():
                msg = getattr(rec, "message", None)
                msg_text = getattr(msg, "text", "") if msg else ""
                if msg_text and ref not in index:
                    index[ref] = msg_text

    # 3. Card-level canonical notes and elements
    for c in cards:
        card_texts: list[str] = []
        if c.topic:
            card_texts.append(c.topic)
        if c.summary:
            card_texts.append(c.summary)
        for hf in c.hard_facts:
            if hf.text:
                card_texts.append(hf.text)
                for r in hf.source_refs:
                    if r not in index and hf.text:
                        index[r] = hf.text
        for ud in c.useful_details:
            if ud.text:
                card_texts.append(ud.text)
                for r in ud.source_refs:
                    if r not in index and ud.text:
                        index[r] = ud.text
        for co in c.community_observations:
            if co.text:
                card_texts.append(co.text)
                for r in co.source_refs:
                    if r not in index and co.text:
                        index[r] = co.text
        for obs in getattr(c, "operational_observations", []) or []:
            obs_text = getattr(obs, "text", "") or getattr(obs, "observation", "")
            if not obs_text and getattr(obs, "detail", None):
                obs_loc = getattr(obs, "location", "")
                obs_det = getattr(obs, "detail", "")
                obs_text = f"{obs_loc}: {obs_det}".strip(": ") if obs_loc else obs_det
            if obs_text:
                card_texts.append(obs_text)
                for r in getattr(obs, "source_refs", []) or []:
                    if r not in index and obs_text:
                        index[r] = obs_text
                for fid in getattr(obs, "source_fragment_ids", []) or []:
                    ref_fid = f"fragment:{fid}"
                    if ref_fid not in index and obs_text:
                        index[ref_fid] = obs_text

        if card_texts and c.id not in index:
            index[c.id] = " ".join(card_texts)
        if c.summary and f"{c.id}:summary" not in index:
            index[f"{c.id}:summary"] = c.summary
        if c.topic and f"{c.id}:topic" not in index:
            index[f"{c.id}:topic"] = c.topic

    return {key: _sanitize_digest_support_text(value) for key, value in index.items()}
