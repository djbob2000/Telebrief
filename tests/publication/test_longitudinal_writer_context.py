import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticlePublicationWindow,
    ArticleSupport,
)
from src.publication.article_length import derive_article_length_profile
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft
from src.publication.article_writer_context import render_article_writer_context

pytestmark = pytest.mark.unit


def _make_sup(
    support_id: str = "sup:1",
    text: str = "На улице Нагорной завершился ремонт водовода.",
    observed_at: dt.datetime | None = None,
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=observed_at or dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
    )


def _make_ctx(
    supports: tuple[ArticleSupport, ...] = (),
    window: ArticlePublicationWindow | None = None,
    edition_name: str = "Бердянск",
) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=("Итоги недели в Бердянске",),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=("utilities", "transport"),
        publication_window=window,
        edition_name=edition_name,
    )


def test_weekly_article_allows_weekly_temporal_markers():
    draft = StructuredArticleDraft(
        title="Итоги недели в Бердянске: ремонт сетей и транспорт",
        lead="За прошедшую неделю город столкнулся с масштабными восстановительными работами.",
        sections=(
            ArticleSection(
                heading="Инфраструктура",
                paragraphs=(
                    ArticleParagraph(
                        text="На улице Нагорной завершился ремонт водовода.",
                        cited_support_ids=("sup:1",),
                        claims=(
                            ArticleClaimAtom(
                                text="На улице Нагорной завершился ремонт водовода",
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        title_support_ids=("sup:1",),
        lead_support_ids=("sup:1",),
        title_claims=(
            ArticleClaimAtom(
                text="Итоги недели в Бердянске",
                cited_support_ids=("sup:1",),
            ),
        ),
        lead_claims=(
            ArticleClaimAtom(
                text="За прошедшую неделю город столкнулся с восстановительными работами",
                cited_support_ids=("sup:1",),
            ),
        ),
    )
    window = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    assert window.lookback_hours == 168
    sup = _make_sup()
    ctx = _make_ctx(supports=(sup,), window=window)
    val = validate_article_draft(draft, ctx)
    violations = [iss.code for iss in val.issues]
    assert "REPORTING_WINDOW_EXPANSION" not in violations


def test_weekly_article_rejects_monthly_temporal_markers():
    draft = StructuredArticleDraft(
        title="Итоги месяца в Бердянске: ремонт сетей и транспорт",
        lead="За прошедший месяц город столкнулся с масштабными восстановительными работами.",
        sections=(
            ArticleSection(
                heading="Инфраструктура",
                paragraphs=(
                    ArticleParagraph(
                        text="На улице Нагорной завершился ремонт водовода.",
                        cited_support_ids=("sup:1",),
                        claims=(
                            ArticleClaimAtom(
                                text="На улице Нагорной завершился ремонт водовода",
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        title_support_ids=("sup:1",),
        lead_support_ids=("sup:1",),
        title_claims=(
            ArticleClaimAtom(
                text="Итоги месяца в Бердянске",
                cited_support_ids=("sup:1",),
            ),
        ),
        lead_claims=(
            ArticleClaimAtom(
                text="За прошедший месяц город столкнулся с восстановительными работами",
                cited_support_ids=("sup:1",),
            ),
        ),
    )
    window = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    sup = _make_sup()
    ctx = _make_ctx(supports=(sup,), window=window)
    val = validate_article_draft(draft, ctx)
    violations = [iss.code for iss in val.issues]
    assert "REPORTING_WINDOW_EXPANSION" in violations


def test_monthly_article_allows_monthly_temporal_markers():
    draft = StructuredArticleDraft(
        title="Итоги месяца в Бердянске: панорама городской жизни",
        lead="За прошедший месяц город столкнулся с масштабными восстановительными работами.",
        sections=(
            ArticleSection(
                heading="Инфраструктура",
                paragraphs=(
                    ArticleParagraph(
                        text="На улице Нагорной завершился ремонт водовода.",
                        cited_support_ids=("sup:1",),
                        claims=(
                            ArticleClaimAtom(
                                text="На улице Нагорной завершился ремонт водовода",
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        title_support_ids=("sup:1",),
        lead_support_ids=("sup:1",),
        title_claims=(
            ArticleClaimAtom(
                text="Итоги месяца в Бердянске",
                cited_support_ids=("sup:1",),
            ),
        ),
        lead_claims=(
            ArticleClaimAtom(
                text="За прошедший месяц город столкнулся с восстановительными работами",
                cited_support_ids=("sup:1",),
            ),
        ),
    )
    window = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
    )
    assert window.lookback_hours >= 720
    sup = _make_sup(observed_at=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc))
    ctx = _make_ctx(supports=(sup,), window=window)
    val = validate_article_draft(draft, ctx)
    violations = [iss.code for iss in val.issues]
    assert "REPORTING_WINDOW_EXPANSION" not in violations


def test_length_profile_scales_for_weekly_and_monthly():
    cfg = PublicationEditorialConfig()
    window_weekly = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    ctx_weekly = _make_ctx(window=window_weekly)
    prof_weekly = derive_article_length_profile(ctx_weekly, cfg)
    assert prof_weekly.target_min_words >= 1500
    assert prof_weekly.target_max_words >= 2500

    window_monthly = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
    )
    ctx_monthly = _make_ctx(window=window_monthly)
    prof_monthly = derive_article_length_profile(ctx_monthly, cfg)
    assert prof_monthly.target_min_words >= 3000
    assert prof_monthly.target_max_words >= 4500


def test_render_article_writer_context_includes_longitudinal_directive():
    window = ArticlePublicationWindow(
        snapshot_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc),
    )
    ctx = _make_ctx(window=window)
    rendered = render_article_writer_context(ctx)
    assert "LONGITUDINAL PUBLICATION DIRECTIVE:" in rendered
    assert "Городской горизонт" in rendered
