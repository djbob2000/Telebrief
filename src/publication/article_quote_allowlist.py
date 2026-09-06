from __future__ import annotations

from typing import TYPE_CHECKING

from src.publication.article_claims import _quote_tokens_match, quote_words
from src.publication.article_writer_context import sanitize_writer_source_text

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext

_TRIVIAL_WORDS = frozenset({"да", "нет", "ок", "не", "хорошо", "точно", "плюс", "плюсую"})


def build_article_quote_allowlist(
    context: ArticleEditorialContext,
) -> tuple[str, ...]:
    """Extract verifiable verbatim primary-source snippets eligible for direct quotation marks.

    Rules:
    - support.publication_use == "PUBLISH"
    - support.evidence_kind != "resident_question"
    - support.support_kind != "operational"
    - candidate matches primary source text (exact substring or token-sequence match with typo tolerance)
    - sanitization does not mutate candidate (no masked phones or URLs)
    - candidate has at least 2 words and is not entirely trivial filler words
    """
    allowlist: list[str] = []
    seen: set[str] = set()

    for sup in context.support_index:
        if sup.publication_use != "PUBLISH":
            continue
        if sup.evidence_kind == "resident_question":
            continue
        if sup.support_kind == "operational":
            continue
        cand = sup.text.strip()
        if not cand or len(cand) < 3:
            continue
        tokens = quote_words(cand)
        if len(tokens) < 2 or all(t in _TRIVIAL_WORDS for t in tokens):
            continue
        if cand not in sup.source_text and not _quote_tokens_match(cand, sup.source_text):
            continue
        if sanitize_writer_source_text(cand) != cand:
            continue
        if cand not in seen:
            seen.add(cand)
            allowlist.append(cand)

    return tuple(allowlist)
