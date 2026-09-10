"""Worker startup guardrails."""

from __future__ import annotations

import pytest

from src.worker import validate_worker_configuration


@pytest.mark.unit
def test_processing_worker_stays_within_safe_concurrency_ceiling():
    validate_worker_configuration(2, ["processing"])


@pytest.mark.unit
def test_processing_worker_rejects_unsafe_concurrency():
    with pytest.raises(ValueError, match="processing worker concurrency"):
        validate_worker_configuration(3, ["processing"])


@pytest.mark.unit
def test_publication_only_worker_can_use_higher_concurrency():
    validate_worker_configuration(8, ["publication"])


@pytest.mark.unit
def test_worker_rejects_non_positive_concurrency():
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        validate_worker_configuration(0, ["publication"])
