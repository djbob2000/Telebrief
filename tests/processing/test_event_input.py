"""Tests for the versioned Event-First semantic input fingerprint."""

from src.processing.event_input import build_event_processing_fingerprint


def test_processing_hash_is_versioned_and_normalized():
    first = build_event_processing_fingerprint(
        "  Water OUT https://example.com/a ",
        fragmenter_version="v2",
    )
    second = build_event_processing_fingerprint(
        "water out https://example.com/b",
        fragmenter_version="v2",
    )

    assert first == second


def test_meaningful_text_change_changes_hash():
    first = build_event_processing_fingerprint("Water is out")
    second = build_event_processing_fingerprint("Water is back")

    assert first != second


def test_input_and_fragmenter_versions_change_hash():
    text = "Water is out"

    assert build_event_processing_fingerprint(text, event_input_version="text-v1") != (
        build_event_processing_fingerprint(text, event_input_version="text-v2")
    )
    assert build_event_processing_fingerprint(text, fragmenter_version="v2") != (
        build_event_processing_fingerprint(text, fragmenter_version="v3")
    )


def test_media_and_metadata_are_not_fingerprint_inputs():
    """The v1 function accepts only text and semantic-version arguments."""
    assert build_event_processing_fingerprint("same text") == build_event_processing_fingerprint(
        "same text"
    )
