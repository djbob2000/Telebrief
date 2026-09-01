"""Embedding-based semantic digest rubric classifier (Plan 5 Task 5)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Sequence

from src.config_loader import DigestRubricConfig, DigestRubricsConfig
from src.domain.service_taxonomy import detect_service_families, map_family_to_rubric
from src.editorial_models import StoryCard

logger = logging.getLogger(__name__)

RUBRIC_CLASSIFIER_VERSION = "digest-rubric-embedding-v2"


@dataclass(frozen=True)
class RubricAssignment:
    """Audit record for a single story's presentation rubric classification."""

    story_id: str
    rubric_id: str
    score: float | None
    method: str  # "semantic" | "legacy_hint" | "family_fallback" | "fallback"


def story_classification_text(card: StoryCard) -> str:
    """Build the text representation of a StoryCard for embedding classification."""
    parts: list[str] = [
        card.topic,
        card.summary,
        *card.tags,
    ]

    parts.extend(item.text for item in card.hard_facts[:3])
    parts.extend(item.text for item in card.useful_details[:2])
    parts.extend(item.text for item in card.community_observations[:2])

    text = "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return text[:1800]


def rubric_classification_text(rubric: DigestRubricConfig) -> str:
    """Build the text representation of a DigestRubricConfig for embedding."""
    return f"{rubric.name}\n{rubric.description}".strip()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two numeric vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return -1.0
    return dot / (norm_a * norm_b)


def rubric_config_hash(rubrics: DigestRubricsConfig) -> str:
    """Compute a deterministic hash for a given rubric configuration."""
    data = [(r.id, r.name, r.description) for r in rubrics.items]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


class DigestRubricClassifier:
    """Assigns presentation rubric_ids to StoryCards using vector embeddings without generative LLMs."""

    def __init__(
        self,
        *,
        provider: Any | None = None,
        provider_name: str = "",
        model: str = "",
        dimensions: int = 1536,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.model = model
        self.dimensions = dimensions
        # In-process cache: (provider_name, model, dimensions, config_hash) -> {rubric_id: vector}
        self._rubric_vector_cache: dict[tuple[str, str, int, str], dict[str, list[float]]] = {}

    async def classify(
        self,
        cards: list[StoryCard],
        *,
        rubrics: DigestRubricsConfig,
    ) -> tuple[list[StoryCard], list[RubricAssignment]]:
        """Classify each card into one configured presentation rubric.

        Returns updated cards with rubric_id set, and corresponding audit assignments.
        """
        if not cards:
            return [], []

        known_rubrics_by_id = {r.id: r for r in rubrics.items}
        fallback_rubric = rubrics.fallback

        assignments_by_card_id: dict[str, RubricAssignment] = {}
        unresolved_cards: list[StoryCard] = []

        # 1. Compatibility hint check: matching legacy category without embedding
        for card in cards:
            if card.category and card.category in known_rubrics_by_id:
                assignments_by_card_id[card.id] = RubricAssignment(
                    story_id=card.id,
                    rubric_id=card.category,
                    score=1.0,
                    method="legacy_hint",
                )
            else:
                unresolved_cards.append(card)

        if not unresolved_cards:
            result_cards = [
                replace(c, rubric_id=assignments_by_card_id[c.id].rubric_id) for c in cards
            ]
            return result_cards, [assignments_by_card_id[c.id] for c in cards]

        # 2. Semantic classification via embeddings
        non_fallback_rubrics = [r for r in rubrics.items if not r.fallback]
        cache_key = (
            self.provider_name,
            self.model,
            self.dimensions,
            rubric_config_hash(rubrics),
        )

        try:
            if self.provider is None:
                raise RuntimeError("No embedding provider configured for DigestRubricClassifier")

            if cache_key not in self._rubric_vector_cache and non_fallback_rubrics:
                # Cold path: batch embed non-fallback rubrics + unresolved cards in one call
                rubric_texts = [rubric_classification_text(r) for r in non_fallback_rubrics]
                card_texts = [story_classification_text(c) for c in unresolved_cards]
                all_texts = rubric_texts + card_texts

                all_vectors = await self.provider.embed_many(
                    all_texts,
                    model=self.model,
                    dimensions=self.dimensions,
                    purpose="story_document",
                )

                num_rubrics = len(rubric_texts)
                rubric_vectors = all_vectors[:num_rubrics]
                card_vectors = all_vectors[num_rubrics:]

                # Cache non-fallback rubric vectors
                cached_dict = {
                    r.id: vec for r, vec in zip(non_fallback_rubrics, rubric_vectors, strict=True)
                }
                self._rubric_vector_cache[cache_key] = cached_dict
            else:
                # Warm path: embed only unresolved cards
                card_texts = [story_classification_text(c) for c in unresolved_cards]
                card_vectors = await self.provider.embed_many(
                    card_texts,
                    model=self.model,
                    dimensions=self.dimensions,
                    purpose="story_document",
                )
                cached_dict = self._rubric_vector_cache.get(cache_key, {})

            # 3. Match each card vector to highest similarity rubric
            for card, card_vec in zip(unresolved_cards, card_vectors, strict=True):
                if not cached_dict:
                    # No non-fallback rubrics exist
                    assignments_by_card_id[card.id] = RubricAssignment(
                        story_id=card.id,
                        rubric_id=fallback_rubric.id,
                        score=None,
                        method="fallback",
                    )
                    continue

                best_rubric_id = fallback_rubric.id
                best_score = -1.0

                for rubric_id, rubric_vec in cached_dict.items():
                    sim = cosine_similarity(card_vec, rubric_vec)
                    if sim > best_score:
                        best_score = sim
                        best_rubric_id = rubric_id

                if best_score >= rubrics.min_similarity:
                    assignments_by_card_id[card.id] = RubricAssignment(
                        story_id=card.id,
                        rubric_id=best_rubric_id,
                        score=best_score,
                        method="semantic",
                    )
                else:
                    # Deterministic family fallback before defaulting to other
                    card_text = story_classification_text(card)
                    fams = detect_service_families(card_text)
                    family_rubric = None
                    rubric_candidates = {
                        map_family_to_rubric(f) for f in fams if map_family_to_rubric(f) is not None
                    }
                    valid_candidates = {r for r in rubric_candidates if r in known_rubrics_by_id}
                    if len(valid_candidates) == 1:
                        family_rubric = next(iter(valid_candidates))

                    if family_rubric is not None:
                        assignments_by_card_id[card.id] = RubricAssignment(
                            story_id=card.id,
                            rubric_id=family_rubric,
                            score=best_score,
                            method="family_fallback",
                        )
                    else:
                        assignments_by_card_id[card.id] = RubricAssignment(
                            story_id=card.id,
                            rubric_id=fallback_rubric.id,
                            score=best_score,
                            method="fallback",
                        )

        except Exception as exc:
            logger.warning(
                "Rubric embedding classification failed: %s; falling back to %s",
                exc,
                fallback_rubric.id,
            )
            for card in unresolved_cards:
                assignments_by_card_id[card.id] = RubricAssignment(
                    story_id=card.id,
                    rubric_id=fallback_rubric.id,
                    score=None,
                    method="fallback",
                )

        result_cards = [replace(c, rubric_id=assignments_by_card_id[c.id].rubric_id) for c in cards]
        assignments = [assignments_by_card_id[c.id] for c in cards]
        return result_cards, assignments
