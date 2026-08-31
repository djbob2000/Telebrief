import pytest

from src.config_loader import _parse_edition_scopes


def test_parse_edition_scopes_accepts_structured_scope():
    scopes = _parse_edition_scopes(
        {
            "edition_scopes": {
                "berdyansk": {
                    "name": "Бердянск",
                    "focus_places": ["Бердянск", "Осипенко", "Азовское"],
                    "direct_impact_only": True,
                    "notes": ["A similarly named front direction is not the city itself."],
                }
            }
        }
    )
    scope = scopes["berdyansk"]
    assert scope.name == "Бердянск"
    assert scope.focus_places == ("Бердянск", "Осипенко", "Азовское")
    assert scope.direct_impact_only is True


def test_parse_edition_scopes_rejects_empty_focus_places():
    with pytest.raises(ValueError, match="focus_places"):
        _parse_edition_scopes(
            {"edition_scopes": {"berdyansk": {"name": "Бердянск", "focus_places": []}}}
        )


def test_parse_edition_scopes_rejects_duplicate_normalized_places():
    with pytest.raises(ValueError, match="duplicate"):
        _parse_edition_scopes(
            {
                "edition_scopes": {
                    "berdyansk": {
                        "name": "Бердянск",
                        "focus_places": ["Бердянск", " бердянск "],
                    }
                }
            }
        )
