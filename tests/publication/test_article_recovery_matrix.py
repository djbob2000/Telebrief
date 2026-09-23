"""Permanent acceptance and unit recovery matrix tests for Event-First articles.

The matrix covers single-call writing, targeted copy-editing, grounded partial
coverage, deterministic recovery boundaries, and fail-closed Evidence Boundary
validation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
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
            text = (
                f"В микрорайоне {i} Бердянска специалисты завершили восстановление электросетей "
                f"и подстанции. После работ подача электричества вернулась в дома микрорайона {i}."
            )
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
    l_text = lead_text or supports[0].text.split(". ", maxsplit=1)[0] + "."
    l_sups = lead_sups or [supports[0].support_id]

    def grouped_paragraphs(story_numbers: range) -> tuple[list[dict[str, object]], list[str]]:
        section_stories = [sup_map[f"story:{number}"] for number in story_numbers]
        section_support_ids = [support.support_id for support in section_stories]
        paragraphs: list[dict[str, object]] = []
        for offset in range(0, len(section_stories), 3):
            group = section_stories[offset : offset + 3]
            group_support_ids = [support.support_id for support in group]
            claims = [
                {
                    "text": (
                        support.text.partition(". ")[2]
                        if support.support_id in l_sups and ". " in support.text
                        else support.text
                    ),
                    "cited_support_ids": [support.support_id],
                }
                for support in group
            ]
            paragraphs.append(
                {
                    "text": " ".join(support.text for support in group),
                    "cited_support_ids": group_support_ids,
                    "claims": claims,
                }
            )
        return paragraphs, section_support_ids

    # Each body paragraph develops a small group of related source claims. When
    # a DEVELOP Story also appears in the lead, the body uses its distinct
    # supported consequence rather than repeating the lead's setup.
    sec1_paras, sec1_sups = grouped_paragraphs(range(1, 6))
    sec2_paras, sec2_sups = grouped_paragraphs(range(6, 12))
    sec3_paras, sec3_sups = grouped_paragraphs(range(12, 18))

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
async def test_case_1_catastrophically_incomplete_draft_is_not_regenerated() -> None:
    """A short but populated invalid draft is not mistaken for an empty refusal."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=False,
        article_allow_deterministic_fallback=False,
    )

    generator.provider.chat_completion.return_value = _build_incomplete_79_word_response(
        list(context.support_index)
    )

    observer = RecordingAttemptObserver()
    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await generator.generate_from_event_article_context(
            context,
            coverage_plan=plan,
            attempt_observer=observer,
        )

    assert generator.provider.chat_completion.call_count == 1
    assert exc_info.value.reason in {"global_incompleteness", "validation_failed"}
    assert "global_incompleteness_retry" not in {
        item.get("kwargs", {}).get("error_kind") for item in observer.finished_attempts.values()
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_1_catastrophic_draft_falls_back_to_second_model_after_same_provider_retry():
    """A repeated refusal uses the next configured model, not deterministic fallback."""
    from src.ai_providers import ProviderCascade

    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=False,
        article_allow_deterministic_fallback=False,
    )
    first_response = json.dumps(
        {
            "title": "Пожалуйста, предоставьте материалы для подготовки хроники.",
            "lead": "",
            "sections": [],
        }
    )
    primary = MagicMock()
    primary.chat_completion = AsyncMock(side_effect=[first_response, first_response])
    backup = MagicMock()
    backup.chat_completion = AsyncMock(
        return_value=_build_complete_longread_response(list(context.support_index))
    )
    generator.provider = ProviderCascade(
        [("primary", primary), ("secondary", backup)], generator.logger
    )

    observer = RecordingAttemptObserver()
    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    assert title
    assert lead
    assert body
    assert primary.chat_completion.call_count == 2
    assert backup.chat_completion.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_1_empty_lead_retries_even_when_body_has_content() -> None:
    """A substantive body with no lead is treated as a truncated writer response."""
    from src.ai_providers import ProviderCascade

    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=False,
        article_allow_deterministic_fallback=False,
    )
    incomplete = json.loads(_build_complete_longread_response(list(context.support_index)))
    incomplete["lead"] = ""
    incomplete["lead_support_ids"] = []
    incomplete["lead_claims"] = []
    incomplete_response = json.dumps(incomplete)
    complete_response = _build_complete_longread_response(list(context.support_index))

    primary = MagicMock()
    primary.chat_completion = AsyncMock(side_effect=[incomplete_response, complete_response])
    backup = MagicMock()
    backup.chat_completion = AsyncMock()
    generator.provider = ProviderCascade(
        [("primary", primary), ("secondary", backup)], generator.logger
    )

    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
    )

    assert title
    assert lead
    assert body
    assert primary.chat_completion.call_count == 2
    assert backup.chat_completion.call_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_2_local_issue_triggers_targeted_article_editor_repair() -> None:
    """A valid writer draft with a local issue gets targeted repair, not full regeneration."""
    context, plan = _make_17_story_setup(lead_temporal_role="HISTORICAL_CONTEXT")
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    supports = list(context.support_index)
    sup0 = supports[0]  # HISTORICAL_CONTEXT
    sup1 = supports[1]  # CURRENT_WINDOW

    # Lead has historical sup0 + current window sup1, but lacks continuation framing -> HISTORICAL_CONTEXT_UNFRAMED:LEAD
    unframed_lead = (
        f"{sup0.text.split('. ', maxsplit=1)[0]}. {sup1.text.split('. ', maxsplit=1)[0]}."
    )
    resp_attempt_1 = _build_complete_longread_response(
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
    # The one local lead issue is repaired in one targeted editor call; the
    # lead's Story is not duplicated in the body fixture.
    assert generator.provider.chat_completion.call_count == 2

    # ArticleEditor was called for targeted repair after the single writer call
    repair_attempts = [att for att in observer.started_attempts if att.get("kind") == "repair"]
    assert len(repair_attempts) == 1
    assert repair_attempts[0]["kwargs"]["metadata"]["strategy"] == "article_editor"
    assert "LEAD" in repair_attempts[0]["kwargs"]["metadata"]["units"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quality_only_generator_repair_uses_one_writer_and_bounded_editor_calls() -> None:
    """A writer inventory gets bounded unit edits and final structural validation."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )
    supports = list(context.support_index)
    writer_dict = json.loads(_build_complete_longread_response(supports))
    middle_groups = writer_dict["sections"][1]["paragraphs"]
    writer_dict["sections"][1]["paragraphs"] = [
        {
            "text": claim["text"],
            "cited_support_ids": claim["cited_support_ids"],
            "claims": [claim],
        }
        for group in middle_groups
        for claim in group["claims"]
    ]
    first_inventory_text = writer_dict["sections"][1]["paragraphs"][0]["text"]
    writer_response = json.dumps(writer_dict)
    editor_response = json.dumps({"units": {"P003": first_inventory_text}})
    generator.provider.chat_completion.side_effect = [
        writer_response,
        editor_response,
        editor_response,
    ]
    observer = RecordingAttemptObserver()

    title, lead, body = await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    assert title and lead and body
    assert generator.provider.chat_completion.call_count == 3
    repairs = [item for item in observer.started_attempts if item["kind"] == "repair"]
    assert len(repairs) == 2
    assert all(
        "ARTICLE_INVENTORY_RHYTHM" in violation
        for violation in repairs[0]["kwargs"]["metadata"]["violations"]
    )
    writer_finish = observer.finished_attempts[1]
    assert writer_finish["status"] == "succeeded"
    assert writer_finish["kwargs"]["metadata"]["writer_attempt"]["editor_retry_count"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_metadata_uses_edition_local_as_of_and_retains_utc() -> None:
    context, plan = _make_17_story_setup()
    context = replace(
        context,
        publication_window=PublicationWindow(
            snapshot_at=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc),
            lookback_start=dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc),
        ),
        edition_timezone="Europe/Kyiv",
    )
    generator = _make_article_generator(article_allow_deterministic_fallback=False)
    generator.provider.chat_completion.return_value = _build_complete_longread_response(
        list(context.support_index)
    )
    observer = RecordingAttemptObserver()

    await generator.generate_from_event_article_context(
        context,
        coverage_plan=plan,
        attempt_observer=observer,
    )

    metadata = observer.started_attempts[0]["kwargs"]["metadata"]
    assert metadata["as_of"] == "2026-09-05T15:00:00+03:00"
    assert metadata["as_of_utc"] == "2026-09-05T12:00:00+00:00"
    assert metadata["edition_timezone"] == "Europe/Kyiv"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_5_valid_partial_draft_is_accepted_without_supplement() -> None:
    """A grounded substantial draft may omit lower-priority BRIEF material."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)

    # Draft covers stories 1..16 (all 3 DEVELOP, all 7 WEAVE, 6/7 BRIEF). Missing only story:17 (BRIEF).
    # Coverage = 16/17 = 94.1% >= 80%, missing = 1 <= 3, develop_coverage = 3/3 = 100%.
    raw_draft = _build_complete_longread_response(supports)
    draft_dict = json.loads(raw_draft)

    # Remove only story:17 from its grouped paragraph while retaining the
    # neighboring BRIEF stories synthesized in that same paragraph.
    for paragraph in draft_dict["sections"][2]["paragraphs"]:
        claims = paragraph["claims"]
        remaining_claims = [
            claim for claim in claims if "story:17" not in str(claim.get("cited_support_ids", []))
        ]
        if len(remaining_claims) != len(claims):
            paragraph["claims"] = remaining_claims
            paragraph["cited_support_ids"] = list(
                dict.fromkeys(
                    support_id
                    for claim in remaining_claims
                    for support_id in claim["cited_support_ids"]
                )
            )
            paragraph["text"] = " ".join(claim["text"] for claim in remaining_claims)
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

    assert result.recovery_mode == "none"
    assert result.supplemented_story_ids == ()
    assert set(result.final_covered_story_ids) == {story.story_id for story in plan.stories[:-1]}
    assert result.metadata["coverage_only_diagnostic"] is True
    assert result.metadata["final_story_coverage"] == pytest.approx(16 / 17)
    assert "deterministic_supplement" not in observer.started_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_6_missing_develop_story_rejects_after_factual_validation() -> None:
    """An omitted DEVELOP storyline is a blocking reader-quality failure."""
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
                        "text": sup1.text,
                        "cited_support_ids": [sup1.support_id],
                    },
                    {
                        "text": sup1.text,
                        "cited_support_ids": [sup1.support_id],
                    },
                ],
            },
            {
                "heading": "Ход восстановительных работ",
                "heading_support_ids": [sup1.support_id],
                "paragraphs": [
                    {
                        "text": sup1.text,
                        "cited_support_ids": [sup1.support_id],
                    },
                    {
                        "text": sup1.text,
                        "cited_support_ids": [sup1.support_id],
                    },
                ],
            },
            {
                "heading": "Итоги дня",
                "heading_support_ids": [sup1.support_id],
                "paragraphs": [
                    {
                        "text": sup1.text,
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

    assert exc_info.value.reason == "quality_failed"
    assert exc_info.value.error_kind == "article_quality_rejected"
    assert exc_info.value.metadata["stage"] == "post_finalization_quality"
    assert writer_id in observer.finished_attempts
    assert observer.finished_attempts[writer_id]["status"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_7_missing_develop_story_fails_closed() -> None:
    """Missing DEVELOP coverage remains blocking after finalization."""
    context, plan = _make_17_story_setup()
    supports = list(context.support_index)

    # Draft covers 16/17 stories, BUT the omitted story is story:1 (DEVELOP!)
    sup2_id = supports[1].support_id
    raw_draft = _build_complete_longread_response(
        supports,
        lead_text=supports[1].text.split(". ", maxsplit=1)[0] + ".",
        lead_sups=[sup2_id],
        title_text="Восстановление сетей в микрорайоне 2 Бердянска",
        title_sups=[sup2_id],
    )
    draft_dict = json.loads(raw_draft)

    # Remove story:1 from section 0
    for paragraph in draft_dict["sections"][0]["paragraphs"]:
        claims = paragraph["claims"]
        remaining_claims = [
            claim for claim in claims if "story:1:" not in str(claim.get("cited_support_ids", []))
        ]
        if len(remaining_claims) != len(claims):
            paragraph["claims"] = remaining_claims
            paragraph["cited_support_ids"] = list(
                dict.fromkeys(
                    support_id
                    for claim in remaining_claims
                    for support_id in claim["cited_support_ids"]
                )
            )
            paragraph["text"] = " ".join(claim["text"] for claim in remaining_claims)
    draft_dict["sections"][0]["paragraphs"] = [
        paragraph for paragraph in draft_dict["sections"][0]["paragraphs"] if paragraph["claims"]
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

    assert exc_info.value.reason == "quality_failed"
    assert (
        exc_info.value.metadata["quality_after_finalization"]["counts_by_code"][
            "MISSING_DEVELOP_STORY"
        ]
        == 1
    )
    assert exc_info.value.metadata["stage"] == "post_finalization_quality"


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
    draft_dict["sections"][0]["paragraphs"][0]["claims"] = [
        {
            "text": draft_dict["sections"][0]["paragraphs"][0]["text"],
            "cited_support_ids": draft_dict["sections"][0]["paragraphs"][0]["cited_support_ids"],
        }
    ]

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
    violations = exc_info.value.metadata["factual_validation"]["issue_codes_and_units"]
    assert any("UNSUPPORTED_" in v for v in violations)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_9_grounding_path_preserves_writer_coverage_without_regeneration_for_long_single_story_draft() -> (
    None
):
    """Regression test: raw writer output has 250 words about only Story 1.

    Ensures that _ground_draft_in_coverage_plan does NOT artificially inject
    supports from Stories 2..17, diagnosing true 1/17 coverage without issuing
    a second full AI request.
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

    # 2. Generator must not trigger a second full writer request.
    generator = _make_article_generator(
        article_editor_enabled=False,
        article_allow_deterministic_fallback=False,
    )
    resp_attempt_1 = json.dumps(single_story_raw_dict)
    generator.provider.chat_completion.return_value = resp_attempt_1
    observer = RecordingAttemptObserver()
    with pytest.raises(ArticlePublicationRejected):
        await generator.generate_from_event_article_context(
            context, plan, attempt_observer=observer
        )

    assert generator.provider.chat_completion.call_count == 1
    assert len(observer.started_attempts) == 1
    assert "global_incompleteness_retry" not in {
        item.get("kwargs", {}).get("error_kind") for item in observer.finished_attempts.values()
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_10_missing_develop_story_gets_bounded_editor_attempts_then_rejects() -> None:
    """The editor gets bounded targeted attempts before quality rejection."""
    context, plan = _make_17_story_setup()
    generator = _make_article_generator(
        article_editor_enabled=True,
        article_allow_deterministic_fallback=False,
    )

    sup_1 = list(context.support_index)[0]
    t = sup_1.text

    headings = [
        "Восстановление электросетей",
        "Ход восстановительных работ",
        "Итоги восстановительных работ",
    ]
    sections = []
    for h in headings:
        paras = []
        for _ in range(4):
            p_text = (
                f"{t} Специалисты в Бердянске завершили восстановление электросетей на подстанции."
            )
            paras.append(
                {
                    "text": p_text,
                    "cited_support_ids": [sup_1.support_id],
                    "claims": [{"text": p_text, "cited_support_ids": [sup_1.support_id]}],
                }
            )
        sections.append(
            {
                "heading": h,
                "heading_support_ids": [sup_1.support_id],
                "paragraphs": paras,
            }
        )

    valid_1_story_dict = {
        "title": "Восстановление сетей в микрорайоне 1 Бердянска",
        "title_support_ids": [sup_1.support_id],
        "lead": t,
        "lead_support_ids": [sup_1.support_id],
        "lead_claims": [{"text": t, "cited_support_ids": [sup_1.support_id]}],
        "sections": sections,
    }

    resp_attempt_1 = json.dumps(valid_1_story_dict)
    generator.provider.chat_completion.side_effect = [
        resp_attempt_1,
        json.dumps({"units": {"P001": t}}),
        json.dumps({"units": {"P001": t}}),
    ]

    observer = RecordingAttemptObserver()
    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await generator.generate_from_event_article_context(
            context,
            coverage_plan=plan,
            attempt_observer=observer,
        )

    assert exc_info.value.reason == "quality_failed"
    assert generator.provider.chat_completion.call_count == 3
    repair_attempts = [att for att in observer.started_attempts if att.get("kind") == "repair"]
    assert len(repair_attempts) == 2
    assert any(
        any(
            "MISSING_DEVELOP_STORY" in violation
            for violation in repair["kwargs"]["metadata"]["violations"]
        )
        for repair in repair_attempts
    )
    assert (
        exc_info.value.metadata["quality_before_edit"]["counts_by_code"]["MISSING_DEVELOP_STORY"]
        == 2
    )
    assert all(
        item["code"] == "CROSS_SECTION_REPETITION"
        for item in exc_info.value.metadata["unresolved_quality_findings"]
    )
