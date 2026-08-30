"""Tests for article length profile derivation and adaptive targets."""

from __future__ import annotations

import datetime as dt

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_length import (
    ArticleLengthProfile,
    derive_article_length_profile,
)

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


def _make_support(support_id: str, use: str = "PUBLISH") -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text="Фактическое сообщение",
        source_text="Фактическое сообщение",
        support_kind="evidence",
        publication_use=use,  # type: ignore[arg-type]
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
    )


def test_publication_editorial_config_defaults():
    config = PublicationEditorialConfig()
    assert config.article_max_words == 2200
    assert config.article_max_sections == 8


def test_derive_article_length_profile_thin():
    config = PublicationEditorialConfig(article_max_words=1600)
    supports = tuple(_make_support(f"story:{i}:evidence:0:frag:{i}") for i in range(1, 4))
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )

    profile = derive_article_length_profile(context, config)
    assert isinstance(profile, ArticleLengthProfile)
    assert profile.richness == "thin"
    assert profile.target_min_words == 350
    assert profile.target_max_words == 800
    assert profile.target_min_sections == 2
    assert profile.target_max_sections == 4
    assert profile.hard_min_words == 180
    assert profile.hard_max_words == 1600


def test_derive_article_length_profile_standard():
    config = PublicationEditorialConfig(article_max_words=1600)
    supports = tuple(
        _make_support(f"story:{i}:evidence:{j}:frag:{i * 10 + j}")
        for i in range(1, 7)
        for j in range(2)
    )  # 6 stories, 12 supports
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )

    profile = derive_article_length_profile(context, config)
    assert profile.richness == "standard"
    assert profile.target_min_words == 700
    assert profile.target_max_words == 1400
    assert profile.target_min_sections == 3
    assert profile.target_max_sections == 6
    assert profile.hard_min_words == 180


def test_derive_article_length_profile_rich():
    config = PublicationEditorialConfig(article_max_words=2200, article_max_sections=8)
    supports = tuple(
        _make_support(f"story:{i}:evidence:{j}:frag:{i * 10 + j}")
        for i in range(1, 13)
        for j in range(3)
    )  # 12 stories, 36 supports
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )

    profile = derive_article_length_profile(context, config)
    assert profile.richness == "rich"
    assert profile.target_min_words == 1200
    assert profile.target_max_words == 2000
    assert profile.target_min_sections == 4
    assert profile.target_max_sections == 8
    assert profile.hard_min_words == 180
