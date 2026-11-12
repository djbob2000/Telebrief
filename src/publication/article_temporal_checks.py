"""Temporal fidelity of Event-First article prose (AGENTS.md §0.6, §0.7; article-fidelity W1).

Three deterministic checks and one writer rule set:

- ``HISTORICAL_PRESENTED_AS_CURRENT`` (factual, blocking): a sentence that is supported only
  by past material (``temporal_role = HISTORICAL_CONTEXT``) but speaks in the present tense
  («сегодня», «сейчас», «только что», «уже N дней» without a date, «к вечеру» of the article day).
- ``RELATIVE_TIME_MISDATED`` (factual, blocking): «сегодня» or «вчера» whose cited support was
  not sent on the day the word points to in the article.
- ``RELATIVE_TIME_COPIED`` (reader quality, repair): «N минут назад» or «только что» copied from chat.
- ``REDUNDANT_PARAGRAPH`` (reader quality, repair): a paragraph that restates the previous one.

Blocking findings name their paragraph unit and the flagged sentence as ``claim_text``, so the
existing ArticleEditor and the one-sentence backstop act on them unchanged. Quotations are never
edited: their words are excluded from every check.

Pure module: it reads article objects through duck typing and imports nothing heavy at load time.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext, ArticleSupport
    from src.publication.article_models import ArticleClaimAtom, StructuredArticleDraft

HISTORICAL_PRESENTED_AS_CURRENT = "HISTORICAL_PRESENTED_AS_CURRENT"
RELATIVE_TIME_MISDATED = "RELATIVE_TIME_MISDATED"
RELATIVE_TIME_COPIED = "RELATIVE_TIME_COPIED"
REDUNDANT_PARAGRAPH = "REDUNDANT_PARAGRAPH"

# Blocking codes that the ArticleEditor sentence backstop may resolve by removing one sentence.
TEMPORAL_SENTENCE_TRIM_CODES: frozenset[str] = frozenset(
    {HISTORICAL_PRESENTED_AS_CURRENT, RELATIVE_TIME_MISDATED}
)

ARTICLE_TEMPORAL_WRITER_RULES = """
9. Time references and historical material:
- Read each support's sent_at (edition-local). A support sent on an earlier day is past news: introduce it with its own date («11 ноября, по словам жительницы, свет дали с 11 до 15»), never as today's news.
- Never write «сегодня», «сейчас», «только что», «на данный момент», «к вечеру» with the article's day, or «уже N дней» without a date, for a support that is only historical in the REPORT WINDOW.
- Relative words in a source («сегодня», «вчера», «завтра», «N минут/часов назад») are resolved against that source's sent_at. Write the date or the time of day the report refers to; never copy the chat word into the article.
- «Сегодня» is correct only for a support sent on the article's day; «вчера» only for one sent the day before, or for a source saying «вчера» on the day it was sent.
- Do not repeat a paragraph's facts in the next paragraph under different wording; merge them into one passage.
"""

_QUOTE_SPAN_RE = re.compile(r"«[^»]*»|\"[^\"]*\"")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_ABBREVIATION_DOT_RE = re.compile(r"\b(м|вул|ул|г|обл|р-н|с|пр)\.")
_TOKEN_RE = re.compile(r"[а-яa-z0-9]{3,}")

_PRESENT_MARKER_RE = re.compile(
    r"\b(?:сегодня|сейчас|только\s+что|на\s+данный\s+момент)\b", re.IGNORECASE
)
_TODAY_RE = re.compile(r"\bсегодня\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"\bвчера\b", re.IGNORECASE)
_EVENING_RE = re.compile(r"\bк\s+вечеру\b", re.IGNORECASE)
_ALREADY_DURATION_RE = re.compile(
    r"\bуже\s+(?:\d+|[а-яё]+)\s+(?:дн\w*|сут\w*|недел\w*|месяц\w*)\b", re.IGNORECASE
)
_DATED_RE = re.compile(
    r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
    r"|\b\d{1,2}\.\d{1,2}\b",
    re.IGNORECASE,
)
# Relative elapsed-time phrase copied from a chat («пять минут назад», «2 часа назад», «час назад»).
_ELAPSED_RELATIVE_RE = re.compile(
    r"\b(?:(?:\d+|[а-яё]+)\s+)?(?:минут\w*|час\w*|секунд\w*)\s+назад\b",
    re.IGNORECASE,
)

_MONTHS_RU = (
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


@dataclass(frozen=True)
class TemporalFinding:
    """A temporal finding, independent of the validator and quality types it is reported as."""

    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...] = ()
    claim_text: str | None = None


def _split_sentences(text: str) -> tuple[str, ...]:
    protected = _ABBREVIATION_DOT_RE.sub(lambda match: match.group(1) + "\u0000", text or "")
    parts = (part.replace("\u0000", ".").strip() for part in _SENTENCE_SPLIT_RE.split(protected))
    return tuple(part for part in parts if part)


def _unquoted(text: str) -> str:
    return _QUOTE_SPAN_RE.sub(" ", text)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.casefold().replace("ё", "е")))


def _local_date(value: dt.datetime, timezone_name: str) -> dt.date:
    """Edition-local calendar day of a stored timestamp (UTC when the zone is unknown)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    try:
        from src.timezones import get_timezone, normalize_timezone_name

        zone: dt.tzinfo = get_timezone(normalize_timezone_name(timezone_name))
    except (ValueError, ImportError):
        zone = dt.timezone.utc
    return value.astimezone(zone).date()


def article_local_day(context: ArticleEditorialContext) -> dt.date | None:
    """The article's own day: the as-of time of its report window in the edition zone."""
    window = getattr(context, "publication_window", None)
    if window is None:
        return None
    return _local_date(window.snapshot_at, getattr(context, "edition_timezone", "UTC") or "UTC")


def support_local_day(support: ArticleSupport, timezone_name: str) -> dt.date | None:
    """Day the source was sent (falls back to the collection-side observation time)."""
    value = support.sent_at or support.observed_at
    if value is None:
        return None
    return _local_date(value, timezone_name)


def _has_present_marker(plain: str, article_day: dt.date | None) -> bool:
    if _PRESENT_MARKER_RE.search(plain):
        return True
    if _ALREADY_DURATION_RE.search(plain) and not _DATED_RE.search(plain):
        return True
    evening = _EVENING_RE.search(plain)
    if evening is not None and article_day is not None:
        tail = plain[evening.end() : evening.end() + 40]
        return re.search(rf"\b{article_day.day}\b", tail) is not None
    return False


def _sentence_support_ids(
    sentence: str,
    claim_atoms: Sequence[ArticleClaimAtom],
    cited_ids: Sequence[str],
) -> tuple[str, ...]:
    """Supports of the claim atoms that the sentence restates; the unit's citations otherwise."""
    sentence_tokens = _tokens(sentence)
    ids: list[str] = []
    for atom in claim_atoms:
        atom_tokens = _tokens(atom.text)
        if atom_tokens and len(atom_tokens & sentence_tokens) / len(atom_tokens) >= 0.5:
            ids.extend(atom.cited_support_ids)
    return tuple(dict.fromkeys(ids)) or tuple(cited_ids)


def temporal_prose_findings(
    *,
    unit_id: str,
    unit_type: str,
    unit_text: str,
    cited_ids: Sequence[str],
    claim_atoms: Sequence[ArticleClaimAtom],
    support_map: Mapping[str, ArticleSupport],
    context: ArticleEditorialContext,
) -> tuple[TemporalFinding, ...]:
    """Blocking temporal findings for one lead or paragraph unit (one per flagged sentence)."""
    if unit_type not in ("lead", "paragraph") or not unit_text.strip():
        return ()
    if not any(sid in support_map for sid in cited_ids):
        return ()

    timezone_name = getattr(context, "edition_timezone", "UTC") or "UTC"
    article_day = article_local_day(context)
    findings: list[TemporalFinding] = []
    for sentence in _split_sentences(unit_text):
        plain = _unquoted(sentence)
        sentence_ids = _sentence_support_ids(sentence, claim_atoms, cited_ids)
        supports = [support_map[sid] for sid in sentence_ids if sid in support_map]
        if not supports:
            continue
        if (
            unit_type == "paragraph"
            and _has_present_marker(plain, article_day)
            and all(s.temporal_role == "HISTORICAL_CONTEXT" for s in supports)
        ):
            findings.append(
                TemporalFinding(
                    code=HISTORICAL_PRESENTED_AS_CURRENT,
                    unit_id=unit_id,
                    message=(
                        f"Unit {unit_id} presents historical material as current news; "
                        "introduce it with its own date instead of a present-time word"
                    ),
                    support_ids=sentence_ids,
                    claim_text=sentence,
                )
            )
            continue
        if article_day is None:
            continue
        days = [
            day
            for day in (support_local_day(s, timezone_name) for s in supports)
            if day is not None
        ]
        if not days:
            continue
        if _TODAY_RE.search(plain) and article_day not in days:
            findings.append(
                _misdated(unit_id, sentence, sentence_ids, "«сегодня»", days, article_day)
            )
            continue
        if _YESTERDAY_RE.search(plain):
            yesterday = article_day - dt.timedelta(days=1)
            said_yesterday_on_article_day = any(
                support_local_day(s, timezone_name) == article_day
                and "вчера" in f"{s.text} {s.source_text}".casefold()
                for s in supports
            )
            if yesterday not in days and not said_yesterday_on_article_day:
                findings.append(
                    _misdated(unit_id, sentence, sentence_ids, "«вчера»", days, article_day)
                )
    return tuple(findings)


def _misdated(
    unit_id: str,
    sentence: str,
    support_ids: Sequence[str],
    word: str,
    days: Sequence[dt.date],
    article_day: dt.date,
) -> TemporalFinding:
    sent_days = ", ".join(sorted({day.isoformat() for day in days}))
    return TemporalFinding(
        code=RELATIVE_TIME_MISDATED,
        unit_id=unit_id,
        message=(
            f"Unit {unit_id} uses the relative word {word} for {article_day.isoformat()}, "
            f"but the cited material was sent on {sent_days}; write the date instead"
        ),
        support_ids=tuple(support_ids),
        claim_text=sentence,
    )


def _paragraph_units(draft: StructuredArticleDraft) -> list[tuple[str, Any]]:
    units: list[tuple[str, Any]] = []
    p_idx = 1
    for section in draft.sections:
        for paragraph in section.paragraphs:
            units.append((f"P{p_idx:03d}", paragraph))
            p_idx += 1
    return units


def _paragraph_support_ids(paragraph: Any, context: ArticleEditorialContext) -> frozenset[str]:
    ids = list(paragraph.cited_support_ids)
    for claim in paragraph.claims:
        ids.extend(claim.cited_support_ids)
    return frozenset(sid for sid in ids if sid in context.support_by_id)


def temporal_quality_findings(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
) -> tuple[TemporalFinding, ...]:
    """Repair-level reader findings: copied elapsed time and a paragraph that restates the previous one."""
    from src.publication.article_claim_support import extract_content_stems

    findings: list[TemporalFinding] = []
    previous: tuple[frozenset[str], str] | None = None
    for unit_id, paragraph in _paragraph_units(draft):
        text = str(paragraph.text)
        plain = _unquoted(text)
        elapsed = _ELAPSED_RELATIVE_RE.search(plain)
        if elapsed is not None:
            findings.append(
                TemporalFinding(
                    code=RELATIVE_TIME_COPIED,
                    unit_id=unit_id,
                    message=(
                        f"Unit {unit_id} copies the chat's relative time («{elapsed.group(0)}»); "
                        "write the date or the time of day instead"
                    ),
                    support_ids=tuple(sorted(_paragraph_support_ids(paragraph, context))),
                )
            )
        support_ids = _paragraph_support_ids(paragraph, context)
        if previous is not None and support_ids and support_ids <= previous[0]:
            stems = extract_content_stems(text)
            previous_stems = extract_content_stems(previous[1])
            if stems and len(stems & previous_stems) / len(stems) >= 0.8:
                findings.append(
                    TemporalFinding(
                        code=REDUNDANT_PARAGRAPH,
                        unit_id=unit_id,
                        message=(
                            f"Unit {unit_id} restates the previous paragraph with the same "
                            "supports; merge the two passages"
                        ),
                        support_ids=tuple(sorted(support_ids)),
                    )
                )
        previous = (support_ids, text)
    return tuple(findings)
