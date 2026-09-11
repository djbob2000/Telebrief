"""Permanent acceptance and unit recovery matrix tests for Event-First article pipeline.

Tests the 8 canonical recovery, regeneration, copy-editing, and fail-closed scenarios:
1. 1/17 stories + 79 words -> Full AI regeneration requested, ArticleEditor NOT called first.
2. Regeneration returns complete valid article -> Accepted, deterministic fallback not used.
3. Regeneration returns complete article with local unit issue (HISTORICAL_CONTEXT_UNFRAMED:LEAD) -> ArticleEditor repairs LEAD -> Accepted.
4. Both writer attempts catastrophically incomplete + deterministic fallback false -> ArticlePublicationRejected.
5. Valid AI draft covers 16/17, missing one BRIEF -> Deterministic supplement allowed -> Final coverage == 1.0.
6. Valid AI draft covers 1/17 -> Supplement forbidden -> Rejection path.
7. Any missing DEVELOP story -> Supplement forbidden.
8. Final article still enforces Evidence Boundary -> Unsupported claim remains blocking.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
)
from src.publication.article_finalization import ArticleFinalizer
from src.publication.article_models import (
    StructuredArticleDraft,
)
from src.publication.errors import ArticlePublicationRejected
from tests.publication.test_article_recovery import RecordingAttemptObserver

_NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)


def _make_17_story_setup(
    *,
    lead_temporal_role: str = "CURRENT_WINDOW",
) -> tuple[ArticleEditorialContext, ArticleCoveragePlan]:
    """Create realistic 17-story edition context and coverage plan matching run 48."""
    supports: list[ArticleSupport] = []
    stories: list[ArticleStoryCoverage] = []

    # 3 DEVELOP stories, 7 WEAVE stories, 7 BRIEF stories
    for i in range(1, 18):
        story_id = f"story:{i}"
        sup_id = f"story:{i}:evidence:0:frag:{100 + i}"
        role = lead_temporal_role if i == 1 else "CURRENT_WINDOW"
        if i <= 3:
            prominence = "DEVELOP"
            text = f"В микрорайоне {i} Бердянска специалисты завершили восстановление электросетей и подстанции."
        elif i <= 10:
            prominence = "WEAVE"
            text = f"Городские службы Бердянска проводят плановый ремонт дорожного покрытия на участке {i}."
        else:
            prominence = "BRIEF"
            text = (
                f"В спортивной школе Бердянска номер {i} открылся набор детей в бесплатные секции."
            )

        sup = ArticleSupport(
            support_id=sup_id,
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"channel:{i}",),
            fragment_ids=(100 + i,),
            source_item_ids=(100 + i,),
            observed_at=_NOW,
            temporal_role=role,
            evidence_kind="established_fact",
            story_id=story_id,
        )
        supports.append(sup)

        stories.append(
            ArticleStoryCoverage(
                story_id=story_id,
                topic=f"Городская тема {i}",
                rank=i,
                prominence=prominence,
                support_ids=(sup_id,),
                detail_support_ids=(sup_id,),
            )
        )

    context = ArticleEditorialContext(
        headline_candidates=("Обзор городской жизни Бердянска",),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("бердянск", "бердянске", "бердянска"),
    )
    plan = ArticleCoveragePlan(stories=tuple(stories))
    return context, plan


def _build_incomplete_79_word_response(supports: list[ArticleSupport]) -> str:
    """Simulate run 48 catastrophic failure: ~79 words covering only 1/17 stories."""
    sup1 = supports[0]
    return json.dumps(
        {
            "title": "Восстановление сетей в микрорайоне 1 Бердянска",
            "title_support_ids": [sup1.support_id],
            "title_claims": [
                {
                    "text": "Восстановление сетей в микрорайоне 1 Бердянска",
                    "cited_support_ids": [sup1.support_id],
                }
            ],
            "lead": sup1.text,
            "lead_support_ids": [sup1.support_id],
            "lead_claims": [{"text": sup1.text, "cited_support_ids": [sup1.support_id]}],
            "sections": [
                {
                    "heading": "Энергетическая ситуация",
                    "heading_support_ids": [sup1.support_id],
                    "paragraphs": [
                        {
                            "text": (
                                "В микрорайоне 1 Бердянска специалисты завершили восстановление электросетей "
                                "и подстанции после повреждений. Ремонтные бригады отчитались о завершении "
                                "основного этапа наладки распределительных узлов. Горожане подтверждают "
                                "появление стабильного напряжения в жилых домах и на социальных объектах микрорайона."
                            ),
                            "cited_support_ids": [sup1.support_id],
                        }
                    ],
                }
            ],
        }
    )


def _build_complete_longread_response(
    supports: list[ArticleSupport],
    *,
    lead_text: str | None = None,
    lead_sups: list[str] | None = None,
    title_text: str | None = None,
    title_sups: list[str] | None = None,
) -> str:
    """Build a complete, compliant event-first longread covering all 17 stories across 3 sections."""
    sup_map = {s.story_id: s for s in supports}
    t_text = title_text or "Восстановление сетей и ремонтные работы в Бердянске"
    t_sups = title_sups or [supports[0].support_id]
    l_text = lead_text or supports[0].text
    l_sups = lead_sups or [supports[0].support_id]

    # Section 1: Stories 1..5
    sec1_paras = []
    sec1_sups = []
    for i in range(1, 6):
        sid = f"story:{i}"
        s = sup_map[sid]
        sec1_sups.append(s.support_id)
        sec1_paras.append(
            {
                "text": f"{s.text} Бригады коммунальщиков продолжают дежурство на данном участке.",
                "cited_support_ids": [s.support_id],
            }
        )

    # Section 2: Stories 6..11
    sec2_paras = []
    sec2_sups = []
    for i in range(6, 12):
        sid = f"story:{i}"
        s = sup_map[sid]
        sec2_sups.append(s.support_id)
        sec2_paras.append(
            {
                "text": f"{s.text} Муниципальные службы координируют график движения и ремонтных смен.",
                "cited_support_ids": [s.support_id],
            }
        )

    # Section 3: Stories 12..17
    sec3_paras = []
    sec3_sups = []
    for i in range(12, 18):
        sid = f"story:{i}"
        s = sup_map[sid]
        sec3_sups.append(s.support_id)
        sec3_paras.append(
            {
                "text": f"{s.text} Местные жители отмечают постепенное расширение доступных возможностей.",
                "cited_support_ids": [s.support_id],
            }
        )

    return json.dumps(
        {
            "title": t_text,
            "title_support_ids": t_sups,
            "title_claims": [
                {
                    "text": t_text,
                    "cited_support_ids": t_sups,
                }
            ],
            "lead": l_text,
            "lead_support_ids": l_sups,
            "lead_claims": [
                {
                    "text": l_text,
                    "cited_support_ids": l_sups,
                }
            ],
            "sections": [
                {
                    "heading": "Энергетика и коммунальное хозяйство",
                    "heading_support_ids": sec1_sups[:3],
                    "paragraphs": sec1_paras,
                },
                {
                    "heading": "Транспортная инфраструктура и городские службы",
                    "heading_support_ids": sec2_sups[:3],
                    "paragraphs": sec2_paras,
                },
                {
                    "heading": "Социальная сфера, спорт и городская среда",
                    "heading_support_ids": sec3_sups[:3],
                    "paragraphs": sec3_paras,
                },
            ],
        }
    )


def _make_article_generator(
    *,
    article_editor_enabled: bool = False,
    article_allow_deterministic_fallback: bool = False,
) -> ArticleGenerator:
    settings = Settings(
        schedule_time="19:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4o",
        openai_temperature=0.3,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=150,
            article_max_words=2200,
            article_min_sections=3,
            article_max_sections=8,
            article_editor_enabled=article_editor_enabled,
            article_allow_deterministic_fallback=article_allow_deterministic_fallback,
        ),
    )
    config = Config(
        channels=[],
        settings=settings,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )
    gen = ArticleGenerator(config=config, logger=logging.getLogger("test_recovery_matrix"))
    gen.provider = AsyncMock()
    return gen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_1_globally_incomplete_draft_triggers_regeneration_not_editor() -> None:
    """Case 1: 1/17 stories + 79 words -> Full AI regeneration requested, ArticleEditor NOT called first."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    resp_attempt_1 = _build_incomplete_79_word_response(list(context.support_index))
    resp_attempt_2 = _build_complete_longread_response(list(context.support_index))
    generator.provider.chat_completion.side_effect = [resp_attempt_1, resp_attempt_2]

    observer = RecordingAttemptObserver()
    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    assert title
    assert body
    # Chat completion must be called at least twice (Attempt 1 + Attempt 2 regeneration)
    assert generator.provider.chat_completion.call_count == 2

    # Attempt 1 was marked failed due to global incompleteness with retry scheduled
    finished_att_1 = observer.finished_attempts[1]
    assert finished_att_1["status"] == "failed"
    assert finished_att_1["kwargs"]["error_kind"] == "global_incompleteness_retry"
    assert finished_att_1["kwargs"]["metadata"]["retry_scheduled"] is True
    assert finished_att_1["kwargs"]["metadata"]["next_attempt"] == 2
    assert finished_att_1["kwargs"]["metadata"]["regeneration_reason"] == "global_incompleteness"

    # ArticleEditor was NOT called between attempt 1 and 2
    started_strategies = [
        att["kwargs"].get("metadata", {}).get("strategy") for att in observer.started_attempts
    ]
    assert "article_editor" not in started_strategies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_2_regeneration_recovers_complete_valid_article_without_fallback() -> None:
    """Case 2: Regeneration returns complete valid article -> Accepted, deterministic fallback not used."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    resp_attempt_1 = _build_incomplete_79_word_response(list(context.support_index))
    resp_attempt_2 = _build_complete_longread_response(list(context.support_index))
    generator.provider.chat_completion.side_effect = [resp_attempt_1, resp_attempt_2]

    observer = RecordingAttemptObserver()
    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    assert title
    assert len(body.split()) >= 150
    assert "deterministic_fallback" not in observer.started_kinds
    assert "deterministic_supplement" not in observer.started_kinds

    # Final attempt succeeded cleanly with event_article_writer
    att_2_id = observer.started_attempts[1]["id"]
    finished_att_2 = observer.finished_attempts[att_2_id]
    assert finished_att_2["status"] == "succeeded"
    assert finished_att_2["kwargs"]["metadata"]["winning_kind"] == "event_article_writer"
    assert finished_att_2["kwargs"]["metadata"]["recovery_mode"] == "none"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_3_regeneration_with_local_issue_triggers_article_editor_repair() -> None:
    """Case 3: Regeneration returns complete article with HISTORICAL_CONTEXT_UNFRAMED:LEAD -> ArticleEditor repairs LEAD -> Accepted."""
    context, plan = _make_17_story_setup(lead_temporal_role="HISTORICAL_CONTEXT")
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    supports = list(context.support_index)
    sup0 = supports[0]  # HISTORICAL_CONTEXT
    sup1 = supports[1]  # CURRENT_WINDOW

    resp_attempt_1 = _build_incomplete_79_word_response(supports)

    # Lead has historical sup0 + current window sup1, but lacks continuation framing -> HISTORICAL_CONTEXT_UNFRAMED:LEAD
    unframed_lead = f"{sup0.text} {sup1.text}"
    resp_attempt_2 = _build_complete_longread_response(
        supports,
        lead_text=unframed_lead,
        lead_sups=[sup0.support_id, sup1.support_id],
        title_text="Восстановление сетей в микрорайоне 2 Бердянска",
        title_sups=[sup1.support_id],
    )

    # Editor patches LEAD by adding continuation framing
    editor_patch_response = json.dumps(
        {"units": {"LEAD": f"Как сообщалось ранее, {sup0.text} При этом {sup1.text}"}}
    )

    generator.provider.chat_completion.side_effect = [
        resp_attempt_1,
        resp_attempt_2,
        editor_patch_response,
    ]

    observer = RecordingAttemptObserver()
    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    assert title
    assert "Как сообщалось ранее" in lead
    assert generator.provider.chat_completion.call_count == 3

    # ArticleEditor was called for targeted repair after complete regeneration
    repair_attempts = [att for att in observer.started_attempts if att.get("kind") == "repair"]
    assert len(repair_attempts) == 1
    assert repair_attempts[0]["kwargs"]["metadata"]["strategy"] == "article_editor"
    assert "LEAD" in repair_attempts[0]["kwargs"]["metadata"]["units"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_4_both_writer_attempts_incomplete_fails_closed() -> None:
    """Case 4: Both writer attempts catastrophically incomplete + deterministic fallback false -> ArticlePublicationRejected."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    resp_attempt_1 = _build_incomplete_79_word_response(list(context.support_index))
    resp_attempt_2 = _build_incomplete_79_word_response(list(context.support_index))
    generator.provider.chat_completion.side_effect = [resp_attempt_1, resp_attempt_2]

    observer = RecordingAttemptObserver()
    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await generator.generate_from_event_article_context(
            context,
            coverage_plan=plan,
            attempt_observer=observer,
        )

    # Incompleteness rejection is fail-closed, never emitting an 79-word stub
    assert exc_info.value.reason in ("global_incompleteness", "validation_failed")
    assert "deterministic_fallback" not in observer.started_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_5_valid_draft_missing_one_brief_allows_deterministic_supplement() -> None:
    """Case 5: Valid AI draft covers 16/17, missing one BRIEF -> Deterministic supplement allowed -> Final coverage == 1.0."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)

    # Draft covers stories 1..16 (all 3 DEVELOP, all 7 WEAVE, 6/7 BRIEF). Missing only story:17 (BRIEF).
    # Coverage = 16/17 = 94.1% >= 80%, missing = 1 <= 3, develop_coverage = 3/3 = 100%.
    raw_draft = _build_complete_longread_response(supports)
    draft_dict = json.loads(raw_draft)

    # Remove story:17 from section 3
    draft_dict["sections"][2]["paragraphs"] = [
        p
        for p in draft_dict["sections"][2]["paragraphs"]
        if "story:17" not in str(p.get("cited_support_ids", []))
    ]
    draft_dict["sections"][2]["heading_support_ids"] = [
        sid for sid in draft_dict["sections"][2]["heading_support_ids"] if "story:17" not in sid
    ]

    valid_incomplete_draft = StructuredArticleDraft.from_dict(draft_dict)
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_words=150,
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    result = await finalizer.finalize(
        writer_draft=valid_incomplete_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        attempt_observer=observer,
    )

    assert result.recovery_mode == "supplement"
    assert result.supplemented_story_ids == ("story:17",)
    assert set(result.final_covered_story_ids) == set(plan.story_ids)
    assert result.metadata["final_story_coverage"] == 1.0
    assert "deterministic_supplement" in observer.started_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_6_valid_draft_covering_1_of_17_forbids_supplement_and_fails_closed() -> None:
    """Case 6: Valid AI draft covers 1/17 -> Supplement forbidden -> Rejection path."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)
    sup1 = supports[0]

    # Minimal compliant draft covering only story:1
    d = {
        "title": "Восстановление сетей в микрорайоне Бердянска",
        "title_support_ids": [sup1.support_id],
        "lead": sup1.text,
        "lead_support_ids": [sup1.support_id],
        "sections": [
            {
                "heading": "Энергоснабжение",
                "heading_support_ids": [sup1.support_id],
                "paragraphs": [
                    {
                        "text": f"{sup1.text} Ремонтные бригады отчитались о завершении восстановительных работ.",
                        "cited_support_ids": [sup1.support_id],
                    },
                    {
                        "text": f"{sup1.text} Горожане подтверждают появление стабильного напряжения в жилых домах.",
                        "cited_support_ids": [sup1.support_id],
                    },
                ],
            },
            {
                "heading": "Ход восстановительных работ",
                "heading_support_ids": [sup1.support_id],
                "paragraphs": [
                    {
                        "text": f"{sup1.text} Специалисты провели наладку распределительных узлов.",
                        "cited_support_ids": [sup1.support_id],
                    },
                    {
                        "text": f"{sup1.text} Бригады завершили наладку оборудования.",
                        "cited_support_ids": [sup1.support_id],
                    },
                ],
            },
            {
                "heading": "Итоги дня",
                "heading_support_ids": [sup1.support_id],
                "paragraphs": [
                    {
                        "text": f"{sup1.text} Коммунальные службы завершили ключевой этап мероприятий.",
                        "cited_support_ids": [sup1.support_id],
                    },
                ],
            },
        ],
    }
    one_story_draft = StructuredArticleDraft.from_dict(d)

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_words=50,
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=one_story_draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    # Coverage is 1/17 (5.88% < 80%) -> supplement is strictly forbidden!
    assert exc_info.value.reason == "global_incompleteness"
    assert "deterministic_supplement" not in observer.started_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_7_missing_develop_story_forbids_supplement_and_fails_closed() -> None:
    """Case 7: Any missing DEVELOP story -> Supplement forbidden."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)

    # Draft covers 16/17 stories, BUT the omitted story is story:1 (DEVELOP!)
    sup2_id = supports[1].support_id
    raw_draft = _build_complete_longread_response(
        supports,
        lead_text=supports[1].text,
        lead_sups=[sup2_id],
        title_text="Восстановление сетей в микрорайоне 2 Бердянска",
        title_sups=[sup2_id],
    )
    draft_dict = json.loads(raw_draft)

    # Remove story:1 from section 0
    draft_dict["sections"][0]["paragraphs"] = [
        p
        for p in draft_dict["sections"][0]["paragraphs"]
        if "story:1:" not in str(p.get("cited_support_ids", []))
    ]
    draft_dict["sections"][0]["heading_support_ids"] = [
        sid for sid in draft_dict["sections"][0]["heading_support_ids"] if "story:1:" not in sid
    ]

    missing_develop_draft = StructuredArticleDraft.from_dict(draft_dict)
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_words=150,
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=missing_develop_draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    # develop_story_coverage is 2/3 (66.7% < 100%) -> supplement is strictly forbidden!
    assert exc_info.value.reason == "global_incompleteness"
    assert "deterministic_supplement" not in observer.started_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_8_final_article_enforces_evidence_boundary() -> None:
    """Case 8: Final article still enforces Evidence Boundary -> Unsupported claim remains blocking."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)

    # Complete draft covering all 17 stories, but containing an invented unsupported proper name
    raw_draft = _build_complete_longread_response(supports)
    draft_dict = json.loads(raw_draft)
    # Inject hallucinated city / proper name
    draft_dict["sections"][0]["paragraphs"][0]["text"] = (
        "По заявлению префекта Парижа Иванова, в Бердянске завершились восстановительные работы."
    )

    hallucinated_draft = StructuredArticleDraft.from_dict(draft_dict)
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_words=150,
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=hallucinated_draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    # Evidence boundary rejection is fail-closed
    assert exc_info.value.reason == "validation_failed"
    violations = exc_info.value.metadata.get("violations", [])
    assert any("UNSUPPORTED_" in v for v in violations)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_9_grounding_path_preserves_writer_coverage_and_triggers_regeneration_for_long_single_story_draft() -> (
    None
):
    """Regression test: raw writer output has 250 words about only Story 1.

    Ensures that _ground_draft_in_coverage_plan does NOT artificially inject
    supports from Stories 2..17, diagnosing true 1/17 coverage and triggering
    full AI regeneration.
    """
    from src.article_generator import _ground_draft_in_coverage_plan, _is_globally_incomplete
    from src.publication.article_coverage_diagnostics import diagnose_article_coverage
    from src.publication.article_validator import validate_article_draft

    context, plan = _make_17_story_setup()
    assert len(plan.stories) == 17
    sup_1 = list(context.support_index)[0].support_id

    # Raw model output: ~160 words strictly about Story 1 (power blackout)
    p1 = (
        "В Бердянске в районе Гора продолжаются перебои с электроснабжением из-за повреждения линии. "
        "Энергетики аварийных бригад ведут восстановительные работы на трансформаторной подстанции. "
        "По предварительным данным ремонтников, подачу электроэнергии планируют возобновить к вечеру. "
        "Жители микрорайона отмечают временное отключение света в жилых домах и на прилегающих улицах."
    )
    p2 = (
        "Специалисты продолжают устранять локальные повреждения сетей в жилом массиве на Горе. "
        "По информации дежурных служб, на объекте задействованы две бригады монтеров и спецтехника. "
        "Основная задача бригад состоит в замене вышедшего из строя кабельного участка и изоляторов. "
        "Районная администрация держит ситуацию с электроснабжением жилого сектора на постоянном контроле."
    )
    p3 = (
        "Местные жители сообщают о перепадах напряжения в электросети перед окончательным отключением света. "
        "Аварийные службы заверяют, что все необходимые комплектующие уже доставлены на место аварии. "
        "После завершения монтажных работ начнется постепенное подключение абонентов к питающей сети подстанции. "
        "Ремонтные подразделения планируют полностью закрыть заявку до наступления ночного времени суток."
    )
    single_story_raw_dict = {
        "title": "Электроснабжение на Горе восстанавливают после аварии",
        "title_support_ids": [sup_1],
        "lead": "В районе Гора ремонтные бригады ведут срочные работы по восстановлению электроснабжения после аварии на сетях.",
        "lead_support_ids": [sup_1],
        "lead_claims": [
            {
                "text": "В районе Гора ремонтные бригады ведут срочные работы по восстановлению электроснабжения после аварии на сетях.",
                "cited_support_ids": [sup_1],
            }
        ],
        "sections": [
            {
                "heading": "Ход ремонтных работ на энергосетях",
                "heading_support_ids": [sup_1],
                "paragraphs": [
                    {
                        "text": p1,
                        "cited_support_ids": [sup_1],
                        "claims": [{"text": p1, "cited_support_ids": [sup_1]}],
                    },
                    {
                        "text": p2,
                        "cited_support_ids": [sup_1],
                        "claims": [{"text": p2, "cited_support_ids": [sup_1]}],
                    },
                ],
            },
            {
                "heading": "Сроки завершения подключения",
                "heading_support_ids": [sup_1],
                "paragraphs": [
                    {
                        "text": p3,
                        "cited_support_ids": [sup_1],
                        "claims": [{"text": p3, "cited_support_ids": [sup_1]}],
                    },
                ],
            },
        ],
    }

    # 1. Grounding must NOT inject support IDs from stories 2..17
    grounded_dict = _ground_draft_in_coverage_plan(single_story_raw_dict, plan, context)
    grounded_draft = StructuredArticleDraft.from_dict(grounded_dict)

    # Verify length is long enough to exceed word count minimum (>= 150 words)
    assert (
        grounded_draft.word_count >= 150
    ), f"Expected >= 150 words, got {grounded_draft.word_count}"

    # Verify coverage diagnostics on grounded draft
    diag = diagnose_article_coverage(grounded_draft, plan)
    assert diag.covered_story_count == 1
    assert list(diag.covered_story_ids) == ["story:1"]
    assert diag.story_coverage == pytest.approx(1 / 17, abs=1e-3)
    assert len(diag.uncovered_story_ids) == 16

    val = validate_article_draft(grounded_draft, context)
    is_incomplete = _is_globally_incomplete(val, diag)
    assert (
        is_incomplete is True
    ), "Draft covering only 1 of 17 stories must be classified as globally incomplete"

    # 2. Generator must trigger AI regeneration and record attempt 1 with retry metadata
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )
    resp_attempt_1 = json.dumps(single_story_raw_dict)
    resp_attempt_2 = _build_complete_longread_response(list(context.support_index))
    generator.provider.chat_completion.side_effect = [resp_attempt_1, resp_attempt_2]
    observer = RecordingAttemptObserver()
    title, lead, body = await generator.generate_from_event_article_context(
        context, plan, attempt_observer=observer
    )

    assert title
    assert body
    # Chat completion was called twice (Attempt 1 + Attempt 2 regeneration)
    assert generator.provider.chat_completion.call_count == 2

    # Attempt 1 was recorded failed with global_incompleteness_retry
    att_1 = observer.finished_attempts[1]
    assert att_1["status"] == "failed"
    assert att_1["kwargs"]["error_kind"] == "global_incompleteness_retry"
    assert att_1["kwargs"]["metadata"]["retry_scheduled"] is True
    assert att_1["kwargs"]["metadata"]["next_attempt"] == 2
    assert att_1["kwargs"]["metadata"]["covered_story_count"] == 1
