from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from src.publication.article_claims import _quote_tokens_match, quote_words
from src.publication.article_writer_context import sanitize_writer_source_text

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext

_TRIVIAL_WORDS = frozenset({"да", "нет", "ок", "не", "хорошо", "точно", "плюс", "плюсую"})
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def build_article_quote_allowlist(
    context: ArticleEditorialContext,
    *,
    excluded_support_ids: Collection[str] = (),
    excluded_story_ids: Collection[str] = (),
    candidate_text_by_support_id: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Extract verifiable verbatim primary-source snippets eligible for direct quotation marks.

    Rules:
    - support.publication_use == "PUBLISH"
    - support.evidence_kind != "resident_question"
    - support.support_kind != "operational"
    - candidate comes from the writer-facing projection when supplied and matches primary source text
      (exact substring or contiguous token sequence)
    - sanitization does not mutate candidate (no masked phones or URLs)
    - candidate has at least 2 words and is not entirely trivial filler words
    """
    allowlist: list[str] = []
    seen: set[str] = set()

    excluded = set(excluded_support_ids)
    excluded_stories = set(excluded_story_ids)
    for sup in context.support_index:
        if sup.support_id in excluded:
            continue
        support_story_id = sup.story_id
        if not support_story_id:
            match = re.match(r"(story:[^:]+)", sup.support_id)
            support_story_id = match.group(1) if match else ""
        if support_story_id in excluded_stories:
            continue
        if sup.publication_use != "PUBLISH":
            continue
        if sup.evidence_kind == "resident_question":
            continue
        if sup.support_kind == "operational":
            continue
        candidates = [sup.text.strip()]
        if candidate_text_by_support_id is not None:
            projected_text = candidate_text_by_support_id.get(sup.support_id, "").strip()
            if not projected_text:
                continue
            candidates = [
                candidate for candidate in candidates if candidate and candidate in projected_text
            ]
            candidates.extend(
                sentence.strip()
                for sentence in _SENTENCE_SPLIT_RE.split(projected_text)
                if sentence.strip()
            )
            candidates.append(projected_text)

        for cand in dict.fromkeys(candidate for candidate in candidates if candidate):
            if len(cand) < 3:
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
