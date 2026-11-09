"""Digest episodes: selected Stories that report one concrete incident.

Gate names the Stories it read as one incident while reading a chat. At
publication that link only decides which rubric and which digest item a Story
lands in. It never merges Story identities, facts, sources or locations.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace

import psycopg

from src.config_loader import DigestRubricsConfig
from src.editorial_models import StoryCard
from src.processing.conversation import SAME_INCIDENT_RELATION
from src.publication.rubrics import RubricAssignment

EPISODE_RUBRIC_METHOD = "episode_inherit"


def _card_story_id(card: StoryCard) -> int | None:
    raw = card.id.removeprefix("story:")
    return int(raw) if raw.isdigit() else None


async def load_incident_roots(
    conn: psycopg.AsyncConnection,
    cards: Sequence[StoryCard],
    *,
    snapshot_at: dt.datetime,
) -> dict[int, int]:
    """Map each card's Story to the lowest Story ID of its incident, as of the snapshot.

    The walk passes through Stories that were not selected: two reports that
    both point at one chat line outside the digest still describe one incident.
    """
    story_ids = [story_id for card in cards if (story_id := _card_story_id(card)) is not None]
    if not story_ids:
        return {}
    cursor = await conn.execute(
        """
        WITH RECURSIVE reach(seed, story_id) AS (
            SELECT id, id FROM unnest(%(story_ids)s::bigint[]) AS selected(id)
            UNION
            SELECT reach.seed, linked.story_id
            FROM reach
            CROSS JOIN LATERAL (
                SELECT sr.to_story_id AS story_id
                FROM story_relations sr
                WHERE sr.from_story_id = reach.story_id
                  AND sr.relation_type = %(relation)s
                  AND sr.created_at <= %(snapshot_at)s
                UNION ALL
                SELECT sr.from_story_id
                FROM story_relations sr
                WHERE sr.to_story_id = reach.story_id
                  AND sr.relation_type = %(relation)s
                  AND sr.created_at <= %(snapshot_at)s
            ) linked
        )
        SELECT seed, MIN(story_id) FROM reach GROUP BY seed
        """,
        {
            "story_ids": story_ids,
            "relation": SAME_INCIDENT_RELATION,
            "snapshot_at": snapshot_at,
        },
    )
    return {int(seed): int(root) for seed, root in await cursor.fetchall()}


def mark_digest_episodes(
    cards: Sequence[StoryCard], incident_roots: Mapping[int, int]
) -> list[StoryCard]:
    """Tag every card whose incident has at least two cards in this digest."""
    card_ids_by_root: dict[int, list[str]] = {}
    for card in cards:
        story_id = _card_story_id(card)
        if story_id is not None and story_id in incident_roots:
            card_ids_by_root.setdefault(incident_roots[story_id], []).append(card.id)
    episode_by_card = {
        card_id: f"incident:{root}"
        for root, card_ids in card_ids_by_root.items()
        if len(card_ids) >= 2
        for card_id in card_ids
    }
    return [
        replace(card, episode_id=episode_by_card[card.id]) if card.id in episode_by_card else card
        for card in cards
    ]


def inherit_episode_rubrics(
    cards: Sequence[StoryCard],
    assignments: Sequence[RubricAssignment],
    *,
    rubrics: DigestRubricsConfig,
) -> tuple[list[StoryCard], list[RubricAssignment]]:
    """Move a card out of the fallback rubric to join the rest of its incident.

    Only the fallback rubric gives way. A card the classifier placed in a
    concrete rubric stays there, so a link can never conflate service domains.
    """
    fallback_id = rubrics.fallback.id
    order = {rubric.id: index for index, rubric in enumerate(rubrics.items)}
    votes: dict[str, Counter[str]] = {}
    for card in cards:
        if card.episode_id and card.rubric_id != fallback_id:
            votes.setdefault(card.episode_id, Counter())[card.rubric_id] += 1
    winner = {
        episode_id: min(counts, key=lambda rubric: (-counts[rubric], order.get(rubric, len(order))))
        for episode_id, counts in votes.items()
    }
    moved = {
        card.id: winner[card.episode_id]
        for card in cards
        if card.rubric_id == fallback_id and card.episode_id in winner
    }
    return (
        [replace(card, rubric_id=moved[card.id]) if card.id in moved else card for card in cards],
        [
            replace(item, rubric_id=moved[item.story_id], score=None, method=EPISODE_RUBRIC_METHOD)
            if item.story_id in moved
            else item
            for item in assignments
        ],
    )
