"""Unit tests verifying modular config parsing routines."""

import pytest

from src.config.parsers.common import (
    _parse_collection_config,
    _parse_filter_specs,
)
from src.config.parsers.publication import (
    _parse_digest_rubrics,
)
from src.config.parsers.telegram import (
    _normalize_source_type,
)


def test_parse_collection_config():
    cfg = _parse_collection_config({"collection": {"telegram_interval_minutes": 30}})
    assert cfg.telegram_interval_minutes == 30


def test_parse_collection_config_bounds():
    with pytest.raises(ValueError, match="collection.telegram_interval_minutes must be between"):
        _parse_collection_config({"collection": {"telegram_interval_minutes": 400}})


def test_normalize_source_type():
    assert _normalize_source_type("news", "label") == "news"
    with pytest.raises(ValueError, match="label must be one of"):
        _normalize_source_type("invalid_type", "label")


def test_parse_filter_specs():
    specs = _parse_filter_specs(
        [{"class_path": "src.filters.TestFilter", "config": {"threshold": 10}}],
        "settings.filters",
    )
    assert len(specs) == 1
    assert specs[0].class_path == "src.filters.TestFilter"


def test_parse_digest_rubrics():
    rubrics = _parse_digest_rubrics(
        {
            "digest_rubrics": {
                "min_similarity": 0.45,
                "items": [
                    {
                        "id": "city",
                        "name": "Город",
                        "description": "Городские новости",
                        "fallback": True,
                    }
                ],
            }
        }
    )
    assert rubrics.min_similarity == 0.45
    assert len(rubrics.items) == 1
    assert rubrics.fallback.id == "city"
