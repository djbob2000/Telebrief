"""Deterministic lexical and support assessment for model-declared claim atoms."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.publication.article_claims import (
    ConcreteClaim,
    find_unsupported_claims,
    stem_word,
)
from src.publication.article_context import ArticleSupport

# Strict stopword list: grammatical / connective words only.
# Domain nouns (e.g. генератор, вода, связь, заявка, дефицит, подвоз, свет) MUST NOT be included.
_STOPWORDS = frozenset(
    {
        "и",
        "в",
        "во",
        "не",
        "на",
        "с",
        "со",
        "по",
        "к",
        "ко",
        "у",
        "о",
        "об",
        "обо",
        "от",
        "ото",
        "до",
        "из",
        "изо",
        "за",
        "над",
        "надо",
        "под",
        "подо",
        "при",
        "про",
        "через",
        "для",
        "без",
        "безо",
        "а",
        "но",
        "да",
        "или",
        "либо",
        "то",
        "что",
        "чтобы",
        "как",
        "так",
        "если",
        "хотя",
        "когда",
        "где",
        "куда",
        "откуда",
        "почему",
        "зачем",
        "же",
        "ли",
        "бы",
        "только",
        "уже",
        "еще",
        "ещё",
        "все",
        "всё",
        "это",
        "эта",
        "этот",
        "эти",
        "этом",
        "этой",
        "этих",
        "тот",
        "та",
        "те",
        "том",
        "той",
        "тех",
        "он",
        "она",
        "оно",
        "они",
        "его",
        "ее",
        "её",
        "их",
        "ему",
        "ей",
        "им",
        "нем",
        "нём",
        "ней",
        "них",
        "мы",
        "вы",
        "я",
        "ты",
        "нас",
        "вас",
        "меня",
        "тебя",
        "нам",
        "вам",
        "мне",
        "тебе",
        "собой",
        "собою",
        "свой",
        "своя",
        "свое",
        "своё",
        "свои",
        "своих",
        "своим",
        "был",
        "была",
        "было",
        "были",
        "быть",
        "будет",
        "будут",
        "есть",
        "является",
        "являются",
        "также",
        "тоже",
        "очень",
        "даже",
        "вдруг",
        "между",
        "после",
        "перед",
        "около",
        "вокруг",
        "такой",
        "такая",
        "такое",
        "такие",
        "который",
        "которая",
        "которое",
        "которые",
        # Reporting and temporal discourse markers
        "сохраняется",
        "сохраняются",
        "продолжается",
        "продолжаются",
        "сообщается",
        "сообщают",
        "сообщили",
        "сообщил",
        "отмечается",
        "отмечают",
        "отметили",
        "наблюдается",
        "наблюдаются",
        "зафиксировано",
        "ранее",
        "по-прежнему",
        "прежде",
        "снова",
        "вновь",
        "опять",
        "сейчас",
        "ныне",
        "сегодня",
        "накануне",
        # Ukrainian basic grammatical words
        "чи",
        "що",
        "як",
        "проте",
        "але",
        "від",
        "зі",
        "із",
        "щодо",
        "він",
        "вона",
        "вони",
        "його",
        "її",
        "їх",
        "йому",
        "їй",
        "їм",
        "нами",
        "вами",
        "ними",
        "був",
        "була",
        "було",
        "були",
        "буде",
        "будуть",
        "є",
        "це",
        "цей",
        "ця",
        "ці",
    }
)


def extract_content_stems(text: str) -> set[str]:
    """Extract normalized, non-stopword content stems from Russian/Ukrainian text."""
    if not text:
        return set()
    cleaned = text.lower().replace("ё", "е")
    tokens = re.findall(r"[a-zа-я0-9]+", cleaned)
    stems: set[str] = set()
    for tok in tokens:
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        stems.add(stem_word(tok))
    return stems


@dataclass(frozen=True)
class ClaimSupportAssessment:
    """Detailed result of assessing a claim atom against cited ArticleSupport packets."""

    supported: bool
    content_coverage: float
    unsupported_content_stems: tuple[str, ...]
    unsupported_concrete_claims: tuple[ConcreteClaim, ...]


def assess_claim_against_supports(
    claim_text: str,
    supports: Sequence[ArticleSupport],
    *,
    min_content_coverage: float = 0.70,
) -> ClaimSupportAssessment:
    """Conservatively assess whether claim_text is substantiated by cited supports."""
    support_texts: list[str] = []
    for s in supports:
        if s.text:
            support_texts.append(s.text)
        if s.source_text:
            support_texts.append(s.source_text)

    # 1. High-risk concrete claims (numbers, dates, times, money, phone, cause, etc.)
    unsupported_concrete = find_unsupported_claims(claim_text, support_texts)

    # 2. Extract content stems
    claim_stems = extract_content_stems(claim_text)
    support_stems: set[str] = set()
    for st in support_texts:
        support_stems.update(extract_content_stems(st))

    if not claim_stems:
        coverage = 1.0
        missing_stems: tuple[str, ...] = ()
        is_coverage_ok = True
    else:
        matched_stems = claim_stems.intersection(support_stems)
        missing_set = claim_stems - support_stems
        missing_stems = tuple(sorted(missing_set))
        coverage = len(matched_stems) / len(claim_stems)

        if len(claim_stems) in (1, 2):
            is_coverage_ok = len(missing_set) == 0
        else:
            is_coverage_ok = coverage >= min_content_coverage

    supported = (len(unsupported_concrete) == 0) and is_coverage_ok

    return ClaimSupportAssessment(
        supported=supported,
        content_coverage=coverage,
        unsupported_content_stems=missing_stems,
        unsupported_concrete_claims=unsupported_concrete,
    )
