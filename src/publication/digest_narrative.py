"""Deterministic block planning, models, validation, and single-call writer for Event-First narrative digests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.config_loader import DigestRubricConfig
from src.editorial_models import StoryCard
from src.publication.evidence import PublicationEvidence


@dataclass(frozen=True)
class DigestNarrativeBlock:
    """Immutable presentation block grouping a fixed subset of rubric story cards."""

    block_id: str
    rubric_id: str
    rubric_title: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    canonical_notes: tuple[str, ...]


@dataclass(frozen=True)
class DigestNarrativePlan:
    """Deterministic plan of immutable narrative digest blocks."""

    blocks: tuple[DigestNarrativeBlock, ...]


def plan_digest_narrative_blocks(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[DigestRubricConfig],
    max_cards_per_block: int = 6,
) -> DigestNarrativePlan:
    """Build immutable narrative blocks from classified story cards strictly preserving order."""
    if not cards:
        return DigestNarrativePlan(blocks=())

    rubric_map: dict[str, DigestRubricConfig] = {r.id: r for r in rubrics}
    fallback_rubric = next((r for r in rubrics if r.fallback), rubrics[0] if rubrics else None)
    fallback_id = fallback_rubric.id if fallback_rubric else "other"

    # Group cards by rubric, preserving rubric sequence
    cards_by_rubric: dict[str, list[StoryCard]] = {r.id: [] for r in rubrics}
    for card in cards:
        rid = card.rubric_id if card.rubric_id in rubric_map else fallback_id
        if rid not in cards_by_rubric:
            cards_by_rubric[rid] = []
        cards_by_rubric[rid].append(card)

    blocks: list[DigestNarrativeBlock] = []

    for r in rubrics:
        rubric_cards = cards_by_rubric.get(r.id, [])
        if not rubric_cards:
            continue

        bound = max(1, max_cards_per_block)
        for chunk_idx in range(0, len(rubric_cards), bound):
            chunk = rubric_cards[chunk_idx : chunk_idx + bound]
            block_id = f"block:{r.id}:{chunk_idx // bound}"
            story_ids = tuple(c.id for c in chunk)

            # Collect canonical notes from cards
            notes: list[str] = []
            block_support_ids: list[str] = []
            for c in chunk:
                if c.summary:
                    notes.append(f"{c.topic}: {c.summary}")
                elif c.topic:
                    notes.append(c.topic)
                for hf in c.hard_facts:
                    if hf.text and hf.text not in notes:
                        notes.append(hf.text)
                for co in c.community_observations:
                    if co.text and co.text not in notes:
                        notes.append(co.text)

                # Extract numeric story ID if story:123
                if c.id.startswith("story:"):
                    raw_sid = c.id.split(":", 1)[1]
                    if raw_sid.isdigit():
                        num_sid = int(raw_sid)
                        for eid, evi in evidence.items():
                            if (
                                getattr(evi, "story_id", None) == num_sid
                                and eid not in block_support_ids
                            ):
                                block_support_ids.append(eid)

            blocks.append(
                DigestNarrativeBlock(
                    block_id=block_id,
                    rubric_id=r.id,
                    rubric_title=r.name,
                    story_ids=story_ids,
                    support_ids=tuple(block_support_ids),
                    canonical_notes=tuple(notes),
                )
            )

    return DigestNarrativePlan(blocks=tuple(blocks))
