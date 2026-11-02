"""Run 345: one resident listed six streets with one shared outage; that is not a roster."""

# ruff: noqa: S101
import json
from pathlib import Path

from src.city_context import CityContextResolver
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_models import ArticleParagraph, ArticleSection, StructuredArticleDraft
from src.publication.article_quality import _service_and_place, diagnose_article_quality

PROFILE = Path(__file__).resolve().parents[1] / "data/city_profiles/berdyansk.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _support(sid: str, text: str) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(),
        source_item_ids=(),
        observed_at=None,
        evidence_kind="community_report",
        story_id=f"story:{sid}",
    )


def _codes(sentence: str, supports: list[ArticleSupport]) -> set[str]:
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    ids = tuple(s.support_id for s in supports)
    draft = StructuredArticleDraft(
        title="Свет",
        title_support_ids=ids,
        lead="Свет.",
        lead_support_ids=ids,
        sections=(
            ArticleSection(
                heading="Свет",
                paragraphs=(ArticleParagraph(text=sentence, cited_support_ids=ids),),
            ),
        ),
    )
    report = diagnose_article_quality(
        draft,
        build_article_coverage_plan((), context),
        context,
        place_resolver=CityContextResolver.from_yaml(PROFILE),
    )
    return {finding.code for finding in report.findings}


def test_one_report_listing_streets_is_a_shared_condition() -> None:
    """Exact run 345 paragraph and its cited supports."""
    case = json.loads((FIXTURES / "run345_p001_shared_outage.json").read_text(encoding="utf-8"))
    supports = [_support(item["id"], item["fact"]) for item in case["supports"]]
    assert "OVERLOADED_ROSTER_PARAGRAPH" not in _codes(case["text"], supports)


def test_mixed_states_across_places_are_still_a_roster() -> None:
    supports = [
        _support("a", "По сообщению жителя, на улице Орджоникидзе света нет."),
        _support("b", "По сообщению жителя, на улице Чапаева свет включили."),
        _support("c", "По сообщению жителя, на улице Островского света нет."),
        _support("d", "По сообщению жителя, на улице Пушкина свет включили."),
        _support("e", "По сообщению жителя, на улице Димитрова света нет."),
    ]
    sentence = "На Орджоникидзе, Чапаева, Островского, Пушкина и Димитрова света нет."
    assert "OVERLOADED_ROSTER_PARAGRAPH" in _codes(sentence, supports)


def test_wires_are_not_water() -> None:
    assert _service_and_place("столб с проводами лежит, света нет 12 дней")[0] == "power"
    assert _service_and_place("нет воды третий день")[0] == "water"


def test_multi_sentence_roster_names_its_own_paragraph() -> None:
    supports = [
        _support("a", "По сообщению жителя, на улице Орджоникидзе света нет."),
        _support("b", "По сообщению жителя, на улице Чапаева свет включили."),
        _support("c", "По сообщению жителя, на улице Островского света нет."),
        _support("w", "Жительница рассказала, что на рынке работает швейная мастерская."),
    ]
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    roster_ids = ("a", "b", "c")
    draft = StructuredArticleDraft(
        title="Свет",
        title_support_ids=roster_ids,
        lead="Свет.",
        lead_support_ids=roster_ids,
        sections=(
            ArticleSection(
                heading="Мастерская",
                paragraphs=(
                    ArticleParagraph(
                        text="На рынке работает швейная мастерская.", cited_support_ids=("w",)
                    ),
                ),
            ),
            ArticleSection(
                heading="Свет",
                paragraphs=(
                    ArticleParagraph(
                        text=(
                            "На улице Орджоникидзе света нет. На улице Чапаева свет включили. "
                            "На улице Островского света нет."
                        ),
                        cited_support_ids=roster_ids,
                    ),
                ),
            ),
        ),
    )
    report = diagnose_article_quality(
        draft,
        build_article_coverage_plan((), context),
        context,
        place_resolver=CityContextResolver.from_yaml(PROFILE),
    )

    rosters = [f for f in report.findings if f.code == "MULTI_SENTENCE_ADDRESS_STATUS_ROSTER"]
    assert [f.unit_id for f in rosters] == ["P002"]
    assert set(rosters[0].support_ids) == set(roster_ids)
