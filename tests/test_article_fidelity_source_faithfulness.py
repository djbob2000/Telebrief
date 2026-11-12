# ruff: noqa: S101
"""article-fidelity W7.1 and W7.3: source-faithful durations and hedges (repair, non-blocking)."""

from __future__ import annotations

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_evidence_checks import (
    HEDGE_DROPPED,
    ORDINAL_DURATION_INFLATED,
    diagnose_evidence_faithfulness,
    evidence_units_for_draft,
)
from src.publication.article_models import ArticleParagraph, ArticleSection, StructuredArticleDraft
from src.publication.article_quality import diagnose_article_quality
from src.publication.article_quality_policy import article_quality_policy


def _support(sid: str, text: str, source: str | None = None) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=source if source is not None else text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(),
        source_item_ids=(),
        observed_at=None,
        evidence_kind="community_report",
        story_id=f"story:{sid}",
    )


WEEK_641253 = _support(
    "story:9:evidence:0:frag:641253",
    "По сообщению жителя, света нет четыре недели.",
    "Тут нет света 4 неделю",
)
WEEK_ORDINAL_WORDS = _support(
    "story:9:evidence:1:frag:641254",
    "Жители ждут света на четвёртой неделе.",
    "Идёт четвертая неделя без света",
)
DAYS_SHEVCHENKO = _support(
    "story:10:evidence:0:frag:641300",
    "На Шевченко света нет 11 дней.",
    "Но, не всё в центре, ул Шевченко, начало, уже 11 день нет света (((",
)
MEAT_PLANT_HEDGE = _support(
    "story:11:evidence:0:frag:641400",
    "Мясокомбинат без электричества постепенно останавливается, продукцию негде хранить.",
    "Загибается похоже потихоньку мясокомбинат без электричества, продукцию хранить-то как?",
)


def _draft(text: str, cited: tuple[str, ...], lead: str = "Свет.") -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Свет",
        title_support_ids=cited,
        lead=lead,
        lead_support_ids=cited if lead else (),
        sections=(
            ArticleSection(
                heading="Свет",
                heading_support_ids=cited,
                paragraphs=(ArticleParagraph(text=text, cited_support_ids=cited),),
            ),
        ),
    )


def _findings(text: str, supports: list[ArticleSupport]) -> list[str]:
    by_id = {s.support_id: s for s in supports}
    cited = tuple(s.support_id for s in supports)
    # The lead is judged separately; an empty lead keeps this check on the paragraph alone.
    units = evidence_units_for_draft(_draft(text, cited, lead=""))
    return [f.code for f in diagnose_evidence_faithfulness(units, by_id)]


def _quality_codes(text: str, supports: list[ArticleSupport]) -> set[str]:
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    draft = _draft(text, tuple(s.support_id for s in supports))
    report = diagnose_article_quality(draft, build_article_coverage_plan((), context), context)
    return {f.code for f in report.findings}


# W7.1: ordinal durations


def test_cardinal_week_count_is_inflated_when_the_source_says_the_ordinal() -> None:
    text = "Света нет уже четыре недели."
    assert ORDINAL_DURATION_INFLATED in _findings(text, [WEEK_641253])


def test_gate_paraphrase_cardinal_is_judged_against_the_source_message() -> None:
    # The fact says «четыре недели», but the message says «4 неделю»: still inflated.
    paraphrase = _support(
        "story:9:evidence:2:frag:641255",
        "По сообщению жителя, света нет четыре недели.",
        "Тут нет света 4 неделю",
    )
    assert ORDINAL_DURATION_INFLATED in _findings("Света нет уже четыре недели.", [paraphrase])


def test_ordinal_week_wording_passes() -> None:
    assert _findings("Идёт четвёртая неделя без света.", [WEEK_ORDINAL_WORDS]) == []


def test_days_are_exempt_from_the_week_check() -> None:
    assert _findings("На Шевченко света нет 11 дней.", [DAYS_SHEVCHENKO]) == []


def test_ordinal_week_check_is_a_repair_not_a_blocker() -> None:
    codes = _quality_codes("Света нет уже четыре недели.", [WEEK_641253])
    assert ORDINAL_DURATION_INFLATED in codes
    policy = article_quality_policy(ORDINAL_DURATION_INFLATED)
    assert policy.severity == "repair"
    assert policy.publication_effect == "repair_recommended"


# W7.3: dropped hedges


def test_hedge_dropped_when_the_only_source_is_hedged_and_prose_is_plain() -> None:
    text = (
        "Без электричества он, по словам жителя, постепенно останавливается, "
        "и продукцию негде хранить."
    )
    assert HEDGE_DROPPED in _findings(text, [MEAT_PLANT_HEDGE])


def test_hedge_kept_in_prose_passes() -> None:
    text = "Мясокомбинат, по словам жителя, похоже, постепенно останавливается."
    assert _findings(text, [MEAT_PLANT_HEDGE]) == []


def test_plain_attribution_is_not_a_hedge_and_unhedged_source_passes() -> None:
    plain = _support(
        "story:12:evidence:0:frag:641500",
        "По сообщению жителя, мясокомбинат остановился.",
        "Мясокомбинат остановился без электричества.",
    )
    text = "Мясокомбинат, по словам жителя, остановился."
    assert _findings(text, [plain]) == []


def test_hedge_check_is_a_repair_not_a_blocker() -> None:
    codes = _quality_codes(
        "Без электричества он, по словам жителя, постепенно останавливается.",
        [MEAT_PLANT_HEDGE],
    )
    assert HEDGE_DROPPED in codes
    policy = article_quality_policy(HEDGE_DROPPED)
    assert policy.severity == "repair"
    assert policy.publication_effect == "repair_recommended"
