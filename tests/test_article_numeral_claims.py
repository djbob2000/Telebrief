# ruff: noqa: S101
"""Numbers in time and duration claims must match the cited sources."""

from __future__ import annotations

from src.publication.article_claims import find_unsupported_claims


def test_single_digit_time_claims_must_match_their_number() -> None:
    """A one-digit duration or hour is a concrete claim, not an unchecked word."""
    support = ["На Горе нет света 3 часа"]
    assert find_unsupported_claims("На Горе нет света 3 часа", support) == ()
    assert [c.raw for c in find_unsupported_claims("На Горе нет света 7 часов", support)] == [
        "7 часов"
    ]
    assert [c.raw for c in find_unsupported_claims("Свет пропал в 9 часов", [])] == ["9 часов"]
    assert [c.raw for c in find_unsupported_claims("На Горе нет света 17 часов", support)] == [
        "17 часов"
    ]
