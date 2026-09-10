"""Structured logging regression coverage."""

from __future__ import annotations

import logging

import pytest

from src.utils import ExtraJSONFormatter


@pytest.mark.unit
def test_extra_json_formatter_preserves_structured_record_fields():
    formatter = ExtraJSONFormatter("%(message)s")
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "event_first_authority_complete", (), None
    )
    record.intent_id = 68
    record.requested = 25
    record.busy = 25

    text = formatter.format(record)

    assert '"intent_id": 68' in text
    assert '"busy": 25' in text
