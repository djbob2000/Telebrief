"""Run 343: the writer left the second block's ID blank."""

# ruff: noqa: S101
from src.publication.digest_narrative import _fill_omitted_block_ids

EXPECTED = ["block:infrastructure:composition", "block:health:composition"]


def test_blank_id_in_its_place_is_restored() -> None:
    assert _fill_omitted_block_ids(["block:infrastructure:composition", ""], EXPECTED) == EXPECTED


def test_misplaced_or_missing_blocks_are_not_guessed() -> None:
    swapped = ["block:health:composition", ""]
    assert _fill_omitted_block_ids(swapped, EXPECTED) == swapped
    assert _fill_omitted_block_ids([""], EXPECTED) == [""]
    wrong = ["block:infrastructure:composition", "block:other:composition"]
    assert _fill_omitted_block_ids(wrong, EXPECTED) == wrong
