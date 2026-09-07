"""Deterministic text cleanup utilities for editorial article publications."""

from __future__ import annotations

import re

# Patterns matching meta-omission phrases where weaker LLMs explain what they omitted
_PARENTHETICAL_META_OMISSION_RE = re.compile(
    r"\s*\([^)]*(?:контактн[а-я]+\s+данн[а-я]+\s+опущен[а-я]*|"
    r"телефон[а-я]*\s+не\s+(?:указыва[а-я]+|привод[а-я]+|публику[а-я]+)|"
    r"контакт[а-я]*\s+скрыт[а-я]*|"
    r"дат[а-я]*\s+не\s+(?:указыва[а-я]+|уточня[а-я]+)|"
    r"номера\s+не\s+публику[а-я]+)[^)]*\)",
    re.IGNORECASE,
)

_CLAUSE_META_OMISSION_RE = re.compile(
    r"\s*,\s*(?:хотя|однако|но)\s+[^.!?]*(?:контактн[а-я]+\s+данн[а-я]+\s+опущен[а-я]*|"
    r"телефон[а-я]*\s+не\s+(?:указыва[а-я]+|привод[а-я]+|публику[а-я]+)|"
    r"контакт[а-я]*\s+скрыт[а-я]*|"
    r"дат[а-я]*\s+не\s+(?:указыва[а-я]+|уточня[а-я]+)|"
    r"номера\s+не\s+публику[а-я]+)",
    re.IGNORECASE,
)

_STANDALONE_META_OMISSION_RE = re.compile(
    r"\b(?:контактн[а-я]+\s+данн[а-я]+\s+опущен[а-я]*|"
    r"телефон[а-я]*\s+не\s+(?:указыва[а-я]+|привод[а-я]+|публику[а-я]+)|"
    r"контакт[а-я]*\s+скрыт[а-я]*|"
    r"дат[а-я]*\s+не\s+(?:указыва[а-я]+|уточня[а-я]+)|"
    r"номера\s+не\s+публику[а-я]+)\b",
    re.IGNORECASE,
)


def strip_leaked_meta_omissions(text: str) -> str:
    """Deterministically remove leaked meta-omission phrases from published prose."""
    if not text:
        return text

    cleaned = _PARENTHETICAL_META_OMISSION_RE.sub("", text)
    cleaned = _CLAUSE_META_OMISSION_RE.sub("", cleaned)
    # Fix any spacing before punctuation
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ ]{2,}", " ", cleaned)
    return cleaned.strip()
