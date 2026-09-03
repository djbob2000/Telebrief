"""Narrow deterministic causal/mechanism relation validation for digests."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.publication.article_claims import _stem, normalize_support_text


@dataclass(frozen=True)
class DigestRelationViolation:
    raw: str
    cause: str
    effect: str
    reason: str = "UNSUPPORTED_DIGEST_RELATION"


# Causal connectors where the following clause/phrase is the cause or consequence
_CAUSAL_CONNECTOR_RE = re.compile(
    r"\b(?:из-за|вследствие|по причине|в результате|из-за чего|потому что|так как|в связи с чем|в связи с|вызвано|вызвана|вызван|вызваны|что привело к|повлекло)\s+([^,.;\n]+)",
    re.IGNORECASE,
)

# Mechanism verbs linking Subject/Cause -> Object/Effect
# e.g., "Авария на подстанции оставила Гору без света" -> Subject="Авария на подстанции", Verb="оставила", Object="Гору без света"
_MECHANISM_VERB_RE = re.compile(
    r"([^.;\n,]+?)\s+\b(оставил[аои]?|вызвал[аои]?|привел[аои]?\s+к|спровоцировал[аои]?)\s+([^.;\n]+)",
    re.IGNORECASE,
)

_STOPWORDS = frozenset(
    {
        "для",
        "при",
        "без",
        "под",
        "над",
        "из",
        "за",
        "на",
        "по",
        "со",
        "это",
        "как",
        "что",
        "где",
        "все",
        "всё",
        "его",
        "ее",
        "её",
        "их",
        "был",
        "была",
        "было",
        "были",
        "нет",
        "или",
        "так",
        "чем",
        "том",
        "тот",
        "эта",
        "этот",
        "эти",
        "дом",
        "дома",
    }
)


_RU_UK_LETTER_FOLD = str.maketrans(
    {
        "э": "е",
        "ё": "е",
        "і": "и",
        "ї": "и",
        "ы": "и",
        "ґ": "г",
    }
)

_GENERIC_CAUSE_WORDS = frozenset(
    {
        "проблема",
        "проблемы",
        "проблем",
        "проблемами",
        "проблемах",
        "отсутствие",
        "отсутствия",
        "отсутствием",
        "перебои",
        "перебоев",
        "перебоями",
        "перебоях",
        "трудности",
        "трудностей",
        "сложности",
        "сложностей",
        "ситуация",
        "ситуации",
        "ситуацией",
        "вопрос",
        "вопросы",
    }
)


def _stem_bilingual(w: str) -> str:
    folded = w.translate(_RU_UK_LETTER_FOLD)
    if folded.startswith("електр"):
        return "електр"
    if folded.startswith("интернет"):
        return "интернет"
    if folded.startswith("звяз") or folded.startswith("связ"):
        return "связ"
    if folded.startswith("свитл") or folded.startswith("свет"):
        return "свет"
    return _stem(folded)


def _extract_content_stems(phrase: str) -> list[str]:
    words = re.findall(r"[a-zа-я0-9']+", phrase.casefold())
    return [
        _stem_bilingual(w)
        for w in words
        if len(w) > 2 and w not in _STOPWORDS and w not in _GENERIC_CAUSE_WORDS
    ]


def find_unsupported_digest_relations(
    text: str,
    support_texts: Sequence[str],
) -> list[DigestRelationViolation]:
    """Identify unsupported causal relations, mechanisms, and invented causes in digest text."""
    if not text or not support_texts:
        return []

    norm_supports = [normalize_support_text(st) for st in support_texts if st]
    if not norm_supports:
        return []

    norm_supports_stems = [
        [_stem_bilingual(w) for w in re.findall(r"[a-zа-я0-9']+", st.casefold())]
        for st in support_texts
        if st
    ]

    def _is_stem_supported(stem: str) -> bool:
        for sup, sup_stems in zip(norm_supports, norm_supports_stems, strict=False):
            if stem in sup:
                return True
            if any(stem in sw or sw in stem for sw in sup_stems):
                return True
        return False

    violations: list[DigestRelationViolation] = []

    # 1. Check mechanism verb relations: "X оставил Y без света", "X вызвал Y"
    for m in _MECHANISM_VERB_RE.finditer(text):
        subject_cause = m.group(1).strip()
        _verb = m.group(2).strip()
        object_effect = m.group(3).strip()
        raw_match = m.group(0).strip()

        cause_stems = _extract_content_stems(subject_cause)
        if not cause_stems:
            continue

        # Check if the cause content stems are supported in any support text
        cause_supported = all(_is_stem_supported(stem) for stem in cause_stems)
        if not cause_supported:
            violations.append(
                DigestRelationViolation(
                    raw=raw_match,
                    cause=subject_cause,
                    effect=object_effect,
                    reason="UNSUPPORTED_DIGEST_RELATION",
                )
            )

    # 2. Check causal connectors: "... из-за X", "... вследствие X"
    for m in _CAUSAL_CONNECTOR_RE.finditer(text):
        raw_match = m.group(0).strip()
        cause_clause = m.group(1).strip()

        cause_stems = _extract_content_stems(cause_clause)
        if not cause_stems:
            continue

        cause_supported = all(_is_stem_supported(stem) for stem in cause_stems)
        if not cause_supported:
            violations.append(
                DigestRelationViolation(
                    raw=raw_match,
                    cause=cause_clause,
                    effect="",
                    reason="UNSUPPORTED_DIGEST_RELATION",
                )
            )

    return violations
