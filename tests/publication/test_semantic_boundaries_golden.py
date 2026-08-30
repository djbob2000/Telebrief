import datetime as dt
import json
from pathlib import Path


def test_event_first_semantic_boundaries_golden_schema() -> None:
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "event_first_semantic_boundaries_golden.json"
    )
    assert fixture_path.exists(), f"Golden fixture missing at {fixture_path}"

    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    # 1. Snapshot and lookback
    assert "snapshot_at" in data
    snapshot_at = dt.datetime.fromisoformat(data["snapshot_at"])
    assert snapshot_at.tzinfo is not None

    assert "lookback_hours" in data
    assert isinstance(data["lookback_hours"], int) and data["lookback_hours"] > 0

    # 2. Temporal cases
    temporal_cases = data.get("temporal_cases", [])
    assert len(temporal_cases) >= 3
    case_ids = set()
    for tc in temporal_cases:
        assert "id" in tc and tc["id"]
        assert tc["id"] not in case_ids
        case_ids.add(tc["id"])

        obs = dt.datetime.fromisoformat(tc["observed_at"])
        assert obs.tzinfo is not None

        if tc["effective_from"] is not None:
            ef = dt.datetime.fromisoformat(tc["effective_from"])
            assert ef.tzinfo is not None

        if tc["effective_until"] is not None:
            eu = dt.datetime.fromisoformat(tc["effective_until"])
            assert eu.tzinfo is not None

        assert tc["expected_role"] in {
            "CURRENT_WINDOW",
            "HISTORICAL_CONTEXT",
            "FUTURE_SCHEDULED",
        }

    # 3. Unsupported soft claims
    soft_claims = data.get("unsupported_soft_claims", [])
    assert len(soft_claims) >= 4
    for sc in soft_claims:
        assert isinstance(sc, str) and len(sc.strip()) > 0

    # 4. Geography cases
    geo_cases = data.get("geography_cases", [])
    assert len(geo_cases) >= 6
    for gc in geo_cases:
        assert "text" in gc and gc["text"]
        assert "expected_local_signal" in gc or "expected_nonlocal_relation" in gc
