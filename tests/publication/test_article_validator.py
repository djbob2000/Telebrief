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
                        cited_support_ids=(
                            "story:1:evidence:0:frag:101",
                            "story:2:evidence:0:frag:201",
                        ),
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
    res_mismatch = validate_article_draft(draft_mismatch, ctx, config)
    assert res_mismatch.is_valid is False
    assert any(iss.code == "CLAIM_SUPPORT_MISMATCH" for iss in res_mismatch.issues)


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
    config = PublicationEditorialConfig(article_min_words=10, article_max_words=2000)
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
    assert not result.is_valid
    assert any("MISSING_CLAIM_ATOMS:H001" in v for v in result.violations)
