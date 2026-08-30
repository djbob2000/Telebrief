"""Tests for generic Event-First narrative editorial contracts and invariants."""

from __future__ import annotations

import json
from pathlib import Path

from src.publication.narrative_contract import (
    ARTICLE_NARRATIVE_PROMPT_VERSION,
    DIGEST_NARRATIVE_PROMPT_VERSION,
    build_article_narrative_contract,
    build_digest_narrative_contract,
)


def test_article_narrative_contract_invariants():
    contract = build_article_narrative_contract(output_language="Russian")
    contract_lower = contract.lower()

    assert "validation metadata" in contract_lower
    assert "not sentence templates" in contract_lower
    assert "chronology" in contract_lower
    assert "contrast" in contract_lower
    assert "resident" in contract_lower
    assert "unsupported caus" in contract_lower
    assert "do not pad" in contract_lower
    assert "source-close" in contract_lower
    assert "one independently supportable proposition" in contract_lower
    assert "claim atoms may preserve source-language wording" in contract_lower
    assert "never translate or grammar-correct text inside quotation marks" in contract_lower
    assert "remove quotation marks and write indirect speech" in contract_lower
    assert "broad city-life coverage" in contract_lower
    assert "prominence controls depth, not inclusion" in contract_lower
    assert "do not collapse concrete evidence into generic summaries" in contract_lower
    assert "microdetail" in contract_lower
    assert "brief stories" in contract_lower
    assert "phone numbers" in contract_lower
    assert ARTICLE_NARRATIVE_PROMPT_VERSION == "event-article-narrative-v5"


def test_digest_narrative_contract_invariants():
    contract = build_digest_narrative_contract(output_language="Russian")
    contract_lower = contract.lower()

    assert "scan" in contract_lower
    assert "headline" in contract_lower
    assert "mini-summary" in contract_lower
    assert "compact" in contract_lower
    assert "giant paragraph" in contract_lower
    assert "partition" in contract_lower
    assert "resident" in contract_lower
    assert "unsupported caus" in contract_lower
    assert DIGEST_NARRATIVE_PROMPT_VERSION == "event-digest-narrative-v1"


def test_narrative_contract_is_pure_and_has_no_city_specific_leakage():
    for contract in [
        build_article_narrative_contract(output_language="Russian"),
        build_digest_narrative_contract(output_language="Russian"),
    ]:
        clow = contract.lower()
        for forbidden in ["бердянск", "azmol", "liski", "руб", "рубл", "мир"]:
            assert forbidden not in clow, f"City-specific leakage detected: {forbidden}"


def test_narrative_golden_fixtures_schema():
    fixture_path = (
        Path(__file__).parent.parent / "fixtures" / "berdyansk_narrative_editorial_golden.json"
    )
    assert fixture_path.exists(), "Golden fixture must exist"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert "cases" in data
    assert len(data["cases"]) >= 7
    case_ids = {c["id"] for c in data["cases"]}
    assert "prolonged_power_outage_with_generator_adaptations" in case_ids
    assert "water_chronology_multiple_areas" in case_ids
    assert "atm_cash_card_contrast" in case_ids
    assert "connectivity_workaround" in case_ids
    assert "uncertain_restoration_dates" in case_ids
    assert "short_thin_day_not_padded" in case_ids
    assert "unsupported_causal_interpretation_fails" in case_ids
