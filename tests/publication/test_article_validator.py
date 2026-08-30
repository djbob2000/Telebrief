"""Tests for deterministic ArticleValidator."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def _make_sample_context() -> ArticleEditorialContext:
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Авария на подстанции: временно обесточена центральная и нагорная часть Бердянска. Бригада РЭС ведет восстановительные работы.",
        source_text="Авария на подстанции: временно обесточена центральная и нагорная часть Бердянска. Бригада РЭС ведет восстановительные работы.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:201",
        text="В художественном музее открылась выставка картин местных авторов.",
        source_text="В художественном музее открылась выставка картин местных авторов.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(201,),
        source_item_ids=(2,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    sup_ctx = ArticleSupport(
        support_id="story:3:evidence:0:frag:301",
        text="Историческая справка о музее.",
        source_text="Историческая справка о музее.",
        support_kind="evidence",
        publication_use="CONTEXT",
        source_refs=("ref-3",),
        fragment_ids=(301,),
        source_item_ids=(3,),
        observed_at=_NOW,
        temporal_role="HISTORICAL_CONTEXT",
    )
    support_index = (sup1, sup2, sup_ctx)
    return ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=support_index,
        support_by_id={s.support_id: s for s in support_index},
        recurring_topics=("utilities", "culture"),
    )


@pytest.mark.unit
def test_valid_draft_passes_validation() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(
        article_min_words=10,
        article_max_words=200,
        article_min_sections=2,
        article_max_sections=4,
    )

    draft = StructuredArticleDraft(
        title="Авария на подстанции в Бердянске",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(
                text="Авария на подстанции в Бердянске",
                cited_support_ids=("story:1:evidence:0:frag:101",),
            ),
        ),
        lead="В центральной и нагорной части Бердянска обесточены дома из-за аварии на подстанции.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(
                text="В центральной и нагорной части Бердянска авария на подстанции",
                cited_support_ids=("story:1:evidence:0:frag:101",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Авария на подстанции",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Авария на подстанции",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Бригада РЭС ведет восстановительные работы на подстанции в центральной части.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Бригада РЭС ведет восстановительные работы",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
            ArticleSection(
                heading="Выставка в музее",
                heading_support_ids=("story:2:evidence:0:frag:201",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Выставка в музее",
                        cited_support_ids=("story:2:evidence:0:frag:201",),
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="В художественном музее открылась выставка картин местных авторов.",
                        cited_support_ids=("story:2:evidence:0:frag:201",),
                        claims=(
                            ArticleClaimAtom(
                                text="В художественном музее открылась выставка картин местных авторов",
                                cited_support_ids=("story:2:evidence:0:frag:201",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=35,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is True
    assert result.issues == ()
    assert result.violations == ()


@pytest.mark.unit
def test_draft_missing_support_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=(),  # Missing title support
        title_claims=(),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф без поддержки.",
                        cited_support_ids=(),  # Missing paragraph support
                        claims=(),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    codes = [iss.code for iss in result.issues]
    assert "MISSING_SUPPORT:title" in codes
    assert "MISSING_SUPPORT:paragraph" in codes


@pytest.mark.unit
def test_draft_with_unknown_support_id_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:999:evidence:0:frag:999",),
                        claims=(
                            ArticleClaimAtom(
                                text="Текст параграфа",
                                cited_support_ids=("story:999:evidence:0:frag:999",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(
        iss.code in ("UNKNOWN_SUPPORT_ID", "UNKNOWN_CLAIM_SUPPORT_ID") for iss in result.issues
    )
    assert "story:999:evidence:0:frag:999" in result.unknown_evidence_ids


@pytest.mark.unit
def test_draft_publication_policy_requires_publish_support() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # Title citing only CONTEXT support must fail
    draft = StructuredArticleDraft(
        title="Заголовок из контекста",
        title_support_ids=("story:3:evidence:0:frag:301",),
        title_claims=(
            ArticleClaimAtom(
                text="Заголовок из контекста",
                cited_support_ids=("story:3:evidence:0:frag:301",),
            ),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Текст параграфа",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(
        iss.code == "INVALID_SUPPORT_POLICY" and iss.unit_id == "TITLE" for iss in result.issues
    )


@pytest.mark.unit
def test_draft_unsupported_specifics_fail() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # 1. Invented duration: "в течение полутора часов"
    draft_time = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Бригады восстановили питание в течение полутора часов.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Бригады восстановили питание в течение полутора часов",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_time = validate_article_draft(draft_time, ctx, config)
    assert res_time.is_valid is False
    assert any(
        iss.code in ("UNSUPPORTED_CONCRETE_CLAIM", "UNSUPPORTED_CLAIM_ATOM")
        for iss in res_time.issues
    )

    # 2. Invented mechanism: "по резервной схеме"
    draft_mech = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Энергоснабжение восстановили по резервной схеме.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Энергоснабжение восстановили по резервной схеме",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_mech = validate_article_draft(draft_mech, ctx, config)
    assert res_mech.is_valid is False
    assert any(
        iss.code in ("UNSUPPORTED_MECHANISM", "UNSUPPORTED_CLAIM_ATOM") for iss in res_mech.issues
    )

    # 3. Invented cause: "из-за гидроудара"
    draft_cause = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Давление упало из-за гидроудара.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Давление упало из-за гидроудара",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_cause = validate_article_draft(draft_cause, ctx, config)
    assert res_cause.is_valid is False
    assert any(
        iss.code in ("UNSUPPORTED_CAUSAL_RELATION", "UNSUPPORTED_CLAIM_ATOM")
        for iss in res_cause.issues
    )


@pytest.mark.unit
def test_draft_internal_handle_leak_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Факт с утечкой ручки [story:1:evidence:0:frag:101] в тексте.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Факт с утечкой ручки в тексте",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(iss.code == "INTERNAL_HANDLE_LEAK" for iss in result.issues)


@pytest.mark.unit
def test_draft_missing_claim_atoms_and_support_mismatch() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # 1. Missing claim atoms
    from src.publication.article_models import ArticleClaimAtom

    draft_no_claims = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res = validate_article_draft(draft_no_claims, ctx, config)
    assert res.is_valid is False
    codes = [iss.code for iss in res.issues]
    assert "MISSING_CLAIM_ATOMS" in codes

    # 2. Support mismatch: unit cites frag:101 and frag:201, but claim atom only cites frag:101
    draft_mismatch = StructuredArticleDraft(
        title="Авария на подстанции в Бердянске",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(
                text="Авария на подстанции в Бердянске",
                cited_support_ids=("story:1:evidence:0:frag:101",),
            ),
        ),
        lead="В центральной части Бердянска обесточены дома из-за аварии на подстанции.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(
                text="В центральной части Бердянска авария на подстанции",
                cited_support_ids=("story:1:evidence:0:frag:101",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Авария на подстанции",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Бригада РЭС ведет восстановительные работы.",
                        cited_support_ids=(
                            "story:1:evidence:0:frag:101",
                            "story:2:evidence:0:frag:201",
                        ),
                        claims=(
                            ArticleClaimAtom(
                                text="Бригада РЭС ведет восстановительные работы",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_mismatch = validate_article_draft(draft_mismatch, ctx, config)

    assert res_mismatch.is_valid is True
    mismatch_issue = next(
        iss for iss in res_mismatch.issues if iss.code == "CLAIM_SUPPORT_MISMATCH"
    )
    assert mismatch_issue.severity == "warning"
    assert mismatch_issue.blocking is False


@pytest.mark.unit
def test_draft_with_unknown_claim_support_id_is_blocking_error() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft_unknown = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Текст параграфа",
                                cited_support_ids=("story:999:unknown",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_unknown = validate_article_draft(draft_unknown, ctx, config)
    assert res_unknown.is_valid is False
    assert any(
        iss.code == "UNKNOWN_CLAIM_SUPPORT_ID" and iss.blocking for iss in res_unknown.issues
    )


@pytest.mark.unit
def test_draft_reporting_window_expansion_fails() -> None:
    from src.publication.article_context import PublicationWindow
    from src.publication.article_models import ArticleClaimAtom

    window = PublicationWindow(
        snapshot_at=_NOW,
        lookback_start=_NOW - dt.timedelta(hours=24),
    )
    base_ctx = _make_sample_context()
    ctx = ArticleEditorialContext(
        headline_candidates=base_ctx.headline_candidates,
        support_index=base_ctx.support_index,
        support_by_id=base_ctx.support_by_id,
        recurring_topics=base_ctx.recurring_topics,
        publication_window=window,
    )
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Бердянск: хроника недели и события",
        title_support_ids=("story:1:evidence:0:frag:101",),
        title_claims=(
            ArticleClaimAtom(
                text="Бердянск: хроника недели и события",
                cited_support_ids=("story:1:evidence:0:frag:101",),
            ),
        ),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        lead_claims=(
            ArticleClaimAtom(text="Лид статьи", cited_support_ids=("story:1:evidence:0:frag:101",)),
        ),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Секция", cited_support_ids=("story:1:evidence:0:frag:101",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                        claims=(
                            ArticleClaimAtom(
                                text="Текст параграфа",
                                cited_support_ids=("story:1:evidence:0:frag:101",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    res = validate_article_draft(draft, ctx, config)
    assert res.is_valid is False
    assert any(iss.code == "REPORTING_WINDOW_EXPANSION" for iss in res.issues)


@pytest.mark.unit
def test_draft_temporal_framing_validation() -> None:
    from src.publication.article_context import PublicationWindow
    from src.publication.article_models import ArticleClaimAtom

    window = PublicationWindow(
        snapshot_at=_NOW,
        lookback_start=_NOW - dt.timedelta(hours=24),
    )
    sup_hist = ArticleSupport(
        support_id="story:hist:frag:901",
        text="Неделю назад на подстанции проводились плановые замеры.",
        source_text="Неделю назад на подстанции проводились плановые замеры.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-hist",),
        fragment_ids=(901,),
        source_item_ids=(9,),
        observed_at=_NOW - dt.timedelta(days=7),
        temporal_role="HISTORICAL_CONTEXT",
    )
    sup_curr = ArticleSupport(
        support_id="story:curr:frag:902",
        text="В центре города отключено электроснабжение.",
        source_text="В центре города аварийное отключение электричества.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-curr",),
        fragment_ids=(902,),
        source_item_ids=(10,),
        observed_at=_NOW - dt.timedelta(hours=2),
        temporal_role="CURRENT_WINDOW",
    )
    sup_fut = ArticleSupport(
        support_id="op:gas:sched",
        text="30 августа с 08:00 до 17:00 запланировано отключение газа.",
        source_text="30 августа с 08:00 до 17:00 запланировано отключение газа.",
        support_kind="operational",
        publication_use="PUBLISH",
        source_refs=("ref-fut",),
        fragment_ids=(903,),
        source_item_ids=(11,),
        observed_at=_NOW - dt.timedelta(hours=2),
        effective_from=_NOW + dt.timedelta(days=1),
        effective_until=_NOW + dt.timedelta(days=1, hours=9),
        temporal_role="FUTURE_SCHEDULED",
    )

    support_index = (sup_hist, sup_curr, sup_fut)
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=support_index,
        support_by_id={s.support_id: s for s in support_index},
        recurring_topics=("utilities",),
        publication_window=window,
    )
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # 1. Lead citing only HISTORICAL_CONTEXT fails
    draft_hist_only = StructuredArticleDraft(
        title="Отключение света в центре",
        title_support_ids=("story:curr:frag:902",),
        title_claims=(
            ArticleClaimAtom(
                text="Отключение света в центре", cited_support_ids=("story:curr:frag:902",)
            ),
        ),
        lead="Жители на протяжении последней недели сталкиваются с отключениями.",
        lead_support_ids=("story:hist:frag:901",),
        lead_claims=(
            ArticleClaimAtom(
                text="Жители сталкиваются с отключениями",
                cited_support_ids=("story:hist:frag:901",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Энергоснабжение",
                heading_support_ids=("story:curr:frag:902",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Энергоснабжение", cited_support_ids=("story:curr:frag:902",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="В центре города отключено электроснабжение.",
                        cited_support_ids=("story:curr:frag:902",),
                        claims=(
                            ArticleClaimAtom(
                                text="В центре города отключено электроснабжение",
                                cited_support_ids=("story:curr:frag:902",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=15,
    )
    res_hist = validate_article_draft(draft_hist_only, ctx, config)
    assert res_hist.is_valid is False
    codes = [iss.code for iss in res_hist.issues]
    assert "HISTORICAL_CONTEXT_UNFRAMED" in codes or "INVALID_SUPPORT_POLICY" in codes

    # 2. Historical + Current with continuation framing passes
    draft_hist_valid = StructuredArticleDraft(
        title="Отключение электричества в центре",
        title_support_ids=("story:curr:frag:902",),
        title_claims=(
            ArticleClaimAtom(
                text="Отключение электричества в центре", cited_support_ids=("story:curr:frag:902",)
            ),
        ),
        lead="Отключение электричества в центре продолжается; ранее проводились плановые замеры.",
        lead_support_ids=("story:curr:frag:902", "story:hist:frag:901"),
        lead_claims=(
            ArticleClaimAtom(
                text="Отключение электричества в центре продолжается",
                cited_support_ids=("story:curr:frag:902",),
            ),
            ArticleClaimAtom(
                text="Ранее проводились плановые замеры", cited_support_ids=("story:hist:frag:901",)
            ),
        ),
        sections=(
            ArticleSection(
                heading="Отключение электричества",
                heading_support_ids=("story:curr:frag:902",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Отключение электричества", cited_support_ids=("story:curr:frag:902",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="В центре города отключено электроснабжение.",
                        cited_support_ids=("story:curr:frag:902",),
                        claims=(
                            ArticleClaimAtom(
                                text="В центре города отключено электроснабжение",
                                cited_support_ids=("story:curr:frag:902",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )
    res_hist_valid = validate_article_draft(draft_hist_valid, ctx, config)
    assert res_hist_valid.is_valid is True

    # 3. Future scheduled framed as current fails
    draft_future_unframed = StructuredArticleDraft(
        title="Отключение электричества в центре",
        title_support_ids=("story:curr:frag:902",),
        title_claims=(
            ArticleClaimAtom(
                text="Отключение электричества в центре", cited_support_ids=("story:curr:frag:902",)
            ),
        ),
        lead="В центре города аварийное отключение электричества.",
        lead_support_ids=("story:curr:frag:902",),
        lead_claims=(
            ArticleClaimAtom(
                text="В центре города аварийное отключение электричества",
                cited_support_ids=("story:curr:frag:902",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Отключение газа",
                heading_support_ids=("op:gas:sched",),
                heading_claims=(
                    ArticleClaimAtom(text="Отключение газа", cited_support_ids=("op:gas:sched",)),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Газоснабжение отключено в связи с аварией.",
                        cited_support_ids=("op:gas:sched",),
                        claims=(
                            ArticleClaimAtom(
                                text="Газоснабжение отключено", cited_support_ids=("op:gas:sched",)
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )
    res_fut = validate_article_draft(draft_future_unframed, ctx, config)
    assert res_fut.is_valid is False
    assert any(iss.code == "FUTURE_CONTEXT_UNFRAMED" for iss in res_fut.issues)

    # 4. Future scheduled properly framed passes
    draft_future_valid = StructuredArticleDraft(
        title="Отключение электричества в центре",
        title_support_ids=("story:curr:frag:902",),
        title_claims=(
            ArticleClaimAtom(
                text="Отключение электричества в центре", cited_support_ids=("story:curr:frag:902",)
            ),
        ),
        lead="В центре города аварийное отключение электричества.",
        lead_support_ids=("story:curr:frag:902",),
        lead_claims=(
            ArticleClaimAtom(
                text="В центре города аварийное отключение электричества",
                cited_support_ids=("story:curr:frag:902",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Отключение газа",
                heading_support_ids=("op:gas:sched",),
                heading_claims=(
                    ArticleClaimAtom(text="Отключение газа", cited_support_ids=("op:gas:sched",)),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="30 августа с 08:00 до 17:00 запланировано отключение газа.",
                        cited_support_ids=("op:gas:sched",),
                        claims=(
                            ArticleClaimAtom(
                                text="30 августа запланировано отключение газа",
                                cited_support_ids=("op:gas:sched",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )
    res_fut_valid = validate_article_draft(draft_future_valid, ctx, config)
    assert res_fut_valid.is_valid is True


def test_thematic_heading_without_claim_atoms():
    ctx = _make_sample_context()
    sup_id = ctx.support_index[0].support_id
    config = PublicationEditorialConfig(
        article_min_words=10, article_max_words=2000, article_min_sections=1
    )
    section = ArticleSection(
        heading="Свет по цепочке",
        heading_support_ids=(sup_id,),
        heading_claims=(),
        paragraphs=(
            ArticleParagraph(
                text="Авария на подстанции: временно обесточена центральная часть Бердянска.",
                cited_support_ids=(sup_id,),
                claims=(
                    ArticleClaimAtom(
                        text="Авария на подстанции: временно обесточена центральная часть",
                        cited_support_ids=(sup_id,),
                    ),
                ),
            ),
        ),
    )
    draft = StructuredArticleDraft(
        title="Авария на подстанции в Бердянске",
        title_support_ids=(sup_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Авария на подстанции в Бердянске",
                cited_support_ids=(sup_id,),
            ),
        ),
        lead="Временно обесточена центральная часть Бердянска.",
        lead_support_ids=(sup_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Временно обесточена центральная часть Бердянска",
                cited_support_ids=(sup_id,),
            ),
        ),
        sections=(section,),
        word_count=50,
    )
    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is True
    assert len(result.issues) == 0


def test_heading_with_unsupported_concrete_claim_fails():
    ctx = _make_sample_context()
    sup_id = ctx.support_index[0].support_id
    config = PublicationEditorialConfig(
        article_min_words=10, article_max_words=2000, article_min_sections=1
    )
    # Heading claims 500 houses without support
    section = ArticleSection(
        heading="Обесточены 500 домов в центре",
        heading_support_ids=(sup_id,),
        heading_claims=(),
        paragraphs=(
            ArticleParagraph(
                text="Авария на подстанции: временно обесточена центральная часть Бердянска.",
                cited_support_ids=(sup_id,),
                claims=(
                    ArticleClaimAtom(
                        text="Авария на подстанции: временно обесточена центральная часть",
                        cited_support_ids=(sup_id,),
                    ),
                ),
            ),
        ),
    )
    draft = StructuredArticleDraft(
        title="Энергетический сбой в городе",
        title_support_ids=(sup_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Энергетический сбой в городе",
                cited_support_ids=(sup_id,),
            ),
        ),
        lead="Центральная часть осталась без электричества.",
        lead_support_ids=(sup_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Центральная часть осталась без электричества",
                cited_support_ids=(sup_id,),
            ),
        ),
        sections=(section,),
        word_count=50,
    )
    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any("UNSUPPORTED_CONCRETE_CLAIM" in v for v in result.violations)


@pytest.mark.unit
def test_validate_article_draft_with_length_profile_accepts_soft_length() -> None:
    from src.publication.article_length import ArticleLengthProfile

    sup_id = "story:1:evidence:0:frag:101"
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=800, article_max_words=1600)

    # 350 words draft: fails against default config (min 800), but passes with thin length profile (hard min 180)
    section = ArticleSection(
        heading="Авария на подстанции",
        heading_support_ids=(sup_id,),
        heading_claims=(
            ArticleClaimAtom(
                text="Авария на подстанции",
                cited_support_ids=(sup_id,),
            ),
        ),
        paragraphs=(
            ArticleParagraph(
                text="Авария на подстанции: временно обесточена центральная часть Бердянска. Бригада РЭС ведет восстановительные работы.",
                cited_support_ids=(sup_id,),
                claims=(
                    ArticleClaimAtom(
                        text="Авария на подстанции: временно обесточена центральная часть Бердянска. Бригада РЭС ведет восстановительные работы",
                        cited_support_ids=(sup_id,),
                    ),
                ),
            ),
        ),
    )
    draft = StructuredArticleDraft(
        title="Авария на подстанции в Бердянске",
        title_support_ids=(sup_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Авария на подстанции в Бердянске",
                cited_support_ids=(sup_id,),
            ),
        ),
        lead="Бригада РЭС ведет восстановительные работы после аварии на подстанции.",
        lead_support_ids=(sup_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Бригада РЭС ведет восстановительные работы после аварии на подстанции",
                cited_support_ids=(sup_id,),
            ),
        ),
        sections=(section,),
        word_count=350,
    )

    res_default = validate_article_draft(draft, ctx, config)
    assert not res_default.is_valid
    assert any("WORD_COUNT_OUT_OF_BOUNDS" in v for v in res_default.violations)

    profile = ArticleLengthProfile(
        richness="thin",
        target_min_words=300,
        target_max_words=700,
        target_min_sections=2,
        target_max_sections=3,
        hard_min_words=180,
        hard_max_words=1600,
    )
    res_profile = validate_article_draft(draft, ctx, config, length_profile=profile)
    assert res_profile.is_valid
    assert len(res_profile.issues) == 0


@pytest.mark.unit
def test_charging_phone_paraphrase_is_warning_not_blocking() -> None:
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Есть магазины, кафе, где можно зарядить телефон.",
        source_text="Есть магазины, кафе, где можно зарядить телефон.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Возможность подзарядки телефонов",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Возможность подзарядки телефонов",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="В городе можно зарядить телефон в магазинах и кафе.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе можно зарядить телефон в магазинах и кафе",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Пункты зарядки",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Часть магазинов и кафе предоставляет возможность зарядить телефон.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Часть магазинов и кафе предоставляет возможность зарядить телефон",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is True
    assert "CLAIM_LEXICAL_DIVERGENCE:P001" in result.all_violations
    assert "CLAIM_LEXICAL_DIVERGENCE:P001" not in result.violations


@pytest.mark.unit
def test_unsupported_destination_is_blocking_proper_name() -> None:
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Есть рейсы в Ростов и Таганрог.",
        source_text="Есть рейсы в Ростов и Таганрог.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Движение автобусных рейсов",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Движение автобусных рейсов",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="Организовано автобусное сообщение по междугородним направлениям.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Организовано автобусное сообщение по междугородним направлениям",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Расписание рейсов",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Доступны рейсы в Москву и Воронеж.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Доступны рейсы в Москву и Воронеж",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(v.startswith("UNSUPPORTED_PROPER_NAME:P001") for v in result.violations)


@pytest.mark.unit
def test_date_granularity_expansion_without_support_is_blocked() -> None:
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Обещали 26, потом 28, сегодня обещают 31.",
        source_text="Обещали 26, потом 28, сегодня обещают 31.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    # Expanding "31" to "31 августа" when month is absent is blocked
    draft = StructuredArticleDraft(
        title="Сроки подачи воды",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Сроки подачи воды",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="Сроки подачи воды неоднократно переносились.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Сроки подачи воды неоднократно переносились",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Новый срок",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Срок перенесли на 31 августа.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Срок перенесли на 31 августа",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(
        "UNSUPPORTED_CONCRETE_CLAIM" in v or "UNSUPPORTED_CLAIM_ATOM" in v
        for v in result.violations
    )


@pytest.mark.unit
def test_article_validator_blocks_question_context_overclaim() -> None:
    sup_q = ArticleSupport(
        support_id="story:10:evidence:0:frag:901",
        text="Работает ли пенсионный фонд?",
        source_text="Работает ли пенсионный фонд?",
        support_kind="evidence",
        publication_use="CONTEXT",
        source_refs=("ref-901",),
        fragment_ids=(901,),
        source_item_ids=(10,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="resident_question",
        source_roles=("community",),
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Вопросы жителей",),
        support_index=(sup_q,),
        support_by_id={sup_q.support_id: sup_q},
        recurring_topics=(),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )

    # 1. Unframed factual proposition asserting that fund works (from a question support)
    overclaim_draft = StructuredArticleDraft(
        title="Вопросы работы учреждений",
        title_support_ids=(sup_q.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Вопросы работы учреждений",
                cited_support_ids=(sup_q.support_id,),
            ),
        ),
        lead="В городе обсуждают график приема граждан.",
        lead_support_ids=(sup_q.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе обсуждают график приема граждан",
                cited_support_ids=(sup_q.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Пенсионный фонд",
                heading_support_ids=(sup_q.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Пенсионный фонд возобновил прием граждан.",
                        cited_support_ids=(sup_q.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Пенсионный фонд работает",
                                cited_support_ids=(sup_q.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )

    res_overclaim = validate_article_draft(overclaim_draft, ctx, config)
    assert res_overclaim.is_valid is False
    assert any("QUESTION_CONTEXT_OVERCLAIM" in v for v in res_overclaim.violations)

    # 2. Inquiry-framed draft citing the same question support passes QUESTION_CONTEXT_OVERCLAIM
    inquiry_draft = StructuredArticleDraft(
        title="Вопросы жителей",
        title_support_ids=(sup_q.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Вопросы жителей",
                cited_support_ids=(sup_q.support_id,),
            ),
        ),
        lead="Жители уточняют работу социальных служб.",
        lead_support_ids=(sup_q.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Жители уточняют работу социальных служб",
                cited_support_ids=(sup_q.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Пенсионный фонд",
                heading_support_ids=(sup_q.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Жители в чатах спрашивают, работает ли пенсионный фонд.",
                        cited_support_ids=(sup_q.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Жители спрашивают, работает ли пенсионный фонд",
                                cited_support_ids=(sup_q.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )

    res_inquiry = validate_article_draft(inquiry_draft, ctx, config)
    assert not any("QUESTION_CONTEXT_OVERCLAIM" in v for v in res_inquiry.violations)


@pytest.mark.unit
def test_edition_anchor_in_title_does_not_block_when_edition_set() -> None:
    sup = ArticleSupport(
        support_id="story:1:frag:1",
        text="месяц без света сидим",
        source_text="месяц без света сидим",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Света нет",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Жители Бердянска сообщают, что света нет около месяца",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Жители Бердянска сообщают, что света нет около месяца",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="Света нет около месяца.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Света нет около месяца",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Отключения",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Месяц сидим без света.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Месяц сидим без света",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=25,
    )
    res = validate_article_draft(draft, ctx, config)
    blocking_codes = [i.code for i in res.issues if i.blocking]
    assert "UNSUPPORTED_CLAIM_ATOM" not in blocking_codes
    assert "UNSUPPORTED_PROPER_NAME" not in blocking_codes


@pytest.mark.unit
def test_unsupported_location_in_lead_remains_blocking() -> None:
    sup = ArticleSupport(
        support_id="story:1:frag:1",
        text="оборудование Юпитера запитали от генератора, сигнал пошел на роутеры всего дома",
        source_text="оборудование Юпитера запитали от генератора, сигнал пошел на роутеры всего дома",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Юпитер",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Оборудование Юпитера запитали от генератора",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Оборудование Юпитера запитали от генератора",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="В одном из домов на Азмоле оборудование Юпитера запитали от генератора.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В одном из домов на Азмоле оборудование Юпитера запитали от генератора",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Связь",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Оборудование Юпитера запитали от генератора.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Оборудование Юпитера запитали от генератора",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=30,
    )
    res = validate_article_draft(draft, ctx, config)
    assert res.is_valid is False
    assert any(i.code == "UNSUPPORTED_PROPER_NAME" and "Азмол" in i.message for i in res.issues)


@pytest.mark.unit
def test_corrected_direct_quote_fails_with_unsupported_direct_quote() -> None:
    sup = ArticleSupport(
        support_id="story:1:frag:1",
        text="житель отметил, что все починят к обеду",
        source_text='житель отметил: "все починят к обеду"',
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Ремонт",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Восстановительные работы",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Восстановительные работы",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead='Житель отметил: "все починят к вечеру".',
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text='Житель отметил: "все починят к вечеру"',
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Работы",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Ведутся работы на сети.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Ведутся работы на сети",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=20,
    )
    res = validate_article_draft(draft, ctx, config)
    assert res.is_valid is False
    assert any(i.code == "UNSUPPORTED_DIRECT_QUOTE" and i.blocking for i in res.issues)


@pytest.mark.unit
def test_unsupported_critical_term_emits_unsupported_critical_term() -> None:
    sup = ArticleSupport(
        support_id="story:1:frag:1",
        text="Власти молчат, света нет 3 недели и более",
        source_text="Власти молчат, света нет 3 недели и более",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Света нет",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Нет информации о сроках восстановления света и воды",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Нет информации о сроках восстановления света и воды",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        lead="Света нет 3 недели и более.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Света нет 3 недели и более",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Отключения",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Власти молчат о сроках.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Власти молчат о сроках",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=25,
    )
    res = validate_article_draft(draft, ctx, config)
    assert res.is_valid is False
    assert any(i.code == "UNSUPPORTED_CRITICAL_TERM" and i.blocking for i in res.issues)


@pytest.mark.unit
def test_unsupported_location_in_reader_prose_blocks_even_if_omitted_from_atom() -> None:
    sup = ArticleSupport(
        support_id="story:1:frag:1",
        text="оборудование Юпитера запитали от генератора, сигнал пошел на роутеры всего дома",
        source_text="оборудование Юпитера запитали от генератора, сигнал пошел на роутеры всего дома",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Юпитер",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=200,
        article_min_sections=1,
        article_max_sections=4,
    )
    draft = StructuredArticleDraft(
        title="Оборудование Юпитера запитали от генератора",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Оборудование Юпитера запитали от генератора",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        # Reader-facing prose adds "на Азмоле", but the claim atom omits it:
        lead="В одном из домов на Азмоле оборудование Юпитера запитали от генератора.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="Оборудование Юпитера запитали от генератора",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Связь",
                heading_support_ids=(sup.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Оборудование Юпитера запитали от генератора.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Оборудование Юпитера запитали от генератора",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=30,
    )
    res = validate_article_draft(draft, ctx, config)
    assert res.is_valid is False
    assert any(
        i.code == "UNSUPPORTED_PROPER_NAME"
        and i.unit_id == "LEAD"
        and "азмол" in i.message.lower()
        and i.blocking
        for i in res.issues
    )


@pytest.mark.unit
def test_attempt74_full_draft_validation() -> None:
    raw_supports = [
        ("story:1:frag:1", "Расцвел настолько что месяц без света сидим, это ягодки пошли?"),
        (
            "story:1:frag:2",
            "Логично, что если власти молчат, света нет 3 недели и более, и даже временно не дают",
        ),
        (
            "story:1:frag:3",
            "За 29 дней уж точно можно было объяснить всё населению. Сказать хотя бы, к чему готовиться, иль рассчитывать только на свои силы.",
        ),
        (
            "story:1:frag:4",
            "Обещали 26, потом обещали 28, сегодня обещают 31, 31 начнут обещать 4 сентября и так далее",
        ),
        (
            "story:1:frag:5",
            "В нашем доме нашёлся добрый человек, он от своего генератора запитал оборудование Юпитера, а дальше сигнал пошёл на роутеры всего дома... и гоууу вай фай!",
        ),
        ("story:1:frag:6", "Я на горе, света нет"),
        ("story:1:frag:7", "надійшло понад сотню повідомлень"),
        ("story:1:frag:8", "застосувала 626 безпілотників"),
        ("story:1:frag:9", "осенью нам всем понадобится подобные буржуйки"),
        ("story:1:frag:10", "поступило обращение от жителей"),
        ("story:1:frag:11", "ремонтные бригады продолжают работу"),
        ("story:1:frag:12", "света нет в нескольких районах"),
    ]

    supports = tuple(
        ArticleSupport(
            support_id=sid,
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{idx}",),
            fragment_ids=(idx,),
            source_item_ids=(idx,),
            observed_at=_NOW,
            temporal_role="CURRENT_WINDOW",
        )
        for idx, (sid, text) in enumerate(raw_supports, start=1)
    )

    ctx = ArticleEditorialContext(
        headline_candidates=("Проблемы со светом",),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=("utilities", "power"),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )

    config = PublicationEditorialConfig(
        article_min_words=10,
        article_max_words=500,
        article_min_sections=2,
        article_max_sections=4,
    )

    draft = StructuredArticleDraft(
        title="Жители Бердянска сообщают об отсутствии света около месяца",
        title_support_ids=("story:1:frag:1",),
        title_claims=(
            ArticleClaimAtom(
                text="Жители Бердянска сообщают об отсутствии электроснабжения около месяца",
                cited_support_ids=("story:1:frag:1",),
            ),
        ),
        lead="В Бердянске жители пишут, что власти молчат о сроках восстановления электроснабжения, а света нет около месяца.",
        lead_support_ids=("story:1:frag:1", "story:1:frag:2"),
        lead_claims=(
            ArticleClaimAtom(
                text="Жители пишут, что власти молчат",
                cited_support_ids=("story:1:frag:2",),
            ),
            ArticleClaimAtom(
                text="Жители Бердянска сообщают об отсутствии электроснабжения около месяца",
                cited_support_ids=("story:1:frag:1",),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Проблемы с электричеством",
                heading_support_ids=("story:1:frag:4",),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Жители отмечают, что обещанные сроки постоянно сдвигаются: обещали 26, потом 28, затем 31 число.",
                        cited_support_ids=("story:1:frag:4",),
                        claims=(
                            ArticleClaimAtom(
                                text="В сообщениях обещали 26, потом 28, затем 31",
                                cited_support_ids=("story:1:frag:4",),
                            ),
                        ),
                    ),
                ),
            ),
            ArticleSection(
                heading="Локальные решения",
                heading_support_ids=("story:1:frag:5",),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="В одном из домов житель от генератора запитал оборудование Юпитера, восстановив интернет для всего дома.",
                        cited_support_ids=("story:1:frag:5",),
                        claims=(
                            ArticleClaimAtom(
                                text="В одном из домов оборудование Юпитера запитали от генератора и восстановили интернет для дома",
                                cited_support_ids=("story:1:frag:5",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=65,
    )

    result = validate_article_draft(draft, ctx, config)
    blocking_issues = [i for i in result.issues if i.blocking]
    assert result.is_valid is True, f"Expected valid draft, got blocking issues: {blocking_issues}"
    assert blocking_issues == []
    assert all(i.severity == "warning" and not i.blocking for i in result.issues)
