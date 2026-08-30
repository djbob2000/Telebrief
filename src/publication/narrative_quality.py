"""Diagnostic metrics for editorial prose cohesion, synthesis, and narrative style.

Observability-only module: these metrics are diagnostics for comparative benchmarking
and A/B evaluation, and are NEVER used as hard publication truth gates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.publication.article_models import StructuredArticleDraft

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯA-Z«\"])")
_REPEATED_ATTRIBUTION_START_RE = re.compile(
    r"^(?:по\s+(?:сообщениям|данным|информации|словам|сведениям|сводкам)|жители\s+(?:сообщают|жалуются|отмечают|пишут)|как\s+(?:сообщают|стало\s+известно|передают))\b",
    re.IGNORECASE,
)
_DATABASE_LABEL_RE = re.compile(
    r"^[А-Яа-яA-Za-z0-9\s_-]+(?:\s*\([^)]+\))?\s*:\s+[А-Яа-яA-Za-z]",
)
_DIRECT_QUOTE_RE = re.compile(r"«[^»]+»|\"[^\"]+\"")


@dataclass(frozen=True)
class NarrativeQualityReport:
    """Prose quality and synthesis diagnostic report for an editorial draft."""

    paragraph_count: int
    multi_support_paragraph_count: int
    repeated_attribution_starts: int
    database_label_patterns: int
    direct_quote_count: int
    avg_sentences_per_paragraph: float

    @property
    def synthesis_ratio(self) -> float:
        """Ratio of paragraphs synthesizing 2 or more distinct support items."""
        if self.paragraph_count == 0:
            return 0.0
        return self.multi_support_paragraph_count / self.paragraph_count


def evaluate_article_narrative(draft: StructuredArticleDraft) -> NarrativeQualityReport:
    """Compute deterministic diagnostic quality metrics on a structured article draft."""
    paragraphs: list[str] = []
    multi_support_count = 0
    direct_quotes = 0
    attribution_starts = 0
    db_labels = 0
    total_sentences = 0

    for section in draft.sections:
        for para in section.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            paragraphs.append(text)

            distinct_supports = set(para.cited_support_ids)
            if len(distinct_supports) >= 2:
                multi_support_count += 1

            if _DATABASE_LABEL_RE.search(text):
                db_labels += 1

            # Check quotes
            quotes = _DIRECT_QUOTE_RE.findall(text)
            direct_quotes += len(quotes)

            # Check sentence splits & sentence-level attribution starts
            raw_sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
            total_sentences += max(1, len(raw_sentences))

            for sent in raw_sentences:
                if _REPEATED_ATTRIBUTION_START_RE.search(sent):
                    attribution_starts += 1

    para_count = len(paragraphs)
    avg_sentences = (total_sentences / para_count) if para_count > 0 else 0.0

    return NarrativeQualityReport(
        paragraph_count=para_count,
        multi_support_paragraph_count=multi_support_count,
        repeated_attribution_starts=attribution_starts,
        database_label_patterns=db_labels,
        direct_quote_count=direct_quotes,
        avg_sentences_per_paragraph=round(avg_sentences, 2),
    )
