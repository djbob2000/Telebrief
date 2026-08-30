"""Tests for deterministic text fragmentation and noise filtering."""

from __future__ import annotations

import pytest

from src.domain.event_pipeline import NewSourceFragment
from src.processing.fragments import (
    FRAGMENTER_VERSION,
    hash_normalized_text,
    is_noise_or_classified,
    normalize_fragment_text,
    split_into_fragments,
)
from src.repositories.fragments import FragmentRepository


@pytest.mark.unit
def test_normalize_fragment_text_strips_urls_and_normalizes():
    raw = "  Срочно! Смотрите https://t.me/channel/123 и http://example.com #Бердянск  новости.  "
    norm = normalize_fragment_text(raw)
    assert "http" not in norm
    assert "t.me" not in norm
    assert norm == "срочно! смотрите и бердянск новости."


@pytest.mark.unit
def test_hash_normalized_text_deterministic():
    text1 = "срочно бердянск"
    text2 = "срочно бердянск"
    assert hash_normalized_text(text1) == hash_normalized_text(text2)
    assert len(hash_normalized_text(text1)) == 64


@pytest.mark.unit
def test_is_noise_or_classified_detects_ads():
    ad = "Продам диван б/у, цена 5000 руб, самовывоз из центра, звонить +79901234567"
    is_noise, reason = is_noise_or_classified(ad)
    assert is_noise is True
    assert reason == "commercial_classified"


@pytest.mark.unit
def test_is_noise_or_classified_detects_short_chatter():
    chatter = "Доброе утро!"
    is_noise, reason = is_noise_or_classified(chatter)
    assert is_noise is True
    assert reason == "obvious_noise"

    too_short = "Ок."
    is_noise, reason = is_noise_or_classified(too_short)
    assert is_noise is True
    assert reason in {"obvious_noise", "too_short"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Нет воды",
        "На Горе света нет",
        "Когда свет?",
        "Автобус 4 не ходит",
        "ПВО слышно",
    ],
)
def test_short_civic_reports_remain_fragment_candidates(text: str):
    fragments = split_into_fragments(text)

    assert len(fragments) == 1
    assert fragments[0].is_candidate is True
    assert fragments[0].drop_reason is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Ок",
        "Окей",
        "Да",
        "Нет",
        "Ага",
        "Угу",
        "Понял",
        "Понятно",
        "Спасибо",
        "Привет",
        "Доброе утро!",
    ],
)
def test_short_reaction_chatter_is_not_candidate(text: str):
    fragments = split_into_fragments(text)

    assert len(fragments) == 1
    assert fragments[0].is_candidate is False
    assert fragments[0].drop_reason in {"obvious_noise", "too_short"}


@pytest.mark.unit
def test_split_into_fragments_splits_paragraphs_and_filters():
    text = (
        "На АКЗ авария на водопроводе, отключили воду до вечера.\n\n"
        "Продам холодильник 10000 руб звонить +79901112233\n\n"
        "На Мелитопольском шоссе ведутся аварийно-восстановительные работы водоканала."
    )
    fragments = split_into_fragments(text, max_chars=500)
    assert len(fragments) == 3

    f0 = fragments[0]
    assert f0.ordinal == 0
    assert "авария на водопроводе" in f0.text_content
    assert f0.is_candidate is True
    assert f0.drop_reason is None

    f1 = fragments[1]
    assert f1.ordinal == 1
    assert "Продам" in f1.text_content
    assert f1.is_candidate is False
    assert f1.drop_reason == "commercial_classified"

    f2 = fragments[2]
    assert f2.ordinal == 2
    assert "Мелитопольском" in f2.text_content
    assert f2.is_candidate is True


@pytest.mark.postgres
async def test_fragment_repository_persistence_and_queries(conn, revision):
    rev_id = revision.id

    repo = FragmentRepository()
    new_frags = [
        NewSourceFragment(
            ordinal=0,
            text_content="Water outage on AKZ district",
            normalized_hash="hash0",
            fragmenter_version=FRAGMENTER_VERSION,
            is_candidate=True,
        ),
        NewSourceFragment(
            ordinal=1,
            text_content="Selling couch 5000 rub",
            normalized_hash="hash1",
            fragmenter_version=FRAGMENTER_VERSION,
            is_candidate=False,
            drop_reason="commercial_classified",
        ),
    ]

    persisted = await repo.create_fragments(conn, rev_id, new_frags)
    assert len(persisted) == 2
    assert persisted[0].source_item_revision_id == rev_id
    assert persisted[0].is_candidate is True
    assert persisted[1].is_candidate is False

    # Query candidates
    candidates = await repo.list_candidates_for_revisions(conn, [rev_id])
    assert len(candidates) == 1
    assert candidates[0].id == persisted[0].id

    # Query by ID
    single = await repo.get_by_id(conn, persisted[0].id)
    assert single is not None
    assert single.text_content == "Water outage on AKZ district"
