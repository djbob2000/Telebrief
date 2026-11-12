"""Masked words and profanity in direct-quote candidates and finished article prose.

Sources sometimes mask a swear word («Б... Дь», «х*й», «бл*»). Such a mask is
not a sentence boundary, and a quote that starts right after it is a fragment of
a sentence whose first words were removed. Candidate quotes are cut to whole
words after the mask, and profanity is never allowlisted.

Pure, dependency-light: only the shared profanity pattern is imported.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.publication.profanity_terms import PROFANITY_AND_ABUSE_RE

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext
    from src.publication.article_material import ArticleMaterialProjection
    from src.publication.article_models import StructuredArticleDraft
    from src.publication.article_quality import ArticleReaderQualityFinding

# Letters + ellipsis/asterisks + optional short trailing letters, with an optional
# space before the trailing part so «Б... Дь» is one masked word.
_MASK_RE = re.compile(
    r"(?<![^\W\d_])(?P<head>[^\W\d_]{1,3})(?:\.{2,}|…|\*+)"
    r"(?:[ \t]?(?P<tail>[^\W\d_]{1,3})(?![^\W\d_]))?"
)
_MASK_TAIL_RE = re.compile(
    r"(?<![^\W\d_])[^\W\d_]{1,3}(?:\.{2,}|…|\*+)(?:[ \t]?[^\W\d_]{1,3}(?![^\W\d_]))?$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_CUT_LEADING_RE = re.compile(r"^[\s,.;:!?—–-]+")
_CUT_TRAILING_RE = re.compile(r"[\s.!?…]+$")
_DIRECT_QUOTE_INNER_RE = re.compile(r"«([^»]+)»|“([^”]+)”|\"([^\"]+)\"")

# Stems of common swear words, lowercase, ё folded to е. A mask is treated as a
# masked swear word when a listed word starts with its visible head and ends with
# its visible tail.
_SWEAR_WORDS: tuple[str, ...] = (
    "хуй",
    "хуя",
    "хуе",
    "хуи",
    "похуй",
    "нахуй",
    "пизда",
    "пизду",
    "пиздец",
    "блядь",
    "блять",
    "бляди",
    "ебать",
    "ебаный",
    "еблан",
    "ебал",
    "мудак",
    "мудило",
    "сука",
    "суки",
    "залупа",
    "говно",
    "дерьмо",
    "жопа",
    "срака",
    "пидор",
    "пидорас",
)

QUOTE_MASK_WRITER_RULE = (
    "Замаскированные слова в источниках (например, «Б... Дь», «х*й», «бл*») и ругательства "
    "не переносите в текст и не цитируйте: передайте смысл косвенной речью, без кавычек и без "
    "мата. Не начинайте прямую цитату с середины предложения, которое в источнике идёт после "
    "маски."
)

QUOTE_MASK_EDITOR_INSTRUCTIONS: dict[str, str] = {
    "QUOTE_STARTS_AFTER_MASK": (
        " -> Цитата в этом абзаце начинается сразу после замаскированного слова в источнике, "
        "то есть является обрывком предложения. Слова внутри прямой цитаты менять нельзя, поэтому "
        "перепишите эту реплику косвенной речью без кавычек, сохранив подтверждённый смысл, "
        "атрибуцию и конкретные детали. Не добавляйте ругательства и не восстанавливайте маску."
    ),
    "PROFANITY_IN_ARTICLE": (
        " -> Удалите ругательство или замаскированное ругательство из этого фрагмента. Сохраните "
        "подтверждённый смысл нейтральными словами, косвенной речью; не добавляйте новых фактов."
    ),
}


def _mask_spans(text: str) -> tuple[tuple[int, int], ...]:
    return tuple(match.span() for match in _MASK_RE.finditer(text))


def _inside_mask(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in spans)


def split_sentences_outside_masks(text: str) -> list[str]:
    """Split at sentence boundaries, never inside a masked word such as «Б... Дь»."""
    spans = _mask_spans(text)
    pieces: list[str] = []
    cursor = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        if _inside_mask(match.start(), spans) or _inside_mask(match.end(), spans):
            continue
        pieces.append(text[cursor : match.start()])
        cursor = match.end()
    pieces.append(text[cursor:])
    return [piece for piece in pieces if piece.strip()]


def _word_count(text: str) -> int:
    return len(re.findall(r"[^\W\d_]+", text))


def is_masked_swear(head: str, tail: str) -> bool:
    """True when a mask with these visible letters can only be a listed swear word."""
    head_l = head.lower().replace("ё", "е")
    tail_l = tail.lower().replace("ё", "е")
    return any(
        word.startswith(head_l) and word.endswith(tail_l) and len(word) > len(head_l) + len(tail_l)
        for word in _SWEAR_WORDS
    )


def contains_profanity(text: str) -> bool:
    """Explicit profanity or a masked swear word, anywhere in the text."""
    if PROFANITY_AND_ABUSE_RE.search(text):
        return True
    return any(
        is_masked_swear(match.group("head"), match.group("tail") or "")
        for match in _MASK_RE.finditer(text)
    )


def _cut_after_last_mask(candidate: str) -> str:
    matches = list(_MASK_RE.finditer(candidate))
    if not matches:
        return candidate
    return candidate[matches[-1].end() :]


def mask_safe_quote_candidate(candidate: str, source_text: str) -> str | None:
    """Return the candidate cut to whole words after any mask, or None to drop it.

    A candidate that contains a mask keeps only the text after its last mask. A
    candidate that begins inside a mask in the source, or right after one, is cut
    to the first whole word after that mask. Candidates with fewer than two words
    left, or with profanity, are dropped.
    """
    text = candidate
    if _MASK_RE.search(text):
        text = _cut_after_last_mask(text)
    if source_text and text and text in source_text:
        start = source_text.find(text)
        spans = _mask_spans(source_text)
        for span_start, span_end in spans:
            if span_start < start < span_end:
                tail_end = start + len(text)
                text = source_text[span_end:tail_end]
                break
    text = _CUT_LEADING_RE.sub("", text)
    if text != candidate:
        text = _CUT_TRAILING_RE.sub("", text)
    if not text or _word_count(text) < 2:
        return None
    if contains_profanity(text):
        return None
    return text


def quote_starts_after_mask(quote: str, source_text: str) -> bool:
    """True when the quote occurs in the source immediately after a masked word."""
    if not quote or not source_text:
        return False
    start = source_text.find(quote)
    if start <= 0:
        return False
    prefix = source_text[:start].rstrip()
    return _MASK_TAIL_RE.search(prefix) is not None


def direct_quote_inner_texts(text: str) -> tuple[str, ...]:
    """Inner texts of typographic, curly and straight quotation marks in prose."""
    inners: list[str] = []
    for match in _DIRECT_QUOTE_INNER_RE.finditer(text):
        inner = next((group for group in match.groups() if group), "")
        if inner.strip():
            inners.append(inner.strip())
    return tuple(inners)


def quote_mask_quality_findings(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None = None,
) -> list[ArticleReaderQualityFinding]:
    """Repair findings for quotes that start after a mask and for profanity in prose."""
    from src.publication.article_quality import (
        ArticleReaderQualityFinding,
        _citable_support_ids,
    )
    from src.publication.article_speaker import prose_units

    support_by_id = getattr(context, "support_by_id", {})
    findings: list[ArticleReaderQualityFinding] = []
    for unit_id, text, cited_ids in prose_units(draft):
        if not text.strip():
            continue
        citable = _citable_support_ids(cited_ids, context, material_projection)
        if contains_profanity(text):
            findings.append(
                ArticleReaderQualityFinding(
                    code="PROFANITY_IN_ARTICLE",
                    unit_id=unit_id,
                    message="В тексте есть ругательство или замаскированное ругательство; передайте смысл нейтрально.",
                    support_ids=citable,
                    severity="repair",
                )
            )
        sources = [
            support_by_id[sid].source_text
            for sid in citable
            if sid in support_by_id and support_by_id[sid].source_text
        ]
        if any(
            quote_starts_after_mask(quote, source)
            for quote in direct_quote_inner_texts(text)
            for source in sources
        ):
            findings.append(
                ArticleReaderQualityFinding(
                    code="QUOTE_STARTS_AFTER_MASK",
                    unit_id=unit_id,
                    message="Прямая цитата начинается сразу после замаскированного слова в источнике; перескажите её косвенно.",
                    support_ids=citable,
                    severity="repair",
                )
            )
    return findings
