"""Deterministic text fragmentation and rule-based noise filtering."""

from __future__ import annotations

import hashlib
import re

from src.domain.event_pipeline import NewSourceFragment

FRAGMENTER_VERSION = "v1"

MIN_CANDIDATE_ALNUM_CHARS = 6

_SHORT_REACTION_PATTERN = re.compile(
    r"^(?:ок|окей|да|нет|ага|угу|понял(?:а)?|понятно|ясно|хорошо|ладно|норм|супер|класс|лол|привет)[!.?]*$",
    re.IGNORECASE,
)

# Regexes for URL stripping, whitespace normalization, and noise detection
_URL_PATTERN = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_NON_ALPHANUM_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)

# Patterns for obvious commercial classified ads
_CLASSIFIED_PATTERNS = [
    re.compile(r"\b(продам|куплю|сдам|сдаю|сниму|аренда|продается|продаётся)\b", re.IGNORECASE),
    re.compile(r"\b(цена|стоимость|руб|рублей|р\.|грн|usd|\$)\b.*\b\d+\b", re.IGNORECASE),
    re.compile(r"\b\d+\b.*\b(руб|рублей|р\.|грн|usd|\$)\b", re.IGNORECASE),
    re.compile(r"\b(звонить|обращаться|писать в лс|л\.с\.|самовывоз|доставка)\b", re.IGNORECASE),
    re.compile(r"(\+7\s?9\d{2}|\+380|\b89\d{2})\s?\d{3}", re.IGNORECASE),
    re.compile(r"\b(маникюр|педикюр|наращивание|ресниц|брови|эпиляция|стрижка)\b", re.IGNORECASE),
    re.compile(
        r"\b(грузоперевозки|грузчики|переезды|ремонт квартир|установка окон)\b", re.IGNORECASE
    ),
]

# Patterns for short chatter, greetings, reaction noise
_NOISE_PATTERNS = [
    re.compile(
        r"^(доброе утро|добрый день|добрый вечер|спокойной ночи|всем привет|привет всем)[!.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(спасибо|благодарю|пожалуйста|не за что|плюсую|согласен|согласна)[!.]*$", re.IGNORECASE
    ),
    re.compile(r"^(подскажите пожалуйста|подскажите|кто знает|есть у кого)[?.]*$", re.IGNORECASE),
]


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
    """Check text against deterministic noise and classified ad rules."""
    trimmed = text.strip()
    if not trimmed:
        return True, "empty"

    if _SHORT_REACTION_PATTERN.search(trimmed):
        return True, "obvious_noise"

    # Check greetings / chatter
    for p in _NOISE_PATTERNS:
        if p.search(trimmed):
            return True, "obvious_noise"

    # Count alphanumeric characters
    alnum_chars = sum(1 for c in trimmed if c.isalnum())
    if alnum_chars < MIN_CANDIDATE_ALNUM_CHARS:
        return True, "too_short"

    # Check classified ad indicators (require at least 2 distinct cues for confidence)
    classified_cues = 0
    for p in _CLASSIFIED_PATTERNS:
        if p.search(trimmed):
            classified_cues += 1

    if classified_cues >= 2:
        return True, "commercial_classified"

    return False, None


def split_into_fragments(
    raw_text: str,
    *,
    max_chars: int = 1200,
    fragmenter_version: str = FRAGMENTER_VERSION,
) -> list[NewSourceFragment]:
    """Deterministically fragment raw post text into structured candidate chunks."""
    if not raw_text or not raw_text.strip():
        return []

    # 1. First-pass split by double newlines or list items
    paragraphs = re.split(r"\n\s*\n", raw_text.strip())
    raw_chunks: list[str] = []

    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue
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
