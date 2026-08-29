"""Tests for CitySituationRollup and digest vs article separation."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.db.uow import DatabaseUnitOfWork
from src.editorial_models import EditorialAnalysis, StoryCard
from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
    render_city_situation_section,
)
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def test_city_situation_rollup_serialization():
    t0 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2026, 8, 29, 14, 0, tzinfo=dt.timezone.utc)

    item = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="AVAILABLE",
        detail="Вода подается",
        source_refs=("ref-1", "ref-2"),
        first_observed_at=t0,
        last_observed_at=t1,
        observation_count=2,
    )
    rollup = CitySituationRollup(items=(item,))

    d = rollup.to_dict()
    assert len(d["items"]) == 1
    assert d["items"][0]["subject_key"] == "water_supply"

    restored = CitySituationRollup.from_dict(d)
    assert len(restored.items) == 1
    assert restored.items[0].subject_key == "water_supply"
    assert restored.items[0].state == "AVAILABLE"
    assert restored.items[0].source_refs == ("ref-1", "ref-2")


def test_render_city_situation_section():
    t0 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
    item_avail = CitySituationItem(
        subject_key="banking_cash",
        subject_label="Банкоматы",
        dimension="availability",
        location="АКЗ",
        entity="банкомат",
        state="AVAILABLE",
        detail="Выдает наличные",
        source_refs=("ref-1",),
        first_observed_at=t0,
        last_observed_at=t0,
        observation_count=1,
    )
    item_unavail = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Порыв трубы",
        source_refs=("ref-2",),
        first_observed_at=t0,
        last_observed_at=t0,
        observation_count=1,
    )
    rollup = CitySituationRollup(items=(item_avail, item_unavail))

    rendered = render_city_situation_section(rollup)
    assert "<b>Городская обстановка:</b>" in rendered
    assert "🟢 <b>Банкоматы (АКЗ)</b>: Выдает наличные" in rendered
    assert "🔴 <b>Водоснабжение (Колония)</b>: Порыв трубы" in rendered


def test_editorial_analysis_city_situation_roundtrip():
    t0 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
    item = CitySituationItem(
        subject_key="power_supply",
        subject_label="Электросеть",
        dimension="availability",
        location="Центр",
        entity="сеть",
        state="AVAILABLE",
        detail="Свет есть",
        source_refs=("ref-p",),
        first_observed_at=t0,
        last_observed_at=t0,
        observation_count=1,
    )
    rollup = CitySituationRollup(items=(item,))
    card = StoryCard(
        id="story:1",
        topic="Электричество",
        importance="medium",
        summary="Свет есть в Центре",
    )

    analysis = EditorialAnalysis(cards=[card], city_situation=rollup)
    d = analysis.to_dict()
    assert "city_situation" in d

    restored = EditorialAnalysis.from_dict(d)
    assert restored.city_situation is not None
    assert len(restored.city_situation.items) == 1
    assert restored.city_situation.items[0].subject_key == "power_supply"


@pytest.mark.postgres
async def test_digest_vs_article_city_situation_separation(conn, pool, edition):
    """Verify that digest publication runs include CitySituationRollup while article runs do not."""
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-sep", prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-sep", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-sep", prompt_version="v1"
    )

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10099', 'https://t.me/e', 'E') RETURNING id"
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )
    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-sep', %s) RETURNING id",
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-sep', 'Текст') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Текст', 'h-fsep', 'v1', TRUE, %s) RETURNING id",
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Свет в Центре",
        "headline": "Свет в Центре",
        "digest_summary": "Свет работает стабильно.",
        "operational_observations": [
            {
                "subject_key": "power_supply",
                "subject_label": "Электросеть",
                "dimension": "availability",
                "location": "Центр",
                "entity": "сеть",
                "state": "AVAILABLE",
                "detail": "Свет включен",
                "source_fragment_ids": [frag_id],
            }
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-sep-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            story_id,
            event_payload["digest_summary"],
            event_payload["headline"],
            event_payload["digest_summary"],
            json.dumps(event_payload),
            _NOW,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    # 1. Digest publication run
    digest_run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-digest-rollup",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )
    digest_cand = await repo.insert_candidate(
        conn, digest_run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=1
    )
    digest_dec = await repo.insert_selection_decision(
        conn,
        digest_run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=digest_run.id,
            candidate_id=digest_cand.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    digest_input = await repo.freeze_selected_input(
        conn,
        digest_run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=digest_dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    digest_editorial = await adapter.adapt_inputs_on(conn, digest_run.id, inputs=[digest_input])
    assert digest_editorial.analysis.city_situation is not None
    assert len(digest_editorial.analysis.city_situation.items) == 1
    assert digest_editorial.analysis.city_situation.items[0].subject_key == "power_supply"

    # 2. Article publication run
    article_run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-article-rollup",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )
    article_cand = await repo.insert_candidate(
        conn, article_run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=1
    )
    article_dec = await repo.insert_selection_decision(
        conn,
        article_run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=article_run.id,
            candidate_id=article_cand.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    article_input = await repo.freeze_selected_input(
        conn,
        article_run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=article_dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )

    article_editorial = await adapter.adapt_inputs_on(conn, article_run.id, inputs=[article_input])
    # For articles, city_situation must be None!
    assert article_editorial.analysis.city_situation is None
