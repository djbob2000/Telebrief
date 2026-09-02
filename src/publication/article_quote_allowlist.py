from __future__ import annotations

from typing import TYPE_CHECKING

from src.publication.article_writer_context import sanitize_writer_source_text

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext


def build_article_quote_allowlist(
    context: ArticleEditorialContext,
) -> tuple[str, ...]:
    """Extract verifiable verbatim primary-source snippets eligible for direct quotation marks.

    Rules:
    - support.publication_use == "PUBLISH"
    - support.evidence_kind != "resident_question"
    - support.support_kind != "operational"
    - support.text is a literal exact substring of support.source_text
    - sanitization does not mutate candidate (no masked phones or URLs)
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
        if cand not in sup.source_text:
            continue
        if sanitize_writer_source_text(cand) != cand:
            continue
        if cand not in seen:
            seen.add(cand)
            allowlist.append(cand)

    return tuple(allowlist)
