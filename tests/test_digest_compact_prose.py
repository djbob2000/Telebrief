# ruff: noqa: S101
"""One factual observation needs no duplicated headline/body presentation."""

from src.publication.digest_narrative import DigestEditorialItemDraft


def _item(headline: str, body: str) -> DigestEditorialItemDraft:
    return DigestEditorialItemDraft.from_dict(
        {
            "headline": headline,
            "body": body,
            "covered_story_ids": ["story:1"],
            "cited_support_ids": ["support:1"],
            "claims": [
                {"text": body, "covered_story_ids": ["story:1"], "cited_support_ids": ["support:1"]}
            ],
        }
    )


def test_complete_attributed_observation_renders_once_with_coverage() -> None:
    headline = "На АКЗ за неделю набирается менее суток со светом"
    body = "Житель сообщил, что на АКЗ за неделю набирается менее суток со светом."
    item = _item(headline, body)
    assert item.headline == ""
    assert item.body == body
    assert item.covered_story_ids == ("story:1",)
    assert item.cited_support_ids == ("support:1",)
    assert item.claims[0].text == body


def test_headline_detail_absent_from_body_is_preserved() -> None:
    item = _item("На АКЗ света нет 62 дня", "Житель сообщает, что свет дали на 15 минут.")
    assert "62 дня" in item.headline
    assert "15 минут" in item.body


def test_qualified_attribution_is_not_treated_as_a_complete_duplicate() -> None:
    body = "По словам жителя соседней улицы, на АКЗ света нет неделю."
    item = _item("На АКЗ света нет неделю", body)
    assert "соседней улицы" in item.body
