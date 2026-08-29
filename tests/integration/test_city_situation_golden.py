"""Integration tests for the Berdyansk city situation golden regression corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def golden_fixture() -> dict[str, Any]:
    fixture_path = (
        Path(__file__).resolve().parent.parent / "fixtures" / "berdyansk_city_situation_golden.json"
    )
    with fixture_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.integration
def test_city_situation_golden_contract(golden_fixture: dict[str, Any]) -> None:
    cases = golden_fixture.get("cases", [])
    negative_cases = golden_fixture.get("negative_cases", [])

    ids = {case["id"] for case in cases}
    assert ids == {
        "electricity_citywide",
        "nikolaevka_grp",
        "local_uav_pvo",
        "connectivity_workarounds",
        "atm_cash",
        "passport_fee",
        "water_koloniya_timeline",
    }

    neg_ids = {case["id"] for case in negative_cases}
    assert neg_ids == {"bucha_fire", "commercial_haircut"}

    # Validate each case has required deterministic metadata
    for case in cases:
        assert "expected_scope" in case
        assert "expected_retention" in case
        assert "fragments" in case
        assert len(case["fragments"]) > 0
        for frag in case["fragments"]:
            assert "fragment_id" in frag
            assert "source_id" in frag
            assert "observed_at" in frag
            assert "text" in frag

    for neg_case in negative_cases:
        assert "expected_scope" in neg_case
        assert "expected_retention" in neg_case
        assert "fragments" in neg_case
        assert len(neg_case["fragments"]) > 0
