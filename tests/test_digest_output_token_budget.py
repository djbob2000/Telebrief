# ruff: noqa: S101

import pytest


def test_digest_narrative_output_budget_defaults_to_verified_limit():
    from src.config.parsers.publication import _parse_publication_editorial_config
    from src.config.schemas.publication import PublicationEditorialConfig

    assert PublicationEditorialConfig().digest_narrative_max_output_tokens == 2800
    parsed = _parse_publication_editorial_config({"publication_editorial": {}})
    assert parsed.digest_narrative_max_output_tokens == 2800


def test_digest_narrative_output_budget_rejects_unbounded_provider_request():
    from src.config.parsers.publication import _parse_publication_editorial_config

    with pytest.raises(ValueError, match="digest_narrative_max_output_tokens.*4096"):
        _parse_publication_editorial_config(
            {"publication_editorial": {"digest_narrative_max_output_tokens": 32768}}
        )
