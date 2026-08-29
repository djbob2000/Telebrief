"""Representative evidence sampling via MMR and multi-source diversity."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Sequence

from src.domain.event_pipeline import SourceFragment


@dataclass(frozen=True)
class FragmentWithContext:
    """A source fragment enriched with its embedding vector and source metadata."""

    fragment: SourceFragment
    vector: Sequence[float]
    source_id: int
    source_name: str
    source_type: str  # "official", "news", "community", etc.
    timestamp: dt.datetime

    @property
    def is_official(self) -> bool:
        return self.source_type.lower() in ("official", "authorities", "utility", "emergency")


@dataclass(frozen=True)
class SampledFragment:
    """Sampled representative fragment for LLM analysis input."""

    fragment_id: int
    text_content: str
    source_id: int
    source_name: str
    source_type: str
    timestamp: dt.datetime
    similarity_to_centroid: float
    is_official: bool


def _cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2, strict=True))
    norm1 = math.sqrt(sum(a * a for a, b in zip(v1, v1, strict=True)))
    norm2 = math.sqrt(sum(b * b for b, b in zip(v2, v2, strict=True)))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class RepresentativeEvidenceSampler:
    """Selects up to `limit` diverse, high-value representative fragments from a story cluster."""

    def sample_fragments(
        self,
        fragments: Sequence[FragmentWithContext],
        *,
        centroid: Sequence[float],
        limit: int = 16,
        lambda_param: float = 0.7,
    ) -> list[SampledFragment]:
        """Select representative fragments via Maximal Marginal Relevance (MMR) and source diversity."""
        candidates = [f for f in fragments if f.fragment.is_candidate]
        if not candidates:
            return []

        if len(candidates) <= limit:
            return [
                SampledFragment(
                    fragment_id=c.fragment.id,
                    text_content=c.fragment.text_content,
                    source_id=c.source_id,
                    source_name=c.source_name,
                    source_type=c.source_type,
                    timestamp=c.timestamp,
                    similarity_to_centroid=_cosine_similarity(c.vector, centroid),
                    is_official=c.is_official,
                )
                for c in candidates
            ]

        # Precompute similarities to centroid
        sim_to_centroid: dict[int, float] = {
            c.fragment.id: _cosine_similarity(c.vector, centroid) for c in candidates
        }

        selected: list[FragmentWithContext] = []
        selected_source_ids: set[int] = set()
        remaining: list[FragmentWithContext] = list(candidates)

        # 1. Pick first element: top official fragment or closest to centroid
        def _seed_score(c: FragmentWithContext) -> float:
            base = sim_to_centroid[c.fragment.id]
            bonus = 0.2 if c.is_official else 0.0
            return base + bonus

        first = max(remaining, key=_seed_score)
        selected.append(first)
        selected_source_ids.add(first.source_id)
        remaining.remove(first)

        # 2. MMR loop
        while remaining and len(selected) < limit:
            best_cand: FragmentWithContext | None = None
            best_mmr = -1e9

            for cand in remaining:
                cand_sim = sim_to_centroid[cand.fragment.id]
                max_sim_to_selected = max(
                    _cosine_similarity(cand.vector, s.vector) for s in selected
                )
                # Source diversity bonus
                source_bonus = 0.15 if cand.source_id not in selected_source_ids else 0.0
                official_bonus = 0.1 if cand.is_official else 0.0

                mmr = (
                    lambda_param * cand_sim
                    - (1.0 - lambda_param) * max_sim_to_selected
                    + source_bonus
                    + official_bonus
                )

                if mmr > best_mmr:
                    best_mmr = mmr
                    best_cand = cand

            if best_cand is None:
                break

            selected.append(best_cand)
            selected_source_ids.add(best_cand.source_id)
            remaining.remove(best_cand)

        return [
            SampledFragment(
                fragment_id=s.fragment.id,
                text_content=s.fragment.text_content,
                source_id=s.source_id,
                source_name=s.source_name,
                source_type=s.source_type,
                timestamp=s.timestamp,
                similarity_to_centroid=sim_to_centroid[s.fragment.id],
                is_official=s.is_official,
            )
            for s in selected
        ]
