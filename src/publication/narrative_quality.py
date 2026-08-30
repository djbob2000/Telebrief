"""Reader-first presentation quality and structural density metrics."""

from __future__ import annotations

from dataclasses import dataclass

from src.publication.article_length import ArticleLengthProfile
from src.publication.article_models import StructuredArticleDraft
from src.publication.digest_narrative import DigestNarrativeDraft, DigestNarrativePlan


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
