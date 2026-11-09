# ruff: noqa: S101
"""Stories of one incident share a digest rubric and, in one area, one item."""

from __future__ import annotations

from src.config_loader import DigestRubricConfig, DigestRubricsConfig
from src.editorial_models import StoryCard, StoryElement
from src.publication.digest_episodes import (
    EPISODE_RUBRIC_METHOD,
    inherit_episode_rubrics,
    mark_digest_episodes,
)
from src.publication.digest_presentation import build_thematic_topic_bundles
from src.publication.rubrics import RubricAssignment

RUBRICS = DigestRubricsConfig(
    items=(
        DigestRubricConfig(id="safety", name="safety", description="safety"),
        DigestRubricConfig(id="mobility", name="mobility", description="mobility"),
        DigestRubricConfig(id="other", name="other", description="other", fallback=True),
    )
)


def _card(
    story_id: int, *, rubric_id: str = "safety", episode_id: str = "", topic: str = "Взрывы"
) -> StoryCard:
    return StoryCard(
        id=f"story:{story_id}",
        topic=topic,
        importance="medium",
        summary=f"{topic}: жители сообщили о сильных взрывах ночью.",
        rubric_id=rubric_id,
        episode_id=episode_id,
        community_observations=[StoryElement(text=topic, source_refs=[f"story:{story_id}:ref"])],
    )


def _assignment(card: StoryCard) -> RubricAssignment:
    return RubricAssignment(card.id, card.rubric_id, 0.5, "semantic")


def test_incident_with_two_digest_cards_becomes_an_episode() -> None:
    marked = mark_digest_episodes([_card(1), _card(2), _card(3)], {1: 1, 2: 1, 3: 3})

    assert [card.episode_id for card in marked] == ["incident:1", "incident:1", ""]


def test_lone_card_of_an_incident_is_not_an_episode() -> None:
    assert mark_digest_episodes([_card(5)], {5: 2})[0].episode_id == ""


def test_card_id_that_is_not_a_story_is_ignored() -> None:
    card = StoryCard(id="legacy-1", topic="t", importance="medium", summary="s")

    assert mark_digest_episodes([card], {1: 1}) == [card]


def test_fallback_card_inherits_the_rubric_of_its_incident() -> None:
    cards = [
        _card(1, episode_id="incident:1"),
        _card(2, rubric_id="other", episode_id="incident:1"),
    ]

    moved, audit = inherit_episode_rubrics(cards, [_assignment(c) for c in cards], rubrics=RUBRICS)

    assert [card.rubric_id for card in moved] == ["safety", "safety"]
    assert audit[0] == _assignment(cards[0])
    assert audit[1] == RubricAssignment("story:2", "safety", None, EPISODE_RUBRIC_METHOD)


def test_concrete_rubrics_never_move() -> None:
    cards = [
        _card(1, episode_id="incident:1"),
        _card(2, episode_id="incident:1"),
        _card(3, rubric_id="mobility", episode_id="incident:1"),
    ]

    moved, _ = inherit_episode_rubrics(cards, [], rubrics=RUBRICS)

    assert [card.rubric_id for card in moved] == ["safety", "safety", "mobility"]


def test_rubric_tie_is_broken_by_config_order() -> None:
    cards = [
        _card(1, rubric_id="mobility", episode_id="incident:1"),
        _card(2, episode_id="incident:1"),
        _card(3, rubric_id="other", episode_id="incident:1"),
    ]

    moved, _ = inherit_episode_rubrics(cards, [], rubrics=RUBRICS)

    assert moved[2].rubric_id == "safety"


def test_episode_of_fallback_cards_stays_in_fallback() -> None:
    cards = [
        _card(1, rubric_id="other", episode_id="incident:1"),
        _card(2, rubric_id="other", episode_id="incident:1"),
    ]

    moved, _ = inherit_episode_rubrics(cards, [], rubrics=RUBRICS)

    assert [card.rubric_id for card in moved] == ["other", "other"]


def _bundle(cards: list[StoryCard]) -> tuple[tuple[str, ...], ...]:
    bundles = build_thematic_topic_bundles(cards, edition_slug="berdyansk")
    return tuple(bundle.story_ids for bundle in bundles)


AT_AKZ = "Взрывы в районе АКЗ"
AT_GORA = "Взрывы на Горе"
NO_PLACE = "Двери чуть не вынесло"


def test_unlocated_remark_joins_the_report_of_its_incident() -> None:
    cards = [
        _card(1, episode_id="incident:1", topic=AT_AKZ),
        _card(2, episode_id="incident:1", topic=NO_PLACE),
    ]

    bundles = build_thematic_topic_bundles(cards, edition_slug="berdyansk")

    assert [bundle.story_ids for bundle in bundles] == [("story:1", "story:2")]
    assert bundles[0].unresolved_geography_story_ids == ("story:2",)


def test_without_an_incident_link_the_same_cards_stay_apart() -> None:
    assert len(_bundle([_card(1, topic=AT_AKZ), _card(2, topic=NO_PLACE)])) == 2


def test_two_reports_from_one_area_named_two_ways_share_an_item() -> None:
    cards = [
        _card(1, episode_id="incident:1", topic=AT_GORA),
        _card(2, episode_id="incident:1", topic="На Горе двери ходуном"),
    ]

    assert _bundle(cards) == (("story:1", "story:2"),)


def test_an_incident_across_two_named_areas_keeps_the_geography_boundary() -> None:
    cards = [
        _card(1, episode_id="incident:1", topic=AT_AKZ),
        _card(2, episode_id="incident:1", topic=AT_GORA),
    ]

    assert len(_bundle(cards)) == 2
