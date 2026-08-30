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
from src.publication.article_semantic_support import (
    _STOPWORDS,
    assess_semantic_support,
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
    blocking_semantic_terms: tuple[str, ...] = ()
    unmatched_proper_names: tuple[str, ...] = ()
    blocking_proper_names: tuple[str, ...] = ()
    lexical_only_warning: bool = False


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

    # 2. Risk-based semantic novelty and proper name matching
    semantic = assess_semantic_support(claim_text, support_texts)
    blocking = (
        bool(unsupported_concrete)
        or bool(semantic.blocking_terms)
        or bool(semantic.blocking_proper_names)
    )

    supported = not blocking
    lexical_only_warning = (
        not blocking
        and bool(semantic.unmatched_terms)
        and semantic.lexical_coverage < min_content_coverage
    )

    return ClaimSupportAssessment(
        supported=supported,
        content_coverage=semantic.lexical_coverage,
        unsupported_content_stems=semantic.unmatched_terms,
        unsupported_concrete_claims=unsupported_concrete,
        blocking_semantic_terms=semantic.blocking_terms,
        unmatched_proper_names=semantic.unmatched_proper_names,
        blocking_proper_names=semantic.blocking_proper_names,
        lexical_only_warning=lexical_only_warning,
    )
