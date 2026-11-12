"""Source-faithfulness reader-quality checks for article prose (article-fidelity W7.1, W7.3).

Both checks are deterministic and non-blocking (``repair``). They compare the
prose with the cited sources, and they judge the source message itself, not the
Gate paraphrase: a Gate fact may inflate «4 неделю» into «четыре недели» (the
source form stays authoritative), and a Gate fact may drop an hedge the message
kept.

- ``ORDINAL_DURATION_INFLATED`` (W7.1): prose states a cardinal week or month
  count («четыре недели», «4 месяца») while every cited source states only the
  ordinal form («4 неделю», «четвертая неделя»). Days are exempt.
- ``HEDGE_DROPPED`` (W7.3): a unit cites one support only, whose source carries a
  hedge or hearsay marker («похоже», «говорят»), and the prose has no
  uncertainty marker. Plain attribution («по словам жителя») is not a hedge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.publication.article_context import ArticleSupport
from src.publication.article_models import StructuredArticleDraft

ORDINAL_DURATION_INFLATED = "ORDINAL_DURATION_INFLATED"
HEDGE_DROPPED = "HEDGE_DROPPED"

_CARDINAL_WORDS: dict[int, tuple[str, ...]] = {
    2: ("два", "две", "двух", "двум"),
    3: ("три", "трех", "трёх", "трем"),
    4: ("четыре", "четырех", "четырёх"),
    5: ("пять", "пяти"),
    6: ("шесть", "шести"),
    7: ("семь", "семи"),
    8: ("восемь", "восьми"),
    9: ("девять", "девяти"),
    10: ("десять", "десяти"),
}
_ORDINAL_WORD_STEMS: dict[int, str] = {
    2: "втор",
    3: "трет",
    4: "четв[её]рт",
    5: "пят",
    6: "шест",
    7: "седьм",
    8: "восьм",
    9: "девят",
    10: "десят",
}
_CARDINAL_NOUN = r"(?:недел(?:и|ь)|месяц(?:а|ев))"
_SINGULAR_ORDINAL_NOUN = r"(?:неделю|неделя|неделе|месяц)"
_ORDINAL_NOUN = r"(?:недел\w*|месяц\w*)"

_QUOTED_RE = re.compile(r"«[^»]*»|\"[^\"]*\"|“[^”]*”")
_SOURCE_HEDGE_RE = re.compile(
    r"\b(?:похоже|кажется|вроде|видимо|наверное|говорят|по слухам|со слов)\b",
    re.IGNORECASE,
)
_PROSE_HEDGE_RE = re.compile(
    r"\b(?:похоже|по-видимому|видимо|возможно|вероятно|по слухам|якобы|кажется|со слов"
    r"|по словам соседей)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceCheckFinding:
    """A reader-quality finding before it is converted to the shared quality report."""

    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...]


def _cardinal_prose_counts(text: str) -> set[int]:
    """Week or month counts stated as cardinals in prose outside direct quotes."""
    prose = _QUOTED_RE.sub(" ", text)
    counts: set[int] = set()
    for number in _CARDINAL_WORDS:
        digits = re.search(rf"(?<![\d.,:])\b{number}\s+{_CARDINAL_NOUN}\b", prose, re.IGNORECASE)
        words = "|".join(_CARDINAL_WORDS[number])
        word = re.search(rf"\b(?:{words})\s+{_CARDINAL_NOUN}\b", prose, re.IGNORECASE)
        if digits or word:
            counts.add(number)
    return counts


def _source_duration_forms(text: str, number: int) -> tuple[bool, bool]:
    """(has_cardinal, has_ordinal) for week or month counts of ``number`` in a source."""
    lowered = text.casefold().replace("ё", "е")
    words = "|".join(_CARDINAL_WORDS[number])
    cardinal = (
        re.search(rf"(?<![\d.,:])\b{number}\s+{_CARDINAL_NOUN}\b", lowered) is not None
        or re.search(rf"\b(?:{words})\s+{_CARDINAL_NOUN}\b", lowered) is not None
    )
    ordinal = (
        re.search(
            rf"(?<![\d.,:])\b{number}\s*-\s*(?:ю|я|й|го|е|ая|ую|ой)\s*{_ORDINAL_NOUN}",
            lowered,
        )
        is not None
        or re.search(rf"(?<![\d.,:])\b{number}\s+{_SINGULAR_ORDINAL_NOUN}\b", lowered) is not None
        or re.search(
            rf"\b{_ORDINAL_WORD_STEMS[number]}\w*\s+{_ORDINAL_NOUN}",
            lowered,
        )
        is not None
    )
    return cardinal, ordinal


def _unit_support_ids(cited: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(cited))


def _ordinal_duration_findings(
    unit_id: str,
    unit_text: str,
    cited_ids: Sequence[str],
    support_by_id: Mapping[str, ArticleSupport],
) -> list[EvidenceCheckFinding]:
    findings: list[EvidenceCheckFinding] = []
    supports = [support_by_id[sid] for sid in cited_ids if sid in support_by_id]
    if not supports:
        return findings
    for number in sorted(_cardinal_prose_counts(unit_text)):
        ordinal_ids: list[str] = []
        cardinal_found = False
        for support in supports:
            # The source message is the authority; the Gate fact may paraphrase it.
            source = support.source_text or support.text
            has_cardinal, has_ordinal = _source_duration_forms(source, number)
            if has_cardinal:
                cardinal_found = True
            if has_ordinal:
                ordinal_ids.append(support.support_id)
        if ordinal_ids and not cardinal_found:
            findings.append(
                EvidenceCheckFinding(
                    code=ORDINAL_DURATION_INFLATED,
                    unit_id=unit_id,
                    message=(
                        f"В сюжете указана количественная длительность ({number}), а источник "
                        "называет порядковую неделю или месяц; напишите «идёт N-я неделя» или "
                        "сохраните порядковую форму."
                    ),
                    support_ids=tuple(ordinal_ids),
                )
            )
    return findings


def _hedge_findings(
    unit_id: str,
    unit_text: str,
    cited_ids: Sequence[str],
    support_by_id: Mapping[str, ArticleSupport],
) -> list[EvidenceCheckFinding]:
    unique_ids = _unit_support_ids(cited_ids)
    if len(unique_ids) != 1:
        return []
    support = support_by_id.get(unique_ids[0])
    if support is None:
        return []
    source = support.source_text or support.text
    if not _SOURCE_HEDGE_RE.search(source):
        return []
    if _PROSE_HEDGE_RE.search(unit_text):
        return []
    return [
        EvidenceCheckFinding(
            code=HEDGE_DROPPED,
            unit_id=unit_id,
            message=(
                "Источник передаёт оговорку или пересказ с чужих слов, а текст подаёт "
                "сообщение как установленный факт; сохраните оговорку."
            ),
            support_ids=(support.support_id,),
        )
    ]


def diagnose_evidence_faithfulness(
    units: Sequence[tuple[str, str, Sequence[str]]],
    support_by_id: Mapping[str, ArticleSupport],
) -> tuple[EvidenceCheckFinding, ...]:
    """Run W7.1 and W7.3 over ``(unit_id, prose, cited_support_ids)`` tuples."""
    findings: list[EvidenceCheckFinding] = []
    for unit_id, unit_text, cited_ids in units:
        if not unit_text.strip() or not cited_ids:
            continue
        findings.extend(_ordinal_duration_findings(unit_id, unit_text, cited_ids, support_by_id))
        findings.extend(_hedge_findings(unit_id, unit_text, cited_ids, support_by_id))
    return tuple(findings)


def evidence_units_for_draft(
    draft: StructuredArticleDraft,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Lead and paragraph units with every support their claims cite (positional IDs)."""
    units: list[tuple[str, str, tuple[str, ...]]] = [
        (
            "LEAD",
            draft.lead,
            tuple(
                dict.fromkeys(
                    (
                        *draft.lead_support_ids,
                        *(sid for claim in draft.lead_claims for sid in claim.cited_support_ids),
                    )
                )
            ),
        )
    ]
    paragraph_index = 1
    for section in draft.sections:
        for paragraph in section.paragraphs:
            cited = tuple(
                dict.fromkeys(
                    (
                        *paragraph.cited_support_ids,
                        *(sid for claim in paragraph.claims for sid in claim.cited_support_ids),
                    )
                )
            )
            units.append((f"P{paragraph_index:03d}", paragraph.text, cited))
            paragraph_index += 1
    return tuple(units)
