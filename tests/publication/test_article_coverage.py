from __future__ import annotations

import json
from pathlib import Path

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "article_city_life_coverage_cases.json"


def _load_cases() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_city_life_fixture_contains_major_supporting_brief_and_question_cases():
    data = _load_cases()
    ids = {story["story_id"] for story in data["stories"]}
    assert ids >= {
        "story:power",
        "story:safety",
        "story:telecom",
        "story:sport",
        "story:route",
        "story:question",
    }
