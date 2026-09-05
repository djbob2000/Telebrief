"""Deterministic lexical and support assessment for model-declared claim atoms."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
    cleaned = (
        text.casefold()
        .replace("ё", "е")
        .replace("і", "и")
        .replace("ї", "и")
        .replace("є", "е")
        .replace("ґ", "г")
    )
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
    blocking_critical_terms: tuple[str, ...] = ()
    unmatched_proper_names: tuple[str, ...] = ()
    blocking_proper_names: tuple[str, ...] = ()
    lexical_only_warning: bool = False
    causal_analysis: Any | None = None


def assess_claim_against_supports(
    claim_text: str,
    supports: Sequence[ArticleSupport],
    *,
    min_content_coverage: float = 0.50,
    allowed_context_terms: Sequence[str] = (),
    all_known_draft_supports: Sequence[str] = (),
    direct_quote_allowlist: Sequence[str] | None = None,
) -> ClaimSupportAssessment:
    """Conservatively assess whether claim_text is substantiated by cited supports."""
    support_texts: list[str] = []
    for s in supports:
        if s.text:
            support_texts.append(s.text)
        if s.source_text:
            support_texts.append(s.source_text)
        if (obs := getattr(s, "observed_at", None)) is not None:
            support_texts.append(obs.strftime("%H:%M"))
            support_texts.append(obs.strftime("%-H:%M"))

    # 1. High-risk concrete claims (numbers, dates, times, money, phone, cause, etc.)
    primary_source_texts = [s.source_text for s in supports if s.source_text]
    unsupported_concrete = find_unsupported_claims(
        claim_text,
        support_texts,
        allowed_context_terms=allowed_context_terms,
        all_known_draft_supports=all_known_draft_supports,
        direct_quote_source_texts=primary_source_texts,
        direct_quote_allowlist=direct_quote_allowlist,
    )

    # 2. Risk-based semantic novelty and proper name matching
    semantic = assess_semantic_support(
        claim_text,
        support_texts,
        allowed_context_terms=allowed_context_terms,
    )

    unsupported_scope_phrases = (
        "на ряде улиц",
        "в ряде районов",
        "по ряду улиц",
        "на многих улицах",
        "во многих районах",
        "по всему городу",
    )
    has_unsupported_scope = any(
        phrase in claim_text.lower() and not any(phrase in st.lower() for st in support_texts)
        for phrase in unsupported_scope_phrases
    )

    has_unsupported_semantic_addition = bool(semantic.blocking_terms) and (
        semantic.lexical_coverage < min_content_coverage or has_unsupported_scope
    )

    # Hard Evidence Boundary: only concrete claims (numbers/dates/causality/quotes),
    # critical domain mismatch (power/water/gas), proper name hallucinations, and
    # ungrounded semantic additions (coverage < threshold or unsupported geographic scope) block.
    # General lexical vocabulary divergence on well-supported claims is non-blocking (lexical_only_warning).
    blocking = (
        bool(unsupported_concrete)
        or has_unsupported_semantic_addition
        or bool(semantic.blocking_critical_terms)
        or bool(semantic.blocking_proper_names)
    )

    supported = not blocking
    lexical_only_warning = (
        not blocking
        and bool(semantic.unmatched_terms)
        and semantic.lexical_coverage < min_content_coverage
    )

    blocking_semantic = semantic.blocking_terms if has_unsupported_semantic_addition else ()

    return ClaimSupportAssessment(
        supported=supported,
        content_coverage=semantic.lexical_coverage,
        unsupported_content_stems=semantic.unmatched_terms,
        unsupported_concrete_claims=unsupported_concrete,
        blocking_semantic_terms=blocking_semantic,
        blocking_critical_terms=semantic.blocking_critical_terms,
        unmatched_proper_names=semantic.unmatched_proper_names,
        blocking_proper_names=semantic.blocking_proper_names,
        lexical_only_warning=lexical_only_warning,
    )
