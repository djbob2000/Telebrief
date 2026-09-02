"""Unit and integration tests for ArticleEditorialContext."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.db.uow import DatabaseUnitOfWork
from src.domain.event_payload import OperationalObservationPayload
from src.domain.operational_state import ResolvedObservation
from src.editorial_models import StoryCard
from src.publication.article_context import (
    PublicationWindow,
    build_article_editorial_context,
    classify_support_temporal_role,
)
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.evidence import PublicationEvidence
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_T0 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)


def test_build_article_editorial_context_preserves_evidence_and_timeline():
    cards = [
        StoryCard(
            id="story:1",
            topic="Авария на водоводе",
            importance="high",
            summary="Порыв трубы на АКЗ.",
            category="utilities",
            tags=["жкх", "вода"],
        ),
        StoryCard(
            id="story:2",
            topic="Выставка картин",
            importance="medium",
            summary="В художественном музее открылась выставка.",
            category="culture",
            tags=["культура", "музей"],
        ),
        StoryCard(
            id="story:3",
            topic="Новости дня",  # Generic fallback topic
            importance="low",
            summary="Краткая сводка.",
        ),
    ]

    evi1 = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="Порыв трубы d=500мм на АКЗ",
        source_text="Порыв трубы d=500мм на АКЗ",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="telegram:source:1:item:1:rev:1:frag:101",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_T0,
    )
    evi2 = PublicationEvidence(
        evidence_id="story:2:evidence:0:frag:201",
        story_id=2,
        text="Представлено 40 работ местных художников",
        source_text="Представлено 40 работ местных художников",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=201,
        source_ref="telegram:source:2:item:2:rev:1:frag:201",
        source_id=2,
        source_item_id=2,
        source_role="community",
        observed_at=_T1,
    )
    evi_obs = PublicationEvidence(
        evidence_id="story:1:evidence:1:frag:102",
        story_id=1,
        text="Вода отсутствует с 9 утра",
        source_text="Вода отсутствует с 9 утра",
        kind="community_observation",
        publication_use="PUBLISH",
        fragment_id=102,
        source_ref="telegram:source:1:item:3:rev:1:frag:102",
        source_id=1,
        source_item_id=3,
        source_role="community",
        observed_at=_T0,
    )

    obs_payload = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="АКЗ",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Порыв водовода",
        source_fragment_ids=(101,),
    )
    obs_res = ResolvedObservation(
        observation=obs_payload,
        observed_at=_T0,
        source_refs=("telegram:source:1:item:1:rev:1:frag:101",),
    )

    evi_excl = PublicationEvidence(
        evidence_id="story:1:evidence:2:frag:103",
        story_id=1,
        text="Реклама сантехники",
        source_text="Реклама сантехники",
        kind="commercial_offer",
        publication_use="EXCLUDE",
        fragment_id=103,
        source_ref="telegram:source:1:item:4:rev:1:frag:103",
        source_id=1,
        source_item_id=4,
        source_role="community",
        observed_at=_T0,
    )

    gas_obs_payload = OperationalObservationPayload(
        subject_key="gas_supply",
        subject_label="Газоснабжение",
        dimension="availability",
        location="Центр",
        entity="газопровод",
        state="SCHEDULED",
        detail="30 августа с 08:00 до 17:00 будет временно прекращена подача газа.",
        source_fragment_ids=(2003,),
    )
    gas_obs_res = ResolvedObservation(
        observation=gas_obs_payload,
        observed_at=_T0,
        source_refs=("telegram:source:1:item:5:rev:1:frag:2003",),
        effective_from=dt.datetime(2026, 8, 30, 8, 0, tzinfo=dt.timezone.utc),
        effective_until=dt.datetime(2026, 8, 30, 17, 0, tzinfo=dt.timezone.utc),
    )

    from src.collector import Message
    from src.editorial_models import SourceRecord

    source_records = {
        "telegram:source:1:item:5:rev:1:frag:2003": SourceRecord(
            ref="telegram:source:1:item:5:rev:1:frag:2003",
            message=Message(
                text="30 августа с 08:00 до 17:00 будет временно прекращена подача газа.",
                sender="ГорГаз",
                timestamp=_T0,
            ),
            source_type="official",
        )
    }

    ctx = build_article_editorial_context(
        cards=cards,
        evidence_items=[evi1, evi2, evi_obs, evi_excl],
        operational_observations=[obs_res, gas_obs_res],
        source_records=source_records,
    )

    # 1. Headline candidates exclude generic fallback 'Новости дня'
    assert "Авария на водоводе" in ctx.headline_candidates
    assert "Выставка картин" in ctx.headline_candidates
    assert "Новости дня" not in ctx.headline_candidates
    assert [c.id for c in ctx.story_cards] == ["story:1", "story:2", "story:3"]

    # 2. Unified ArticleSupport tests
    assert "story:1:evidence:0:frag:101" in ctx.support_by_id
    assert all(s.publication_use != "EXCLUDE" for s in ctx.support_index)
    assert "story:1:evidence:2:frag:103" not in ctx.support_by_id
    assert any(s.support_kind == "operational" for s in ctx.support_index)

    op = next(
        s for s in ctx.support_index if s.support_kind == "operational" and "gas" in s.support_id
    )
    assert op.source_text == "30 августа с 08:00 до 17:00 будет временно прекращена подача газа."
    assert op.effective_from == dt.datetime(2026, 8, 30, 8, 0, tzinfo=dt.timezone.utc)
    assert op.effective_until == dt.datetime(2026, 8, 30, 17, 0, tzinfo=dt.timezone.utc)

    # 3. Prompt context generation has [SUPPORT ...] headers
    prompt_str = ctx.to_prompt_context()
    assert "[SUPPORT story:1:evidence:0:frag:101]" in prompt_str
    assert "[SUPPORT op:gas_supply:availability:frag:2003]" in prompt_str
    assert "role=" in prompt_str
    assert "Порыв трубы d=500мм на АКЗ" in prompt_str


def test_build_article_editorial_context_epistemic_propagation():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    evi = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="На Горе света нет",
        source_text="На Горе света нет",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="telegram:source:7:item:9:rev:1:frag:101",
        source_id=7,
        source_item_id=9,
        source_role="community",
        observed_at=now,
    )

    context = build_article_editorial_context(
        cards=(),
        evidence_items=[evi],
        snapshot_at=now,
        lookback_hours=24,
    )

    support = context.support_index[0]
    assert support.evidence_kind == "community_report"
    assert support.source_roles == ("community",)

    prompt = context.to_prompt_context()
    assert "evidence_kind=community_report" in prompt
    assert "source_roles=community" in prompt
    assert "framing=attributed_report" in prompt


def test_classify_support_temporal_role():
    window = PublicationWindow(
        snapshot_at=dt.datetime(2026, 8, 29, 21, 0, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 8, 28, 21, 0, tzinfo=dt.timezone.utc),
    )

    # 1. future scheduled state announced today
    assert (
        classify_support_temporal_role(
            observed_at=dt.datetime(2026, 8, 29, 11, tzinfo=dt.timezone.utc),
            effective_from=dt.datetime(2026, 8, 30, 8, tzinfo=dt.timezone.utc),
            effective_until=dt.datetime(2026, 8, 30, 17, tzinfo=dt.timezone.utc),
            support_kind="operational",
            window=window,
        )
        == "FUTURE_SCHEDULED"
    )

    # 2. ordinary current-window evidence
    assert (
        classify_support_temporal_role(
            observed_at=dt.datetime(2026, 8, 29, 10, tzinfo=dt.timezone.utc),
            effective_from=None,
            effective_until=None,
            support_kind="evidence",
            window=window,
        )
        == "CURRENT_WINDOW"
    )

    # 3. old resolved evidence
    assert (
        classify_support_temporal_role(
            observed_at=dt.datetime(2026, 8, 22, 10, tzinfo=dt.timezone.utc),
            effective_from=None,
            effective_until=dt.datetime(2026, 8, 23, 10, tzinfo=dt.timezone.utc),
            support_kind="operational",
            window=window,
        )
        == "HISTORICAL_CONTEXT"
    )

    # 4. old operational state whose effective interval still covers snapshot
    assert (
        classify_support_temporal_role(
            observed_at=dt.datetime(2026, 8, 22, 10, tzinfo=dt.timezone.utc),
            effective_from=dt.datetime(2026, 8, 22, 10, tzinfo=dt.timezone.utc),
            effective_until=None,
            support_kind="operational",
            window=window,
        )
        == "CURRENT_WINDOW"
    )


@pytest.mark.postgres
async def test_event_adapter_builds_article_editorial_context(conn, pool, edition):
    """EventEditorialAdapter populates analysis.article_context for article runs."""
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-ctx",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )

    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-ctx", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-ctx", prompt_version="v1"
    )

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _T0),
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
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-ctx', %s) RETURNING id",
        (src_id, _T0),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-ctx', 'Текст') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Текст', 'h-fctx', 'v1', TRUE, %s) RETURNING id",
        (sir_id, _T0),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Открытие выставки",
        "category": "culture",
        "headline": "Открытие художественной выставки",
        "digest_summary": "В музее открылась выставка картин.",
        "evidence_items": [
            {
                "text": "Выставка продлится до конца месяца",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [frag_id],
            }
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-ctx-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            story_id,
            event_payload["digest_summary"],
            event_payload["headline"],
            event_payload["digest_summary"],
            json.dumps(event_payload),
            _T0,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-article-ctx",
        snapshot_at=_T0,
        policy_ids=(elig.id, sel.id, wri.id),
    )
    cand = await repo.insert_candidate(
        conn, run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=1
    )
    dec = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_T0,
        ),
    )
    pub_input = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    assert len(editorial.analysis.article_context.general_facts) == 1
    assert (
        "Выставка продлится до конца месяца"
        in editorial.analysis.article_context.general_facts[0].text
    )


def test_build_article_editorial_context_resident_question_framing():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    evi_q = PublicationEvidence(
        evidence_id="story:10:evidence:0:frag:901",
        story_id=10,
        text="Работает ли пенсионный фонд?",
        source_text="Работает ли пенсионный фонд?",
        kind="resident_question",
        publication_use="CONTEXT",
        fragment_id=901,
        source_ref="telegram:source:3:item:8:rev:1:frag:901",
        source_id=3,
        source_item_id=8,
        source_role="community",
        observed_at=now,
    )

    ctx = build_article_editorial_context(
        cards=(),
        evidence_items=[evi_q],
        snapshot_at=now,
        lookback_hours=24,
    )

    assert len(ctx.support_index) == 1
    sup = ctx.support_index[0]
    assert sup.evidence_kind == "resident_question"
    assert sup.publication_use == "CONTEXT"

    prompt_str = ctx.to_prompt_context()
    assert "framing=question_context" in prompt_str
    assert "publication_use=CONTEXT" in prompt_str


@pytest.mark.unit
def test_article_context_carries_generic_edition_anchor() -> None:
    ctx = build_article_editorial_context(
        cards=[],
        evidence_items=[],
        edition_name="Бердянск",
    )
    assert ctx.edition_name == "Бердянск"
    assert "Бердянск" in ctx.edition_anchor_terms
    assert "EDITION CONTEXT: Бердянск" in ctx.to_prompt_context()


def test_article_support_preserves_story_id_for_evidence():
    card = StoryCard(id="story:42", topic="Тема", importance="high", summary="Сводка")
    evi = PublicationEvidence(
        evidence_id="story:42:evidence:0:frag:101",
        story_id=42,
        text="Факт",
        source_text="Факт",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="telegram:source:1:item:1:rev:1:frag:101",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_T0,
    )
    ctx = build_article_editorial_context(
        cards=[card],
        evidence_items=[evi],
    )
    support = ctx.support_index[0]
    assert support.story_id == "story:42"


def test_article_support_preserves_story_id_for_operational():
    card = StoryCard(
        id="story:77",
        topic="Газ",
        importance="high",
        summary="Сводка",
        representative_source_refs=["telegram:source:1:item:5:rev:1:frag:700"],
    )
    gas_obs = OperationalObservationPayload(
        subject_key="gas_supply",
        subject_label="Газ",
        dimension="availability",
        location="Центр",
        entity="газопровод",
        state="SCHEDULED",
        detail="Отключение газа",
        source_fragment_ids=(700,),
    )
    obs_res = ResolvedObservation(
        observation=gas_obs,
        observed_at=_T0,
        source_refs=("telegram:source:1:item:5:rev:1:frag:700",),
    )
    ctx = build_article_editorial_context(
        cards=[card],
        evidence_items=[],
        operational_observations=[obs_res],
    )
    op_support = next(s for s in ctx.support_index if s.support_kind == "operational")
    assert op_support.story_id == "story:77"


def test_render_operational_state_fact():
    """Test 7A & 7B: Exhaustive mapping for all 6 states, fail-closed on unknown state."""
    from src.publication.article_context import render_operational_state_fact

    states_expected = {
        "UNAVAILABLE": "отсутствует",
        "AVAILABLE": "работает",
        "DEGRADED": "работает с ограничениями",
        "RESTRICTED": "ограничено",
        "SCHEDULED": "запланировано: ремонт водовода",
        "UNKNOWN": "ситуация уточняется",
    }

    for state, exp_substr in states_expected.items():
        obs = OperationalObservationPayload(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            location="АКЗ",
            entity="водовод",
            state=state,
            detail="ремонт водовода"
            if state == "SCHEDULED"
            else ("ситуация уточняется" if state == "UNKNOWN" else ""),
            source_fragment_ids=(1,),
        )
        rendered = render_operational_state_fact(obs)
        assert exp_substr in rendered
        # Must not leak technical enum
        assert state not in rendered
        # Must not contain brackets
        assert "[" not in rendered and "]" not in rendered

    # Test 7B: Unknown state fails closed
    class BadObs:
        state = "CORRUPTED"
        subject_label = "Тест"
        location = ""
        entity = ""
        detail = ""

    with pytest.raises(ValueError, match="Unknown operational state"):
        render_operational_state_fact(BadObs())


def test_article_support_text_has_no_technical_leaks():
    """Test 7C: ArticleSupport.text for operational observations contains 0 brackets and 0 raw enums."""
    card = StoryCard(
        id="story:1",
        topic="Электроснабжение",
        importance="high",
        summary="Сводка по свету",
        representative_source_refs=["telegram:source:1:item:1:rev:1:frag:1"],
    )
    obs = OperationalObservationPayload(
        subject_key="power_outage",
        subject_label="Электроснабжение",
        dimension="availability",
        location="Бердянск",
        entity="подстанция",
        state="UNAVAILABLE",
        detail="вообще нигде нет электроснабжения",
        source_fragment_ids=(1,),
    )
    obs_res = ResolvedObservation(
        observation=obs,
        observed_at=_T0,
        source_refs=("telegram:source:1:item:1:rev:1:frag:1",),
    )
    ctx = build_article_editorial_context(
        cards=[card],
        evidence_items=[],
        operational_observations=[obs_res],
    )
    op_sup = next(s for s in ctx.support_index if s.support_kind == "operational")

    assert "[" not in op_sup.text
    assert "]" not in op_sup.text
    assert "UNAVAILABLE" not in op_sup.text
    assert "AVAILABLE" not in op_sup.text
    assert "отсутствует" in op_sup.text
