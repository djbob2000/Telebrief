"""Integration tests for Event-First single-call narrative digest publication and deterministic fallback."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import psycopg
import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import (
    ChannelConfig,
    Config,
    PublicationEditorialConfig,
    Settings,
)
from src.db.uow import DatabaseUnitOfWork
from src.publication.generation import PublicationGenerationService
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


def _make_config(
    *,
    digest_narrative_mode: str = "single_call",
    article_min_words: int = 800,
) -> Config:
    return Config(
        telegram_api_id=12345,
        telegram_api_hash="test_hash",
        telegram_bot_token="test_token",
        openai_api_key="test_key",
        log_level="INFO",
        channels=[ChannelConfig(id="@test_chan", name="Test Official Channel")],
        settings=Settings(
            schedule_time="20:00",
            timezone="Europe/Kyiv",
            lookback_hours=24,
            openai_model="test-model",
            openai_temperature=0.0,
            output_language="Russian",
            publication_editorial=PublicationEditorialConfig(
                digest_narrative_mode=digest_narrative_mode,
                digest_narrative_max_cards_per_block=6,
                article_min_words=article_min_words,
            ),
        ),
    )


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition_id,
        config_hash="elig-hash-digest",
        prompt_version="elig-v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-digest", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-digest", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


@pytest.mark.postgres
@pytest.mark.integration
async def test_digest_narrative_single_call_success(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    config = _make_config(digest_narrative_mode="single_call")
    policy_ids = await _seed_policies(conn, edition.id)

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
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10091', 'https://t.me/adm', 'Администрация', 'official')
        RETURNING id
        """
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )
    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'msg', 'm-dig-1', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-dig-1', 'Водоканал завершил ремонт на сетях в центре города.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Водоканал завершил ремонт на сетях в центре города.', 'hf-dig-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Водоснабжение",
        "headline": "Ремонт на сетях",
        "digest_summary": "Водоканал завершил ремонт на сетях в центре города.",
        "evidence_items": [
            {
                "text": "Водоканал завершил ремонт на сетях в центре города.",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-dig-1', %s, %s, %s, %s)
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

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-digest-single-call-run",
        snapshot_at=_NOW,
        policy_ids=policy_ids,
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
            confidence=0.98,
            reason="Good",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    evidence_id = f"story:{story_id}:evidence:0:frag:{frag_id}"

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:other:0",
                    "items": [
                        {
                            "headline": "Ремонт на сетях водоснабжения",
                            "body": "Водоканал завершил ремонт на сетях в центре города.",
                            "cited_support_ids": [evidence_id],
                            "covered_story_ids": [f"story:{story_id}"],
                        }
                    ],
                }
            ]
        }
    )

    import logging

    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    generator.provider = mock_provider

    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    pub = await service.generate(run.id, defer_delivery=False)
    assert pub.publication_run_id == run.id
    assert mock_provider.chat_completion.call_count == 1
    assert "Водоканал завершил ремонт на сетях в центре города." in pub.body


@pytest.mark.postgres
@pytest.mark.integration
async def test_digest_narrative_single_call_validation_failure_falls_back_to_deterministic(
    conn, pool, edition
):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    config = _make_config(digest_narrative_mode="single_call")
    policy_ids = await _seed_policies(conn, edition.id)

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
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10092', 'https://t.me/adm', 'Администрация', 'official')
        RETURNING id
        """
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )
    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'msg', 'm-dig-2', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-dig-2', 'Водоканал ремонтирует сети в центре.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Водоканал ремонтирует сети в центре.', 'hf-dig-2', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Водоснабжение",
        "headline": "Ремонт на сетях",
        "digest_summary": "Водоканал ремонтирует сети в центре.",
        "evidence_items": [
            {
                "text": "Водоканал ремонтирует сети в центре.",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-dig-2', %s, %s, %s, %s)
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

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-digest-fallback-run",
        snapshot_at=_NOW,
        policy_ids=policy_ids,
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
            confidence=0.98,
            reason="Good",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    evidence_id = f"story:{story_id}:evidence:0:frag:{frag_id}"

    # Provider returns unsupported number 950
    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "blocks": [
                {
                    "block_id": "block:other:0",
                    "items": [
                        {
                            "headline": "Отключение 950 домов в центре",
                            "body": "Водоканал отключил 950 домов в центре города.",
                            "cited_support_ids": [evidence_id],
                            "covered_story_ids": [f"story:{story_id}"],
                        }
                    ],
                }
            ]
        }
    )

    import logging

    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    generator.provider = mock_provider

    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    pub = await service.generate(run.id, defer_delivery=False)
    assert pub.publication_run_id == run.id
    assert mock_provider.chat_completion.call_count == 1
    # Fallback to deterministic bullets
    assert "• **Ремонт на сетях**" in pub.body


class _RecordingObserver:
    def __init__(self):
        self.started: list[dict] = []
        self.finished: list[dict] = []

    async def attempt_started(self, stage: str, **kwargs) -> int:
        self.started.append({"stage": stage, **kwargs})
        return len(self.started)

    async def attempt_finished(self, attempt_id: int, status: str, **kwargs) -> None:
        self.finished.append({"attempt_id": attempt_id, "status": status, **kwargs})


@pytest.mark.asyncio
async def test_event_first_article_city_life_broad_coverage():
    from src.editorial_models import StoryCard
    from src.publication.article_context import ArticleEditorialContext, ArticleSupport

    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    config = _make_config(article_min_words=100)

    card_power = StoryCard(
        id="story:power", topic="Электроснабжение", importance="high", summary="Свет"
    )

    card_safety = StoryCard(
        id="story:safety", topic="Безопасность над морем", importance="high", summary="Вспышки"
    )
    card_telecom = StoryCard(
        id="story:telecom", topic="Связь при отключениях", importance="medium", summary="Связь"
    )
    card_sport = StoryCard(
        id="story:sport", topic="Набор в спортшколу", importance="low", summary="Спорт"
    )

    s_p1 = ArticleSupport(
        support_id="story:power:1",
        text="Света нет около месяца",
        source_text="Света нет около месяца в некоторых районах",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:power",
    )
    s_p2 = ArticleSupport(
        support_id="story:power:2",
        text="Жильцы дома скидываются по 300 рублей на генератор",
        source_text="Жильцы дома скидываются по 300 рублей на генератор для воды",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:power",
    )
    s_sf = ArticleSupport(
        support_id="story:safety:1",
        text="Видели две вспышки над морем",
        source_text="Жители видели две вспышки над морем ночью",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:safety",
    )
    s_tel = ArticleSupport(
        support_id="story:telecom:1",
        text="Оборудование провайдера работает от генератора",
        source_text="Оборудование провайдера работает от генератора жителя",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-4",),
        fragment_ids=(4,),
        source_item_ids=(4,),
        observed_at=now,
        evidence_kind="service_access",
        story_id="story:telecom",
    )
    s_sp = ArticleSupport(
        support_id="story:sport:1",
        text="Спортивная школа объявила бесплатный набор детей на футбол",
        source_text="Спортивная школа объявила бесплатный набор детей на футбол",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-5",),
        fragment_ids=(5,),
        source_item_ids=(5,),
        observed_at=now,
        evidence_kind="service_access",
        story_id="story:sport",
    )

    all_supports = (s_p1, s_p2, s_sf, s_tel, s_sp)
    ctx = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Безопасность", "Связь", "Спорт"),
        support_index=all_supports,
        support_by_id={s.support_id: s for s in all_supports},
        evidence_index=all_supports,
        recurring_topics=("utilities", "sport"),
        story_cards=(card_power, card_safety, card_telecom, card_sport),
    )

    valid_draft_json = {
        "title": "Обстановка в городе: электроснабжение, события на побережье и связь",
        "title_support_ids": ["story:power:1"],
        "title_claims": [
            {"text": "Света нет около месяца", "cited_support_ids": ["story:power:1"]}
        ],
        "lead": "По сообщениям жителей, в городе около месяца нет света, жильцы скидываются на генератор, а в ночное время очевидцы видели две вспышки над морем.",
        "lead_support_ids": ["story:power:1", "story:power:2", "story:safety:1"],
        "lead_claims": [
            {"text": "Света нет около месяца", "cited_support_ids": ["story:power:1"]},
            {
                "text": "Жильцы дома скидываются по 300 рублей на генератор",
                "cited_support_ids": ["story:power:2"],
            },
            {"text": "Видели две вспышки над морем", "cited_support_ids": ["story:safety:1"]},
        ],
        "sections": [
            {
                "heading": "Электроснабжение и решения жителей",
                "heading_support_ids": ["story:power:1"],
                "heading_claims": [],
                "paragraphs": [
                    {
                        "text": "По сообщениям жителей, света нет около месяца во многих домах города. Жители рассказывают о продолжающихся сложностях и делятся способами организации быта. Жильцы дома скидываются по 300 рублей на генератор, чтобы организовать подачу воды в помещения. Такое совместное решение жильцов дома позволяет регулярно обеспечивать водой квартиры в период длительных отключений электроэнергии.",
                        "cited_support_ids": ["story:power:1", "story:power:2"],
                        "claims": [
                            {
                                "text": "Света нет около месяца",
                                "cited_support_ids": ["story:power:1"],
                            },
                            {
                                "text": "Жильцы дома скидываются по 300 рублей на генератор",
                                "cited_support_ids": ["story:power:2"],
                            },
                        ],
                    }
                ],
            },
            {
                "heading": "События в акватории",
                "heading_support_ids": ["story:safety:1"],
                "heading_claims": [],
                "paragraphs": [
                    {
                        "text": "Жители сообщают о ночных наблюдениях в прибрежной части города. Очевидцы рассказывают, что видели две вспышки над морем в ночное время. Жители обсуждают увиденные две вспышки над морем и делятся информацией в городских сообществах, описывая события ночи.",
                        "cited_support_ids": ["story:safety:1"],
                        "claims": [
                            {
                                "text": "Видели две вспышки над морем",
                                "cited_support_ids": ["story:safety:1"],
                            },
                        ],
                    }
                ],
            },
            {
                "heading": "Связь и городская жизнь",
                "heading_support_ids": ["story:telecom:1", "story:sport:1"],
                "heading_claims": [],
                "paragraphs": [
                    {
                        "text": "В сфере коммуникаций жители находят способы оставаться на связи. Оборудование провайдера работает от генератора для поддержания доступа к сервисам. Кроме того, спортивная школа объявила бесплатный набор детей на футбол. Бесплатный набор детей на футбол открывает возможность для регулярных занятий спортом.",
                        "cited_support_ids": ["story:telecom:1", "story:sport:1"],
                        "claims": [
                            {
                                "text": "Оборудование провайдера работает от генератора",
                                "cited_support_ids": ["story:telecom:1"],
                            },
                            {
                                "text": "Спортивная школа объявила бесплатный набор детей на футбол",
                                "cited_support_ids": ["story:sport:1"],
                            },
                        ],
                    }
                ],
            },
        ],
    }

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(valid_draft_json)

    import logging

    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    generator.provider = mock_provider
    observer = _RecordingObserver()

    title, lead, body = await generator.generate_from_event_article_context(
        ctx, attempt_observer=observer
    )

    assert mock_provider.chat_completion.call_count == 1
    call_kwargs = mock_provider.chat_completion.call_args.kwargs
    user_prompt = next(m["content"] for m in call_kwargs["messages"] if m["role"] == "user")
    assert "ARTICLE COVERAGE PLAN" in user_prompt
    assert "DETAIL SUPPORTS:" in user_prompt

    assert len(observer.finished) == 1
    meta = observer.finished[0]["metadata"]
    assert meta["status"] == "writer_success"
    cov = meta["coverage"]
    assert cov["planned_story_count"] == 4
    assert cov["covered_story_count"] == 4
    assert cov["develop_story_coverage"] == 1.0
    assert cov["weave_story_coverage"] == 1.0
    assert cov["brief_story_coverage"] == 1.0
    assert cov["detail_support_coverage"] == 1.0
    assert cov["leaked_contact_payloads"] == []
