"""Unit tests for bounded authority batch packing."""

from __future__ import annotations

from src.processing.authority_batching import (
    AuthorityBatchItem,
    select_bounded_authority_items,
)


def test_select_bounded_empty():
    admitted, overflow = select_bounded_authority_items([], max_items=10, max_input_chars=48_000)
    assert admitted == ()
    assert overflow == ()


def test_select_bounded_count_limit():
    items = [AuthorityBatchItem(story_id=i, estimated_input_chars=100) for i in range(15)]
    admitted, overflow = select_bounded_authority_items(items, max_items=10, max_input_chars=48_000)
    assert len(admitted) == 10
    assert len(overflow) == 5
    assert tuple(i.story_id for i in admitted) == tuple(range(10))
    assert tuple(i.story_id for i in overflow) == tuple(range(10, 15))


def test_select_bounded_char_limit():
    items = [
        AuthorityBatchItem(story_id=1, estimated_input_chars=20_000),
        AuthorityBatchItem(story_id=2, estimated_input_chars=20_000),
        AuthorityBatchItem(story_id=3, estimated_input_chars=15_000),
    ]
    admitted, overflow = select_bounded_authority_items(items, max_items=10, max_input_chars=45_000)
    assert tuple(i.story_id for i in admitted) == (1, 2)
    assert tuple(i.story_id for i in overflow) == (3,)


def test_select_bounded_oversized_singleton_is_admitted():
    items = [
        AuthorityBatchItem(story_id=1, estimated_input_chars=60_000),
        AuthorityBatchItem(story_id=2, estimated_input_chars=1_000),
    ]
    admitted, overflow = select_bounded_authority_items(items, max_items=10, max_input_chars=48_000)
    assert tuple(i.story_id for i in admitted) == (1,)
    assert tuple(i.story_id for i in overflow) == (2,)


def test_select_bounded_preserves_order_and_no_mutation():
    original = [
        AuthorityBatchItem(story_id=10, estimated_input_chars=500),
        AuthorityBatchItem(story_id=20, estimated_input_chars=500),
        AuthorityBatchItem(story_id=30, estimated_input_chars=500),
    ]
    copy_items = list(original)
    admitted, overflow = select_bounded_authority_items(
        original, max_items=2, max_input_chars=2_000
    )
    assert original == copy_items
    assert tuple(i.story_id for i in admitted) == (10, 20)
    assert tuple(i.story_id for i in overflow) == (30,)
