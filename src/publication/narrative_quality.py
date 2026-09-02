"""Reader-first presentation quality and structural density metrics."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from src.publication.article_length import ArticleLengthProfile
from src.publication.article_models import StructuredArticleDraft
from src.publication.digest_narrative import DigestNarrativeDraft, DigestNarrativePlan

_TECHNICAL_TOKENS_RE = re.compile(
    r"\b(?:AVAILABLE|UNAVAILABLE|DEGRADED|RESTRICTED|SCHEDULED|CONFLICTING)\b|\[(?:story:\d+|SUPPORT\s+\d+|ref-\d+|tg:\S+)\]",
    re.IGNORECASE,
)
_INTERNAL_HANDLES_RE = re.compile(
    r"(?:\[(?:story:\d+|SUPPORT\s+\d+|ref-\d+|tg:\S+)\]|\b(?:story:\d+|evidence:\d+:frag:\d+)\b)",
    re.IGNORECASE,
)
_ATTRIBUTION_RE = re.compile(
    r"\b(?:по\s+сообщениям\s+жителей|жители\s+сообщают|по\s+словам\s+жителей|жители\s+отмечают|отмечают\s+жители|как\s+сообщают\s+жители|сообщают\s+жители|сообщают\s+горожане|по\s+информации\s+жителей|по\s+данным\s+жителей)\b",
    re.IGNORECASE,
)
_TEMPORAL_CHAIN_RE = re.compile(
    r"(?:ранее\s+также|также\s+ранее|(?:^|\.\s+)также\b)",
    re.IGNORECASE | re.MULTILINE,
)
_UKRAINIAN_PROSE_RE = re.compile(
    r"\b(?:та|що|як|це|але|після|було|буде|немає|року|році|місті|вулиці|людей|вони|який|яка|яке|які)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DigestReaderMetrics:
    """Quantitative presentation metrics for scan-first narrative digests."""

    total_blocks: int
    total_items: int
    avg_items_per_block: float
    max_items_per_block: int
    grouped_item_count: int
    single_item_count: int
    avg_headline_words: float
    max_headline_words: int
    avg_body_words: float
    max_body_words: int
    giant_paragraph_block_count: int


@dataclass(frozen=True)
class ArticleReaderMetrics:
    """Quantitative presentation metrics for selective long-read articles."""

    word_count: int
    section_count: int
    avg_words_per_section: float
    richness: str
    target_min_words: int
    target_max_words: int
    is_within_target: bool
    is_above_hard_floor: bool
    unsupported_claim_count: int


@dataclass(frozen=True)
class ReaderTextMetrics:
    """Deterministic quantitative reader text quality metrics."""

    word_count: int
    sentence_count: int
    paragraph_count: int
    bullet_count: int
    heading_count: int
    max_paragraph_words: int
    duplicate_sentence_count: int
    attribution_count: int
    attribution_density: float
    temporal_chain_marker_count: int
    raw_technical_token_count: int
    internal_handle_count: int
    non_output_language_outside_quotes_count: int
    title_word_count: int
    lead_word_count: int
    title_lead_overlap: float


@dataclass(frozen=True)
class DigestReaderProfile:
    """Adaptive reader budget profile for digests depending on reporting richness."""

    richness: Literal["thin", "standard", "rich"]
    target_max_words: int
    hard_max_words: int
    target_max_detail_items: int
    hard_max_detail_items: int
    max_item_body_words: int


@dataclass(frozen=True)
class ReaderQualityViolation:
    """Specific reader-facing quality failure."""

    code: str
    message: str
    metric_value: Any
    threshold: Any


def derive_digest_reader_profile(story_count: int) -> DigestReaderProfile:
    """Derive adaptive digest presentation budgets from story volume."""
    if story_count <= 12:
        return DigestReaderProfile("thin", 450, 650, 10, 12, 80)
    if story_count <= 50:
        return DigestReaderProfile("standard", 800, 1050, 16, 20, 90)
    return DigestReaderProfile("rich", 1200, 1500, 24, 30, 100)


def measure_reader_text_metrics(
    text: str,
    publication_type: str = "article",
) -> ReaderTextMetrics:
    """Extract deterministic presentation quality metrics from rendered markdown text."""
    norm_text = unicodedata.normalize("NFC", text or "")
    lines = norm_text.splitlines()

    total_words = len(norm_text.split())
    bullet_count = sum(1 for line in lines if line.strip().startswith(("•", "-", "* ")))
    heading_count = sum(
        1
        for line in lines
        if line.strip().startswith(("#", "##", "###", "####"))
        or (
            line.strip().startswith("*")
            and line.strip().endswith("*")
            and len(line.strip()) < 80
            and not line.strip().startswith("* ")
        )
    )

    # Paragraph extraction
    raw_blocks = [b.strip() for b in norm_text.split("\n\n") if b.strip()]
    paragraphs: list[str] = []
    title_text = ""
    lead_text = ""

    for idx, block in enumerate(raw_blocks):
        clean_block = block.strip()
        if idx == 0 and (clean_block.startswith("#") or clean_block.startswith("*")):
            title_text = re.sub(r"^[#*•\s\-_]+|[#*•\s\-_]+$", "", clean_block).strip()
            continue
        if idx == 1 and not clean_block.startswith(("#", "•", "-")):
            lead_text = clean_block
            paragraphs.append(clean_block)
            continue
        if not clean_block.startswith(("#", "•", "-")):
            paragraphs.append(clean_block)

    paragraph_count = len(paragraphs)
    paragraph_word_counts = [len(p.split()) for p in paragraphs]
    max_para_words = max(paragraph_word_counts) if paragraph_word_counts else 0

    # Sentences and duplicate sentence detection
    # Split sentences by ., !, ?
    raw_sents = re.split(r"(?<=[.!?])\s+", norm_text)
    cleaned_sents: list[str] = []
    for s in raw_sents:
        s_clean = re.sub(r"^[#*•\s\-_]+", "", s).strip()
        s_clean = re.sub(r"[«»“”\"\'`]", "", s_clean).lower()
        s_clean = re.sub(r"\s+", " ", s_clean).strip()
        if len(s_clean) > 20:
            cleaned_sents.append(s_clean)

    sent_counts = Counter(cleaned_sents)
    duplicate_sentence_count = sum(cnt - 1 for cnt in sent_counts.values() if cnt > 1)
    sentence_count = len(raw_sents) if raw_sents and raw_sents[0] else 0

    # Attributions
    attr_matches = _ATTRIBUTION_RE.findall(norm_text)
    attribution_count = len(attr_matches)
    attribution_density = (
        round(attribution_count / sentence_count, 4) if sentence_count > 0 else 0.0
    )

    # Temporal chains
    chain_matches = _TEMPORAL_CHAIN_RE.findall(norm_text)
    temporal_chain_marker_count = len(chain_matches)

    # Technical tokens and internal handles
    technical_token_matches = _TECHNICAL_TOKENS_RE.findall(norm_text)
    raw_technical_token_count = len(technical_token_matches)

    internal_handle_matches = _INTERNAL_HANDLES_RE.findall(norm_text)
    internal_handle_count = len(internal_handle_matches)

    # Non-output language outside quotes (masking quotes)
    text_outside_quotes = re.sub(r"[«\"][^»\"]+[»\"]", "", norm_text)
    ukr_words = _UKRAINIAN_PROSE_RE.findall(text_outside_quotes)
    non_output_language_count = len(ukr_words)

    title_word_count = len(title_text.split())
    lead_word_count = len(lead_text.split())

    overlap = 0.0
    if title_word_count and lead_word_count:
        title_words = set(re.findall(r"[a-zа-яё0-9]+", title_text.lower()))
        lead_words = set(re.findall(r"[a-zа-яё0-9]+", lead_text.lower()))
        if title_words:
            overlap = round(len(title_words & lead_words) / len(title_words), 4)

    return ReaderTextMetrics(
        word_count=total_words,
        sentence_count=sentence_count,
        paragraph_count=paragraph_count,
        bullet_count=bullet_count,
        heading_count=heading_count,
        max_paragraph_words=max_para_words,
        duplicate_sentence_count=duplicate_sentence_count,
        attribution_count=attribution_count,
        attribution_density=attribution_density,
        temporal_chain_marker_count=temporal_chain_marker_count,
        raw_technical_token_count=raw_technical_token_count,
        internal_handle_count=internal_handle_count,
        non_output_language_outside_quotes_count=non_output_language_count,
        title_word_count=title_word_count,
        lead_word_count=lead_word_count,
        title_lead_overlap=overlap,
    )


def evaluate_article_markdown_quality(text: str) -> ReaderTextMetrics:
    """Evaluate reader quality metrics for article markdown text."""
    return measure_reader_text_metrics(text, publication_type="article")


def evaluate_digest_markdown_quality(text: str) -> ReaderTextMetrics:
    """Evaluate reader quality metrics for digest markdown text."""
    return measure_reader_text_metrics(text, publication_type="digest_grouped")


def measure_digest_reader_metrics(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
) -> DigestReaderMetrics:
    """Compute reader UX metrics for narrative digest draft."""
    total_blocks = len(draft.blocks)
    if total_blocks == 0:
        return DigestReaderMetrics(
            total_blocks=0,
            total_items=0,
            avg_items_per_block=0.0,
            max_items_per_block=0,
            grouped_item_count=0,
            single_item_count=0,
            avg_headline_words=0.0,
            max_headline_words=0,
            avg_body_words=0.0,
            max_body_words=0,
            giant_paragraph_block_count=0,
        )

    all_items = [item for b in draft.blocks for item in b.items]
    total_items = len(all_items)
    items_per_block = [len(b.items) for b in draft.blocks]
    max_items = max(items_per_block) if items_per_block else 0
    avg_items = total_items / total_blocks if total_blocks else 0.0

    grouped_items = sum(1 for item in all_items if len(item.covered_story_ids) > 1)
    single_items = sum(1 for item in all_items if len(item.covered_story_ids) <= 1)

    headline_word_counts = [len(item.headline.split()) for item in all_items]
    body_word_counts = [len(item.body.split()) for item in all_items]

    avg_hl = sum(headline_word_counts) / total_items if total_items else 0.0
    max_hl = max(headline_word_counts) if headline_word_counts else 0
    avg_b = sum(body_word_counts) / total_items if total_items else 0.0
    max_b = max(body_word_counts) if body_word_counts else 0

    giant_blocks = 0
    for b in draft.blocks:
        if len(b.items) == 1:
            it = b.items[0]
            if len(it.covered_story_ids) >= 3 and len(it.body) > 400:
                giant_blocks += 1

    return DigestReaderMetrics(
        total_blocks=total_blocks,
        total_items=total_items,
        avg_items_per_block=round(avg_items, 2),
        max_items_per_block=max_items,
        grouped_item_count=grouped_items,
        single_item_count=single_items,
        avg_headline_words=round(avg_hl, 2),
        max_headline_words=max_hl,
        avg_body_words=round(avg_b, 2),
        max_body_words=max_b,
        giant_paragraph_block_count=giant_blocks,
    )


def measure_article_reader_metrics(
    draft: StructuredArticleDraft,
    length_profile: ArticleLengthProfile,
    unsupported_claim_count: int = 0,
) -> ArticleReaderMetrics:
    """Compute reader UX metrics for long-form article draft."""
    w_count = draft.word_count
    sec_count = len(draft.sections)
    avg_words_sec = round(w_count / sec_count, 2) if sec_count else 0.0

    within_target = length_profile.target_min_words <= w_count <= length_profile.target_max_words
    above_hard_floor = w_count >= length_profile.hard_min_words

    return ArticleReaderMetrics(
        word_count=w_count,
        section_count=sec_count,
        avg_words_per_section=avg_words_sec,
        richness=length_profile.richness,
        target_min_words=length_profile.target_min_words,
        target_max_words=length_profile.target_max_words,
        is_within_target=within_target,
        is_above_hard_floor=above_hard_floor,
        unsupported_claim_count=unsupported_claim_count,
    )
