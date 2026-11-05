"""Grounding checks shared by the newsroom memory layers.

Situation memory and the storyline chronicle store model-written statements
next to the source fragments they cite. A statement is kept only when its
concrete claims (numbers, dates, names, direct quotes) occur in those cited
fragments; anything else is dropped rather than repaired by guesswork.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REF_RE = re.compile(r"^fragment:\d+$")


def clean_text(value: Any, limit: int = 400) -> str:
    """Collapse whitespace and cap the length of a model-written field."""
    return " ".join(str(value or "").split())[:limit]


def parse_refs(value: Any) -> list[str]:
    """Return the well-formed ``fragment:<id>`` refs of a model-written list."""
    if not isinstance(value, list):
        return []
    return [str(ref) for ref in value if REF_RE.match(str(ref))]


def grounded_claim(
    text: str,
    refs: Sequence[str],
    texts: Mapping[str, Sequence[str]],
    *,
    quote_texts: Mapping[str, str] | None = None,
) -> bool:
    """True when the concrete claims of ``text`` occur in its own cited sources.

    ``texts`` maps a ref to every text a claim may be checked against (the
    extracted evidence and the primary source). ``quote_texts``, when given,
    maps a ref to its primary source wording: direct quotes must match it.
    """
    from src.publication.article_claims import find_unsupported_claims

    if not text or not refs or any(ref not in texts for ref in refs):
        return False
    supports = [support for ref in refs for support in texts[ref]]
    quote_sources = None
    if quote_texts is not None:
        quote_sources = [quote_texts[ref] for ref in refs if quote_texts.get(ref)]
    return not find_unsupported_claims(text, supports, direct_quote_source_texts=quote_sources)
