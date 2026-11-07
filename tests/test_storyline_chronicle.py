# ruff: noqa: S101
"""Unit tests for the storyline chronicle and period-review periods (AGENTS.md §0.12)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from src.publication.chronicle import (
    ChronicleReport,
    ChronicleStory,
    MergedStoryline,
    StoredEntry,
    _rollup_batches,
    child_periods,
    clip_sentences,
    entry_weight,
    merge_children,
    period_for,
    periods_between,
    posted_day_text,
    select_facts,
    split_inline_refs,
    story_theme,
    validate_rollup,
    validate_week_storylines,
    week_segments,
    weight_score,
)
from src.publication.period_review import (
    review_label,
    review_lookback_hours,
    review_period,
)

UTC = dt.UTC
KYIV = ZoneInfo("Europe/Kyiv")


def _report(ref: str, story_id: int, day: int, text: str, source_id: int = 7) -> ChronicleReport:
    return ChronicleReport(
        ref=ref,
        story_id=story_id,
        observed_at=dt.datetime(2026, 10, day, 10, tzinfo=UTC),
        source_id=source_id,
        evidence_text=text,
        source_text=text,
    )


def _story(story_id: int, reports: tuple[ChronicleReport, ...]) -> ChronicleStory:
    return ChronicleStory(
        story_id=story_id,
        theme="power",
        headline=f"Story {story_id}",
        first_seen_at=reports[0].observed_at,
        reports=reports,
    )


# ---------------------------------------------------------------------------
# Periods


def test_period_for_covers_each_kind_including_year_end() -> None:
    day = dt.date(2026, 12, 31)
    assert period_for("month", day) == period_for("month", dt.date(2026, 12, 1))
    assert period_for("month", day).end == dt.date(2027, 1, 1)
    assert period_for("quarter", day).start == dt.date(2026, 10, 1)
    assert period_for("quarter", day).end == dt.date(2027, 1, 1)
    assert period_for("half_year", day).start == dt.date(2026, 7, 1)
    assert period_for("year", day).start == dt.date(2026, 1, 1)
    assert period_for("half_year", dt.date(2026, 6, 30)).start == dt.date(2026, 1, 1)


def test_week_segments_split_at_month_boundary() -> None:
    segments = week_segments(dt.date(2026, 9, 28), dt.date(2026, 10, 12))
    # The week of 28 September is split at 1 October, not at its Sunday.
    assert segments[0].end == dt.date(2026, 10, 1)
    # No week crosses a month boundary.
    for segment in segments:
        assert segment.start.month == segment.last_day.month
    assert segments[0].start == dt.date(2026, 9, 28)
    assert segments[-1].end == dt.date(2026, 10, 12)
    # Segments tile the range without gaps.
    for left, right in zip(segments, segments[1:], strict=False):
        assert left.end == right.start


def test_periods_between_lists_every_overlapping_month() -> None:
    months = periods_between("month", dt.date(2026, 9, 11), dt.date(2026, 11, 7))
    assert [p.start for p in months] == [
        dt.date(2026, 9, 1),
        dt.date(2026, 10, 1),
        dt.date(2026, 11, 1),
    ]


def test_child_periods_roll_up_exactly_the_parent() -> None:
    quarter = period_for("quarter", dt.date(2026, 7, 15))
    months = child_periods(quarter)
    assert [m.start.month for m in months] == [7, 8, 9]
    year = period_for("year", dt.date(2026, 5, 1))
    assert [q.start.month for q in child_periods(year)] == [1, 4, 7, 10]
    october = period_for("month", dt.date(2026, 10, 1))
    weeks = child_periods(october)
    assert weeks[0].start == dt.date(2026, 10, 1)
    assert weeks[-1].end == dt.date(2026, 11, 1)
    assert sum((w.end - w.start).days for w in weeks) == 31


# ---------------------------------------------------------------------------
# Review windows and labels


def test_monthly_review_on_first_covers_previous_month() -> None:
    slot = dt.datetime(2026, 11, 1, 18, 0, tzinfo=KYIV)
    period = review_period("monthly_article", slot, KYIV)
    assert (period.start, period.end) == (dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    # Lookback is the whole period up to the slot. The period contains the
    # 25 October switch to winter time, so 31 local days take 744 + 1 hours.
    assert review_lookback_hours("monthly_article", slot, KYIV) == 31 * 24 + 1 + 18


def test_quarter_half_year_and_year_reviews_pick_the_closed_period() -> None:
    quarterly = review_period("quarterly_article", dt.datetime(2026, 10, 3, 18, tzinfo=KYIV), KYIV)
    assert quarterly.start == dt.date(2026, 7, 1)
    half = review_period("half_year_article", dt.datetime(2027, 1, 5, 18, tzinfo=KYIV), KYIV)
    assert half.start == dt.date(2026, 7, 1)
    yearly = review_period("yearly_article", dt.datetime(2027, 1, 7, 18, tzinfo=KYIV), KYIV)
    assert yearly.start == dt.date(2026, 1, 1)


def test_partial_period_label_names_real_coverage() -> None:
    period = period_for("year", dt.date(2026, 6, 1))
    assert review_label(period, dt.date(2026, 9, 11)) == ("2026 года: 11 сентября – 31 декабря")
    assert review_label(period, dt.date(2026, 1, 1)) == "2026 года"


def test_partial_month_label_and_full_month_label() -> None:
    october = period_for("month", dt.date(2026, 10, 1))
    assert review_label(october, dt.date(2026, 10, 1)) == "октября 2026 года"
    assert review_label(october, dt.date(2026, 10, 5)) == "октября 2026 года: 5–31 октября"


# ---------------------------------------------------------------------------
# Themes, weights and text helpers


def test_story_theme_follows_service_family() -> None:
    assert story_theme("Нет света на Горе", "жители жалуются") == "power"
    assert story_theme("Автобус №4 не ходит", "") == "transport"
    assert story_theme("Ничего особенного", "") == "other"


def test_weight_score_prefers_longer_and_wider_storylines() -> None:
    reports = (_report("fragment:1", 1, 5, "a", 7), _report("fragment:2", 2, 9, "b", 8))
    stories = [_story(1, reports[:1]), _story(2, reports[1:])]
    weight = entry_weight(stories, UTC)
    assert weight["days"] == ["2026-10-05", "2026-10-09"]
    assert weight["sources"] == 2
    short = {"days": ["2026-10-05"], "sources": 1, "stories": 1}
    assert weight_score(weight) > weight_score(short)


def test_posted_day_text_is_russian_and_rejects_bad_dates() -> None:
    assert posted_day_text("2026-10-05") == "5 октября 2026 года 05.10.2026"
    assert posted_day_text("not-a-date") == ""


def test_split_inline_refs_moves_markers_out_of_prose() -> None:
    text, refs = split_inline_refs("Вода есть (fragment:11, fragment:12) на Горе.")
    assert "fragment" not in text
    assert refs == ["fragment:11", "fragment:12"]


def test_clip_sentences_cuts_at_sentence_end() -> None:
    text = "Первое предложение очень длинное здесь. Второе предложение продолжается дальше и дальше"
    assert clip_sentences(text, 60) == "Первое предложение очень длинное здесь."


# ---------------------------------------------------------------------------
# Week validation: only grounded statements survive


def _week_stories() -> list[ChronicleStory]:
    return [
        _story(
            1,
            (_report("fragment:11", 1, 5, "На Горе нет света 3 часа"),),
        ),
        _story(
            2,
            (_report("fragment:21", 2, 6, "На Дмитрова вода есть"),),
        ),
    ]


def test_week_entry_keeps_grounded_fact_and_drops_invented_number() -> None:
    stories = _week_stories()
    parsed = {
        "storylines": [
            {
                "storyline": "new",
                "title": "Свет на Горе",
                "story_ids": [1],
                "facts": [
                    {
                        "date": "2026-10-05",
                        "text": "На Горе нет света 3 часа",
                        "refs": ["fragment:11"],
                    },
                    {
                        "date": "2026-10-05",
                        "text": "На Горе нет света 7 часов",
                        "refs": ["fragment:11"],
                    },
                    {"date": "2026-10-05", "text": "Есть", "refs": ["fragment:99"]},
                ],
                "summary": "На Горе нет света 3 часа.",
                "summary_refs": ["fragment:11"],
                "details": [],
                "areas": ["Гора"],
            }
        ]
    }
    stats: dict[str, int] = {}
    drafts = validate_week_storylines(
        parsed,
        theme="power",
        stories=stories,
        known_keys=set(),
        claimed=set(),
        tz=UTC,
        new_key=lambda: "sl:new",
        stats=stats,
    )
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.storyline_key == "sl:new"
    assert [fact["text"] for fact in draft.facts] == ["На Горе нет света 3 часа"]
    assert draft.facts[0]["refs"] == ["fragment:11"]
    assert draft.trajectory == "На Горе нет света 3 часа."
    assert stats.get("facts_dropped", 0) == 2
    assert draft.method == "model"


def test_week_entry_ignores_stories_outside_input_and_double_claims() -> None:
    stories = _week_stories()
    parsed = {
        "storylines": [
            {"storyline": "a", "story_ids": [1, 404], "facts": [], "title": "A"},
            {"storyline": "b", "story_ids": [1], "facts": [], "title": "B"},
        ]
    }
    claimed: set[int] = set()
    drafts = validate_week_storylines(
        parsed,
        theme="power",
        stories=stories,
        known_keys={"a"},
        claimed=claimed,
        tz=UTC,
        new_key=lambda: "sl:new",
        stats={},
    )
    # Story 1 is claimed once; story 404 is not in the input at all.
    assert [d.story_ids for d in drafts] == [[1]]
    assert drafts[0].storyline_key == "a"
    assert claimed == {1}


def test_week_entry_falls_back_to_deterministic_facts_without_model_facts() -> None:
    stories = _week_stories()
    parsed = {"storylines": [{"storyline": "x", "story_ids": [2], "facts": [], "title": ""}]}
    drafts = validate_week_storylines(
        parsed,
        theme="water",
        stories=stories,
        known_keys=set(),
        claimed=set(),
        tz=UTC,
        new_key=lambda: "sl:new",
        stats={},
    )
    assert drafts[0].method == "deterministic"
    assert drafts[0].facts[0]["refs"] == ["fragment:21"]


def test_malformed_week_answer_yields_no_entries() -> None:
    stats: dict[str, int] = {}
    drafts = validate_week_storylines(
        {"storylines": "nope"},
        theme="power",
        stories=_week_stories(),
        known_keys=set(),
        claimed=set(),
        tz=UTC,
        new_key=lambda: "sl:new",
        stats=stats,
    )
    assert drafts == []
    assert stats["malformed"] == 1


# ---------------------------------------------------------------------------
# Rollups: children only, no refs from elsewhere


def _stored(period_start: dt.date, facts: list[dict], refs: dict[str, dict]) -> StoredEntry:
    period = period_for("month", period_start)
    return StoredEntry(
        storyline_id=5,
        theme="power",
        storyline_kind="named",
        period=period,
        title="Свет",
        trajectory="Траектория.",
        facts=facts,
        details=[],
        weight={"stories": 1, "sources": 1, "reports": 1, "days": [], "source_ids": [7]},
        ref_texts=refs,
        metadata={"trajectory_refs": []},
    )


def test_merge_children_drops_facts_outside_the_parent_period() -> None:
    october_fact = {"date": "2026-10-05", "text": "a", "refs": ["fragment:11"], "sources": 1}
    november_fact = {"date": "2026-11-02", "text": "b", "refs": ["fragment:12"], "sources": 1}
    child = _stored(dt.date(2026, 10, 1), [october_fact, november_fact], {})
    merged = merge_children([child], period_for("month", dt.date(2026, 10, 1)))
    assert [f["text"] for f in merged.facts] == ["a"]


def test_select_facts_keeps_best_sourced_then_orders_by_date() -> None:
    facts = [
        {"date": "2026-10-09", "text": "many", "sources": 3, "refs": ["r1"]},
        {"date": "2026-10-02", "text": "single", "sources": 1, "refs": ["r2"]},
        {"date": "2026-10-20", "text": "double", "sources": 2, "refs": ["r3"]},
    ]
    chosen = select_facts(facts, cap=2)
    # The two best-sourced facts are kept, then listed in date order.
    assert [f["text"] for f in chosen] == ["many", "double"]


def test_validate_rollup_rejects_refs_that_are_not_child_refs() -> None:
    ref_texts = {
        "fragment:11": {
            "evidence": "На Горе нет света",
            "source": "На Горе нет света",
            "story_id": 1,
            "date": "2026-10-05",
        }
    }
    child = _stored(
        dt.date(2026, 10, 1),
        [
            {
                "date": "2026-10-05",
                "text": "На Горе нет света",
                "refs": ["fragment:11"],
                "sources": 1,
                "story_ids": [1],
            }
        ],
        ref_texts,
    )
    period = period_for("month", dt.date(2026, 10, 1))
    merged: MergedStoryline = merge_children([child], period)
    parsed = {
        "storylines": [
            {
                "storyline": "sl:5",
                "title": "Свет",
                "facts": [
                    {"date": "2026-10-05", "text": "На Горе нет света", "refs": ["fragment:999"]},
                    {"date": "2026-10-05", "text": "На Горе нет света", "refs": ["fragment:11"]},
                ],
                "summary": "",
                "summary_refs": [],
            }
        ]
    }
    stats: dict[str, int] = {}
    out = validate_rollup(parsed, batch={"sl:5": merged}, period=period, cap=12, stats=stats)
    assert list(out) == ["sl:5"]
    assert [f["refs"] for f in out["sl:5"].facts] == [["fragment:11"]]
    assert stats["facts_dropped"] == 1


def test_rollup_batches_split_by_character_budget() -> None:
    child = _stored(dt.date(2026, 10, 1), [], {})
    merged = merge_children([child], period_for("month", dt.date(2026, 10, 1)))
    rows = [(merged, {"blob": "x" * 60}) for _ in range(3)]
    batches = _rollup_batches(rows, budget=100)
    assert [len(b) for b in batches] == [1, 1, 1]
    assert len(_rollup_batches(rows, budget=10_000)) == 1
