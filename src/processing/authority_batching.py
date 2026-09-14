"""Bounded batch packing for Event-First authority triage."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorityBatchItem:
    story_id: int
    estimated_input_chars: int


def estimate_story_input_chars(
    fragment_count: int,
    *,
    excerpt_chars: int = 320,
    max_gate_fragments: int = 6,
    overhead_chars: int = 300,
) -> int:
    """Conservatively estimate prompt input characters for one story cluster."""
    num_frags = min(max_gate_fragments, max(1, fragment_count))
    return num_frags * (excerpt_chars + 120) + overhead_chars


def select_bounded_authority_items(
    items: Sequence[AuthorityBatchItem],
    *,
    max_items: int,
    max_input_chars: int,
) -> tuple[tuple[AuthorityBatchItem, ...], tuple[AuthorityBatchItem, ...]]:
    """Select a deterministic bounded batch of items without mutating input."""
    if not items:
        return ((), ())
    if max_items <= 0:
        return ((), tuple(items))

    admitted: list[AuthorityBatchItem] = []
    overflow: list[AuthorityBatchItem] = []
    running_chars = 0

    for item in items:
        if len(admitted) == 0:
            admitted.append(item)
            running_chars += item.estimated_input_chars
        elif len(admitted) >= max_items or (
            running_chars + item.estimated_input_chars > max_input_chars
        ):
            overflow.append(item)
        else:
            admitted.append(item)
            running_chars += item.estimated_input_chars

    return (tuple(admitted), tuple(overflow))
