"""Tests for the structured Gate V2 response contract."""

from src.processing.event_triage import build_gate_response_format


def test_gate_response_format_requires_exact_batch_shape():
    response_format = build_gate_response_format(3)

    assert response_format["type"] == "json_schema"
    json_schema = response_format["json_schema"]
    assert json_schema["name"] == "event_triage"
    assert json_schema["strict"] is True

    schema = json_schema["schema"]
    results = schema["properties"]["results"]
    assert schema["required"] == ["results"]
    assert results["minItems"] == 3
    assert results["maxItems"] == 3

    result_item = results["items"]
    assert result_item["additionalProperties"] is False
    assert {
        "story_id",
        "scope",
        "scope_basis_fragment_ids",
        "scope_confidence",
        "scope_reason",
        "retention",
        "enrichment",
        "exclusion_reason",
        "confidence",
        "reason",
        "brief_payload",
    }.issubset(result_item["required"])
