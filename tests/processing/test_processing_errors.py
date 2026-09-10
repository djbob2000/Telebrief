"""Tests for Event-First infrastructure/provider error classification."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from src.processing.errors import classify_processing_failure


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PoolTimeout("pool exhausted"), "db_pool_timeout"),
        (psycopg.errors.DeadlockDetected("deadlock"), "db_deadlock"),
        (psycopg.errors.SerializationFailure("serialization"), "db_serialization_failure"),
        (TimeoutError("provider timeout"), "provider_timeout"),
        (RuntimeError("provider failed"), "provider_failure"),
    ],
)
def test_classify_processing_failure(exc: BaseException, expected: str):
    assert classify_processing_failure(exc) == expected
