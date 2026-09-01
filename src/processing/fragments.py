"""Deterministic text fragmentation and rule-based noise filtering."""

from __future__ import annotations

import hashlib
import re

from src.domain.event_pipeline import NewSourceFragment
from src.processing.noise_detection import (
    URL_PATTERN as _URL_PATTERN,
)
from src.processing.noise_detection import (
    WHITESPACE_PATTERN as _WHITESPACE_PATTERN,
)
from src.processing.noise_detection import (
    classify_text_noise_or_exclusion,
)

FRAGMENTER_VERSION = "v2"

_DEPENDENT_PREFIX_PATTERN = re.compile(
    r"^(?:[📍🏛🏢🛣🕗🕒📞☎️📱🌟📅📆🕖✅❤️❗️❗\s]*)(?:адрес|режим работы|график(?: работы)?|часы работы|время работы|контакты|запись|телефон|тел|стоимость|цена|вход|прием|приём|мы находимся|наш адрес|находимся по адресу)[:\s]",
    re.IGNORECASE,
)


def normalize_fragment_text(text: str) -> str:
    """Normalize text content for deterministic hashing and deduplication."""
    # 1. Remove URLs
    cleaned = _URL_PATTERN.sub("", text)
    # 2. Lowercase
    cleaned = cleaned.lower()
    # 3. Strip hashtags #tag -> tag
    cleaned = re.sub(r"#(\w+)", r"\1", cleaned)
    # 4. Collapse whitespace
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    return cleaned


def hash_normalized_text(norm: str) -> str:
    """Compute sha256 hex digest of normalized UTF-8 string."""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def is_noise_or_classified(text: str) -> tuple[bool, str | None]:
    """Check text against deterministic noise and classified ad rules using shared detector."""
    return classify_text_noise_or_exclusion(text, check_length=True)


def is_dependent_continuation(text: str) -> bool:
    """Return True if text begins with an operational/dependent prefix like 'Режим работы:' or 'Адрес:'."""
    return bool(_DEPENDENT_PREFIX_PATTERN.search(text.strip()))


def extract_parent_anchor(text: str, max_len: int = 40) -> str:
    """Extract a concise identifying prefix from parent text."""
    line = text.split("\n", 1)[0].strip()
    line = re.sub(r"^[\[\(«\"]+", "", line).strip()
    if len(line) <= max_len:
        return line
    truncated = line[: max_len - 3]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return f"{truncated}..."


def split_into_fragments(
    raw_text: str,
    *,
    max_chars: int = 1200,
    fragmenter_version: str = FRAGMENTER_VERSION,
) -> list[NewSourceFragment]:
    """Deterministically fragment raw post text into structured candidate chunks.

    Uses dependent-continuation packing: standalone paragraphs remain separate,
    while operational continuation blocks (e.g. 'Режим работы:', 'Адрес:', 'Контакты:')
    are packed backward into their parent identifying paragraph to prevent orphan fragments.
    """
    if not raw_text or not raw_text.strip():
        return []

    # 1. First-pass split by double newlines or list items
    paragraphs = re.split(r"\n\s*\n", raw_text.strip())
    raw_chunks: list[str] = []

    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue

        # If this paragraph is a dependent continuation block and we already have a previous chunk
        if is_dependent_continuation(p_clean) and raw_chunks:
            prev = raw_chunks[-1]
            if len(prev) + len(p_clean) + 2 <= max_chars:
                raw_chunks[-1] = f"{prev}\n\n{p_clean}"
                continue
            else:
                # If packing would exceed max_chars, give this dependent fragment an identifying parent anchor prefix
                anchor = extract_parent_anchor(prev)
                anchor_prefix = f"[{anchor}] " if anchor else ""
                if len(anchor_prefix) + len(p_clean) <= max_chars:
                    raw_chunks.append(f"{anchor_prefix}{p_clean}")
                    continue
                p_clean = f"{anchor_prefix}{p_clean}"

        if len(p_clean) <= max_chars:
            raw_chunks.append(p_clean)
        else:
            # Split paragraph into sentences on sentence terminators
            sentences = re.split(r"(?<=[.!?])\s+", p_clean)
            current = ""
            for s in sentences:
                if not s:
                    continue
                if current and (len(current) + len(s) + 1 > max_chars):
                    raw_chunks.append(current.strip())
                    current = s
                else:
                    current = f"{current} {s}".strip() if current else s
            if current:
                raw_chunks.append(current.strip())

    # 2. Build NewSourceFragment list
    fragments: list[NewSourceFragment] = []
    ordinal = 0
    for chunk in raw_chunks:
        chunk_clean = chunk.strip()
        if not chunk_clean:
            continue

        is_noise, reason = is_noise_or_classified(chunk_clean)
        norm = normalize_fragment_text(chunk_clean)
        h = hash_normalized_text(norm)

        is_candidate = not is_noise
        drop_reason = reason if is_noise else None

        fragments.append(
            NewSourceFragment(
                ordinal=ordinal,
                text_content=chunk_clean,
                normalized_hash=h,
                fragmenter_version=fragmenter_version,
                is_candidate=is_candidate,
                drop_reason=drop_reason,
            )
        )
        ordinal += 1

    return fragments
