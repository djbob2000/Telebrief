# ruff: noqa: S101
"""article-fidelity W1.3 and W1.4: historical material and relative time in article prose."""

from __future__ import annotations

import datetime as dt

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality_policy import (
    ARTICLE_QUALITY_FINDING_POLICIES,
    article_quality_policy,
)
from src.publication.article_temporal_checks import (
    HISTORICAL_PRESENTED_AS_CURRENT,
    RELATIVE_TIME_COPIED,
    RELATIVE_TIME_MISDATED,
    TEMPORAL_SENTENCE_TRIM_CODES,
    temporal_prose_findings,
    temporal_quality_findings,
)
from src.publication.article_validator import validate_article_draft

UTC = dt.timezone.utc
# Article day: the report window closes on 12 November (UTC, edition timezone UTC).
SNAPSHOT = dt.datetime(2026, 11, 12, 18, 48, tzinfo=UTC)
WINDOW_START = dt.datetime(2026, 11, 11, 21, 51, 27, tzinfo=UTC)
# Message sent on 11 November, 18:53 UTC: the source of the historical outage.
SENT_NOV_11 = dt.datetime(2026, 11, 11, 18, 53, tzinfo=UTC)
SENT_NOV_10 = dt.datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
SENT_NOV_12 = dt.datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _support(
    support_id: str,
    text: str,
    *,
    role: str = "CURRENT_WINDOW",
    sent_at: dt.datetime | None = None,
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=sent_at,
        temporal_role=role,  # type: ignore[arg-type]
        evidence_kind="community_report",
        story_id=f"story:{support_id}",
        sent_at=sent_at,
    )


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        publication_window=PublicationWindow(snapshot_at=SNAPSHOT, lookback_start=WINDOW_START),
        edition_timezone="UTC",
    )


def _findings(text: str, cited: tuple[str, ...], context: ArticleEditorialContext):
    atoms: tuple[ArticleClaimAtom, ...] = ()
    return temporal_prose_findings(
        unit_id="P001",
        unit_type="paragraph",
        unit_text=text,
        cited_ids=cited,
        claim_atoms=atoms,
        support_map=context.support_by_id,
        context=context,
    )


HISTORICAL_OUTAGE = _support(
    "h1",
    "Свет дали с 11 до 15.",
    role="HISTORICAL_CONTEXT",
    sent_at=SENT_NOV_11,
)


# --- W1.3: historical material presented as current -------------------------------


def test_historical_support_with_present_time_word_is_blocked() -> None:
    # Defect (D3): "сегодня дали с 11 до 15" from a message sent on 11 November.
    context = _context(HISTORICAL_OUTAGE)

    found = _findings("Сегодня свет дали с 11 до 15.", ("h1",), context)

    assert [f.code for f in found] == [HISTORICAL_PRESENTED_AS_CURRENT]
    assert found[0].claim_text == "Сегодня свет дали с 11 до 15."
    assert found[0].unit_id == "P001"


def test_historical_support_dated_by_its_own_day_passes() -> None:
    # Faithful: the historical event carries its own date.
    context = _context(HISTORICAL_OUTAGE)

    found = _findings("11 ноября, по словам жительницы, свет дали с 11 до 15.", ("h1",), context)

    assert found == ()


def test_duration_without_a_date_is_present_time() -> None:
    context = _context(HISTORICAL_OUTAGE)

    found = _findings("Без света уже несколько дней.", ("h1",), context)

    assert [f.code for f in found] == [HISTORICAL_PRESENTED_AS_CURRENT]


def test_present_time_word_quoted_by_a_resident_is_not_flagged() -> None:
    # Quotations are exact wording and are never edited by the check.
    context = _context(HISTORICAL_OUTAGE)

    found = _findings("11 ноября жительница писала: «сегодня дали свет».", ("h1",), context)

    assert found == ()


def test_present_time_sentence_with_a_current_support_passes() -> None:
    current = _support("c1", "Свет дали с 9 до 12.", sent_at=SENT_NOV_12)
    context = _context(HISTORICAL_OUTAGE, current)

    found = _findings("Сегодня свет дали с 9 до 12.", ("c1",), context)

    assert found == ()


def test_historical_rule_applies_to_paragraphs_not_to_the_lead() -> None:
    # The lead has its own continuation rule (HISTORICAL_CONTEXT_UNFRAMED).
    context = _context(HISTORICAL_OUTAGE)

    found = temporal_prose_findings(
        unit_id="LEAD",
        unit_type="lead",
        unit_text="Сегодня свет дали с 11 до 15.",
        cited_ids=("h1",),
        claim_atoms=(),
        support_map=context.support_by_id,
        context=context,
    )

    assert all(f.code != HISTORICAL_PRESENTED_AS_CURRENT for f in found)


# --- W1.4: relative day words resolved against the source -------------------------


def test_today_for_a_report_sent_on_another_day_is_misdated() -> None:
    # Source sent on 11 November; the article is written on 12 November.
    context = _context(_support("c1", "Свет дали с 11 до 15.", sent_at=SENT_NOV_11))

    found = _findings("Сегодня свет дали с 11 до 15.", ("c1",), context)

    assert [f.code for f in found] == [RELATIVE_TIME_MISDATED]
    assert RELATIVE_TIME_MISDATED in TEMPORAL_SENTENCE_TRIM_CODES


def test_today_for_a_report_sent_today_passes() -> None:
    context = _context(_support("c1", "Свет дали с 9 до 12.", sent_at=SENT_NOV_12))

    assert _findings("Сегодня свет дали с 9 до 12.", ("c1",), context) == ()


def test_yesterday_for_a_report_sent_the_day_before_passes() -> None:
    context = _context(_support("c1", "Свет дали с 11 до 15.", sent_at=SENT_NOV_11))

    assert _findings("Вчера свет дали с 11 до 15.", ("c1",), context) == ()


def test_yesterday_for_a_report_sent_two_days_before_is_misdated() -> None:
    context = _context(_support("c1", "Свет дали с 11 до 15.", sent_at=SENT_NOV_10))

    found = _findings("Вчера свет дали с 11 до 15.", ("c1",), context)

    assert [f.code for f in found] == [RELATIVE_TIME_MISDATED]


def test_relative_word_without_a_sent_time_falls_back_to_collection_time() -> None:
    # Only observed_at is known: it still anchors the day for the check.
    support = ArticleSupport(
        support_id="c2",
        text="Свет дали с 9 до 12.",
        source_text="Свет дали с 9 до 12.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=SENT_NOV_11,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        story_id="story:c2",
    )
    context = _context(support)

    assert _findings("Сегодня свет дали с 9 до 12.", ("c2",), context) != ()


def test_backstop_codes_cover_the_blocking_temporal_findings() -> None:
    assert HISTORICAL_PRESENTED_AS_CURRENT in TEMPORAL_SENTENCE_TRIM_CODES
    assert RELATIVE_TIME_MISDATED in TEMPORAL_SENTENCE_TRIM_CODES
    assert RELATIVE_TIME_COPIED not in TEMPORAL_SENTENCE_TRIM_CODES


# --- W1.4: copied chat elapsed time is a reader-quality repair --------------------


def _draft(*paragraphs: ArticleParagraph) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("c1",),
        lead="Лид.",
        lead_support_ids=("c1",),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("c1",),
                paragraphs=paragraphs,
            ),
        ),
    )


def test_copied_elapsed_time_is_a_repair_finding() -> None:
    # Defect (D4): the chat's "около пяти минут назад".
    context = _context(_support("c1", "Да 5 минут назад дали.", sent_at=SENT_NOV_12))
    draft = _draft(
        ArticleParagraph(
            text="Подачу воды дали около пяти минут назад.",
            cited_support_ids=("c1",),
        )
    )

    found = temporal_quality_findings(draft, context)

    assert [f.code for f in found] == [RELATIVE_TIME_COPIED]
    assert found[0].unit_id == "P001"
    assert article_quality_policy(RELATIVE_TIME_COPIED).severity == "repair"


def test_dated_or_clock_time_paragraph_passes_the_elapsed_time_check() -> None:
    context = _context(_support("c1", "Да 5 минут назад дали.", sent_at=SENT_NOV_12))
    draft = _draft(
        ArticleParagraph(
            text="Подачу воды дали в 13:00, по словам жителя.",
            cited_support_ids=("c1",),
        )
    )

    assert temporal_quality_findings(draft, context) == ()


def test_quoted_elapsed_time_is_not_a_repair_finding() -> None:
    context = _context(_support("c1", "Да 5 минут назад дали.", sent_at=SENT_NOV_12))
    draft = _draft(
        ArticleParagraph(
            text="Житель написал: «Да 5 минут назад дали».",
            cited_support_ids=("c1",),
        )
    )

    assert temporal_quality_findings(draft, context) == ()


def test_temporal_quality_codes_are_registered_as_repairs() -> None:
    for code in (RELATIVE_TIME_COPIED, "REDUNDANT_PARAGRAPH"):
        policy = ARTICLE_QUALITY_FINDING_POLICIES[code]
        assert policy.severity == "repair"
        assert policy.publication_effect == "repair_recommended"


# --- validator integration: the blocking finding reaches the draft result ---------


def _validate(paragraph_text: str, cited: tuple[str, ...], context: ArticleEditorialContext):
    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("c1",),
        lead="Лид о событиях.",
        lead_support_ids=("c1",),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("c1",),
                paragraphs=(
                    ArticleParagraph(
                        text=paragraph_text,
                        cited_support_ids=cited,
                        claims=(),
                    ),
                ),
            ),
        ),
    )
    return validate_article_draft(draft, context)


def test_validator_reports_historical_presented_as_current_as_blocking() -> None:
    context = _context(
        _support("c1", "Свет дали с 9 до 12.", sent_at=SENT_NOV_12),
        HISTORICAL_OUTAGE,
    )

    result = _validate("Сегодня свет дали с 11 до 15.", ("h1",), context)

    codes = [(issue.code, issue.blocking) for issue in result.issues]
    assert (HISTORICAL_PRESENTED_AS_CURRENT, True) in codes
    flagged = next(i for i in result.issues if i.code == HISTORICAL_PRESENTED_AS_CURRENT)
    assert flagged.claim_text == "Сегодня свет дали с 11 до 15."
