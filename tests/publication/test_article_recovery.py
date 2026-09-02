"""Unit and acceptance tests for Event-First article deterministic recovery and finalizer."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_recovery import ArticleDeterministicComposer

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


def _make_support(
    support_id: str,
    story_id: str,
    text: str,
    evidence_kind: str = "established_fact",
    source_text: str = "",
    temporal_role: str = "CURRENT_WINDOW",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=source_text or text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(f"ref:{support_id}",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role=temporal_role,
        evidence_kind=evidence_kind,
        story_id=story_id,
    )


def _make_plan_and_context() -> tuple[ArticleCoveragePlan, ArticleEditorialContext]:
    sup1 = _make_support(
        "story:1:evidence:0:frag:101",
        "story:1",
        "В микрорайоне восстановили подачу электроэнергии.",
        evidence_kind="established_fact",
    )
    sup2 = _make_support(
        "story:2:evidence:0:frag:202",
        "story:2",
        "Автобус №4 курсирует с интервалом в 30 минут.",
        evidence_kind="community_report",
    )
    sup3 = _make_support(
        "story:3:evidence:0:frag:303",
        "story:3",
        "В спорткомплексе открылся набор в секцию плавания.",
        evidence_kind="established_fact",
    )
    supports = (sup1, sup2, sup3)
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Городской транспорт", "Спорт"),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id,),
                detail_support_ids=(sup1.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Городской транспорт",
                rank=2,
                prominence="DEVELOP",
                support_ids=(sup2.support_id,),
                detail_support_ids=(sup2.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:3",
                topic="Спорт",
                rank=3,
                prominence="BRIEF",
                support_ids=(sup3.support_id,),
                detail_support_ids=(sup3.support_id,),
            ),
        )
    )
    return plan, context


@pytest.mark.unit
def test_supplement_safe_draft_preserves_ai_units_and_adds_supplement() -> None:
    plan, context = _make_plan_and_context()
    sup1 = context.support_by_id["story:1:evidence:0:frag:101"]

    ai_draft = StructuredArticleDraft(
        title="В городе восстанавливают электроснабжение",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        title_generation_origin="AI",
        lead="В микрорайоне восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        lead_generation_origin="AI",
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(sup1.support_id,),
                heading_claims=(
                    ArticleClaimAtom(
                        text="В микрорайоне восстановили подачу электроэнергии",
                        cited_support_ids=(sup1.support_id,),
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="В микрорайоне восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В микрорайоне восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                        generation_origin="AI",
                    ),
                ),
                heading_generation_origin="AI",
            ),
        ),
    )

    composer = ArticleDeterministicComposer()
    result = composer.supplement_safe_draft(
        draft=ai_draft,
        uncovered_story_ids=("story:2", "story:3"),
        context=context,
        plan=plan,
    )

    assert result.title == ai_draft.title
    assert result.lead == ai_draft.lead
    assert result.sections[: len(ai_draft.sections)] == ai_draft.sections
    assert any(
        p.generation_origin == "SUPPLEMENT"
        for section in result.sections
        for p in section.paragraphs
    )
    assert "story:2:evidence:0:frag:202" in result.cited_support_ids
    assert "story:3:evidence:0:frag:303" in result.cited_support_ids

    # DEVELOP miss (story:2) gets its own section, BRIEF miss (story:3) goes to short section
    assert len(result.sections) == 3
    assert result.sections[1].heading == "Городской транспорт"
    assert result.sections[1].heading_generation_origin == "SUPPLEMENT"
    assert result.sections[2].heading == "Коротко о других событиях города"
    assert result.sections[2].heading_generation_origin == "SUPPLEMENT"


@pytest.mark.unit
def test_render_full_fallback_provenance_and_community_attribution() -> None:
    plan, context = _make_plan_and_context()
    composer = ArticleDeterministicComposer()
    fallback = composer.render_full_fallback(context=context, plan=plan)

    assert fallback.cited_support_ids
    assert fallback.title_generation_origin == "FALLBACK"
    assert fallback.lead_generation_origin == "FALLBACK"
    assert all(
        p.generation_origin == "FALLBACK"
        for section in fallback.sections
        for p in section.paragraphs
    )
    assert set(plan.story_ids).issubset(
        {
            story_id
            for story_id, support_ids in plan.support_ids_by_story.items()
            if set(support_ids) & set(fallback.cited_support_ids)
        }
    )

    # Community report (story:2) must have natural attribution
    story2_text = [
        p.text
        for s in fallback.sections
        for p in s.paragraphs
        if "story:2:evidence:0:frag:202" in p.cited_support_ids
    ]
    assert story2_text
    assert story2_text[0].startswith("По сообщениям жителей,")


@pytest.mark.unit
def test_deterministic_temporal_and_contact_sanitation() -> None:
    sup_hist = _make_support(
        "story:10:evidence:0:frag:1",
        "story:10",
        "отремонтировали участок теплотрассы",
        evidence_kind="established_fact",
        temporal_role="HISTORICAL_CONTEXT",
    )
    sup_sched = _make_support(
        "story:10:evidence:1:frag:2",
        "story:10",
        "проведение гидравлических испытаний",
        evidence_kind="established_fact",
        temporal_role="FUTURE_SCHEDULED",
    )
    sup_leak = _make_support(
        "story:11:evidence:0:frag:3",
        "story:11",
        "Справки по телефону +7 990 123-45-67 и на сайте https://example.com/info.",
        source_text="Справки по телефону +7 990 123-45-67 и на сайте https://example.com/info.",
        evidence_kind="established_fact",
    )
    supports = (sup_hist, sup_sched, sup_leak)
    context = ArticleEditorialContext(
        headline_candidates=("ЖКХ", "Справки"),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:10",
                topic="ЖКХ",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup_hist.support_id, sup_sched.support_id),
                detail_support_ids=(sup_hist.support_id, sup_sched.support_id),
            ),
            ArticleStoryCoverage(
                story_id="story:11",
                topic="Справки",
                rank=2,
                prominence="BRIEF",
                support_ids=(sup_leak.support_id,),
                detail_support_ids=(sup_leak.support_id,),
            ),
        )
    )

    composer = ArticleDeterministicComposer()
    fallback = composer.render_full_fallback(context=context, plan=plan)

    full_text = fallback.render_markdown()
    assert "+7 990" not in full_text
    assert "https://" not in full_text
    assert "Ранее" in full_text
    assert "Запланировано:" in full_text


class RecordingAttemptObserver:
    def __init__(self) -> None:
        self.started_attempts: list[dict] = []
        self.finished_attempts: dict[int, dict] = {}
        self.started_kinds: list[str] = []
        self._next_id = 1

    async def attempt_started(self, kind: str, **kwargs) -> int:
        att_id = self._next_id
        self._next_id += 1
        self.started_attempts.append({"id": att_id, "kind": kind, "kwargs": kwargs})
        self.started_kinds.append(kind)
        return att_id

    async def attempt_finished(self, attempt_id: int, status: str, **kwargs) -> None:
        self.finished_attempts[attempt_id] = {"status": status, "kwargs": kwargs}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_safe_incomplete_writer_supplements() -> None:
    from src.config_loader import PublicationEditorialConfig
    from src.publication.article_finalization import ArticleFinalizer

    plan, context = _make_plan_and_context()
    sup1 = context.support_by_id["story:1:evidence:0:frag:101"]

    writer_draft = StructuredArticleDraft(
        title="В городе восстанавливают электроснабжение",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        title_generation_origin="AI",
        lead="В микрорайоне восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        lead_generation_origin="AI",
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(sup1.support_id,),
                heading_claims=(
                    ArticleClaimAtom(
                        text="В микрорайоне восстановили подачу электроэнергии",
                        cited_support_ids=(sup1.support_id,),
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="В микрорайоне восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В микрорайоне восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                        generation_origin="AI",
                    ),
                ),
                heading_generation_origin="AI",
            ),
        ),
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    result = await finalizer.finalize(
        writer_draft=writer_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "passed"
    assert result.recovery_mode == "supplement"
    assert result.ai_covered_story_ids == ("story:1",)
    assert set(result.supplemented_story_ids) == {"story:2", "story:3"}
    assert set(result.final_covered_story_ids) == {"story:1", "story:2", "story:3"}
    assert result.metadata["final_story_coverage"] == 1.0
    assert observer.started_kinds == ["writer", "deterministic_supplement"]
    assert observer.finished_attempts[writer_id]["status"] == "succeeded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_unsafe_writer_uses_full_fallback() -> None:
    from src.config_loader import PublicationEditorialConfig
    from src.publication.article_finalization import ArticleFinalizer

    plan, context = _make_plan_and_context()

    # Draft citing unknown support ID or unsupported claim
    unsafe_draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:999:unknown",),
        lead="Лид",
        lead_support_ids=("story:999:unknown",),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("story:999:unknown",),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст с неизвестным саппортом.",
                        cited_support_ids=("story:999:unknown",),
                    ),
                ),
            ),
        ),
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    result = await finalizer.finalize(
        writer_draft=unsafe_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "rejected"
    assert result.recovery_mode == "full_fallback"
    assert observer.finished_attempts[writer_id]["status"] == "failed"
    assert observer.started_kinds == ["writer", "deterministic_fallback"]
    assert result.metadata["final_story_coverage"] == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_writer_error_uses_full_fallback() -> None:
    from src.config_loader import PublicationEditorialConfig
    from src.publication.article_finalization import ArticleFinalizer

    plan, context = _make_plan_and_context()

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    result = await finalizer.finalize(
        writer_draft=None,
        writer_error=TimeoutError("writer timeout"),
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "failed"
    assert result.recovery_mode == "full_fallback"
    assert observer.finished_attempts[writer_id]["status"] == "failed"
    assert observer.started_kinds == ["writer", "deterministic_fallback"]
    assert result.metadata["final_story_coverage"] == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_supplement_escalation_to_fallback() -> None:
    from src.config_loader import PublicationEditorialConfig
    from src.publication.article_finalization import ArticleFinalizer

    plan, context = _make_plan_and_context()
    sup1 = context.support_by_id["story:1:evidence:0:frag:101"]

    writer_draft = StructuredArticleDraft(
        title="В городе восстанавливают электроснабжение",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        lead="В микрорайоне восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В микрорайоне восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(sup1.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В микрорайоне восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В микрорайоне восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    class BrokenSupplementComposer(ArticleDeterministicComposer):
        def supplement_safe_draft(self, draft, uncovered_story_ids, context, plan):
            # Returns an invalid draft
            return StructuredArticleDraft(
                title="",
                title_support_ids=(),
                lead="",
                lead_support_ids=(),
                sections=(),
            )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer(composer=BrokenSupplementComposer())
    editorial_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    result = await finalizer.finalize(
        writer_draft=writer_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.recovery_mode == "full_fallback"
    assert result.metadata["winning_kind"] == "event_article_deterministic_fallback"
    assert observer.started_kinds == [
        "writer",
        "deterministic_supplement",
        "deterministic_fallback",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_terminal_invariant_error() -> None:
    from src.config_loader import PublicationEditorialConfig
    from src.publication.article_finalization import ArticleFinalizer
    from src.publication.errors import ArticleFinalizationInvariantError

    plan, context = _make_plan_and_context()

    class BrokenComposer(ArticleDeterministicComposer):
        def render_full_fallback(self, context, plan):
            return StructuredArticleDraft(
                title="",
                title_support_ids=(),
                lead="",
                lead_support_ids=(),
                sections=(),
            )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer(composer=BrokenComposer())
    editorial_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    with pytest.raises(ArticleFinalizationInvariantError):
        await finalizer.finalize(
            writer_draft=None,
            writer_error=TimeoutError("writer timeout"),
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            length_profile=None,
            attempt_observer=observer,
        )


@pytest.fixture
def article_generator() -> Any:
    import logging
    from unittest.mock import AsyncMock

    from src.article_generator import ArticleGenerator
    from src.config_loader import Config, PublicationEditorialConfig, Settings

    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=5,
            article_min_sections=1,
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
    gen = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    gen.provider = AsyncMock()
    return gen


@pytest.fixture
def multi_story_context() -> ArticleEditorialContext:
    _, ctx = _make_plan_and_context()
    return ctx


@pytest.mark.unit
def test_article_publication_rejected_exposes_stable_reason_and_metadata() -> None:
    from src.publication.errors import ArticlePublicationRejected

    exc = ArticlePublicationRejected(
        reason="validation_failed",
        message="draft failed deterministic validation",
        metadata={"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]},
    )

    assert exc.reason == "validation_failed"
    assert exc.error_kind == "article_validation_rejected"
    assert exc.metadata == {"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]}
    assert str(exc) == "draft failed deterministic validation"


@pytest.mark.unit
def test_article_publication_rejected_maps_writer_failure_to_stable_error_kind() -> None:
    from src.publication.errors import ArticlePublicationRejected

    exc = ArticlePublicationRejected(
        reason="writer_failed",
        message="provider failed",
        metadata={"exception_type": "TimeoutError"},
    )

    assert exc.error_kind == "article_writer_rejected"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_article_writer_error_uses_full_fallback(
    article_generator,
    multi_story_context,
) -> None:
    article_generator.provider.chat_completion.side_effect = TimeoutError("writer timeout")
    observer = RecordingAttemptObserver()

    title, lead, body = await article_generator.generate_from_event_article_context(
        multi_story_context,
        attempt_observer=observer,
    )

    assert title
    assert body
    assert article_generator.provider.chat_completion.call_count == 1
    assert observer.started_kinds == ["writer", "deterministic_fallback"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_article_validation_failure_uses_full_fallback(
    article_generator,
    multi_story_context,
) -> None:
    import json

    sup_id = "story:1:evidence:0:frag:101"
    invalid_json = json.dumps(
        {
            "title": "Отключение света в центре",
            "title_support_ids": [sup_id],
            "title_claims": [{"text": "Отключение света в центре", "cited_support_ids": [sup_id]}],
            "lead": "В центре города авария на подстанции.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {"text": "В центре города авария на подстанции", "cited_support_ids": [sup_id]}
            ],
            "sections": [
                {
                    "heading": "Энергоснабжение",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [{"text": "Энергоснабжение", "cited_support_ids": [sup_id]}],
                    "paragraphs": [
                        {
                            "text": "Бригады восстановили питание в течение полутора часов.",
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "Бригады восстановили питание в течение полутора часов.",
                                    "cited_support_ids": [sup_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    article_generator.provider.chat_completion.return_value = invalid_json
    observer = RecordingAttemptObserver()

    title, lead, body = await article_generator.generate_from_event_article_context(
        multi_story_context,
        attempt_observer=observer,
    )

    assert title
    assert body
    assert article_generator.provider.chat_completion.call_count == 1
    assert observer.started_kinds == ["writer", "deterministic_fallback"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_article_safe_incomplete_uses_supplement(
    article_generator,
    multi_story_context,
) -> None:
    import json

    sup_id = "story:1:evidence:0:frag:101"
    valid_incomplete_json = json.dumps(
        {
            "title": "В микрорайоне восстановили подачу электроэнергии",
            "title_support_ids": [sup_id],
            "title_claims": [
                {
                    "text": "В микрорайоне восстановили подачу электроэнергии",
                    "cited_support_ids": [sup_id],
                }
            ],
            "lead": "В микрорайоне восстановили подачу электроэнергии.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {
                    "text": "В микрорайоне восстановили подачу электроэнергии",
                    "cited_support_ids": [sup_id],
                }
            ],
            "sections": [
                {
                    "heading": "Электроснабжение",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [],
                    "paragraphs": [
                        {
                            "text": "В микрорайоне восстановили подачу электроэнергии.",
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "В микрорайоне восстановили подачу электроэнергии",
                                    "cited_support_ids": [sup_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    article_generator.provider.chat_completion.return_value = valid_incomplete_json
    observer = RecordingAttemptObserver()

    title, lead, body = await article_generator.generate_from_event_article_context(
        multi_story_context,
        attempt_observer=observer,
    )

    assert title
    assert body
    assert article_generator.provider.chat_completion.call_count == 1
    assert observer.started_kinds == ["writer", "deterministic_supplement"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_article_prompt_contains_epistemic_fidelity_and_no_corroboration_gate(
    article_generator,
) -> None:
    import json

    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="На Горе света нет",
        source_text="На Горе света нет",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=now,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        source_roles=("community",),
    )
    context = ArticleEditorialContext(
        headline_candidates=("Сообщения жителей",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        evidence_index=(sup,),
        recurring_topics=("utilities",),
    )

    valid_json = json.dumps(
        {
            "title": "На Горе света нет",
            "title_support_ids": [sup.support_id],
            "title_claims": [{"text": "На Горе света нет", "cited_support_ids": [sup.support_id]}],
            "lead": "Жители сообщают о проблемах со светом на Горе.",
            "lead_support_ids": [sup.support_id],
            "lead_claims": [{"text": "На Горе света нет", "cited_support_ids": [sup.support_id]}],
            "sections": [
                {
                    "heading": "Обстановка на Горе",
                    "heading_support_ids": [sup.support_id],
                    "heading_claims": [],
                    "paragraphs": [
                        {
                            "text": "По сообщениям жителей, на Горе нет света.",
                            "cited_support_ids": [sup.support_id],
                            "claims": [
                                {
                                    "text": "На Горе света нет",
                                    "cited_support_ids": [sup.support_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    article_generator.provider.chat_completion.return_value = valid_json

    await article_generator.generate_from_event_article_context(context)

    assert article_generator.provider.chat_completion.call_count == 1
    call_kwargs = article_generator.provider.chat_completion.call_args.kwargs
    messages = call_kwargs["messages"]
    system_content = next(m["content"] for m in messages if m["role"] == "system")
    user_content = next(m["content"] for m in messages if m["role"] == "user")

    # Epistemic instructions present
    assert "community_report" in user_content
    assert "framing=attributed_report" in user_content
    assert "Epistemic Fidelity" in system_content or "epistemic" in system_content.lower()
    assert "ARTICLE COVERAGE PLAN" in user_content
    assert "DETAIL SUPPORTS:" in user_content

    # No second-source / corroboration gate
    assert "two independent sources" not in system_content.lower()
    assert "must be corroborated" not in system_content.lower()
    assert "official confirmation required" not in system_content.lower()


def test_theme_first_fallback_packing_and_deduplication():
    """Tests 9A, 9B, 9C, 9D, 9E: Theme-first fallback packing with conservative dedup and safe title/lead."""
    from src.config_loader import PublicationEditorialConfig
    from src.editorial_models import StoryCard
    from src.publication.article_recovery import resolve_article_theme
    from src.publication.article_validator import validate_article_draft

    # Test 9A: Canonical mapping
    card_infra = StoryCard(
        id="s:1",
        topic="Свет и вода",
        importance="high",
        category="utilities",
        tags=["жкх", "вода"],
        summary="Свет и вода",
    )
    card_mob = StoryCard(
        id="s:2",
        topic="Маршруты автобусов",
        importance="medium",
        category="transport",
        tags=["автобус"],
        summary="Маршруты автобусов",
    )
    card_comm = StoryCard(
        id="s:3",
        topic="Связь Юпитер",
        importance="medium",
        category="telecom",
        tags=["интернет"],
        summary="Связь Юпитер",
    )
    card_civic = StoryCard(
        id="s:4",
        topic="Выплаты и пенсии",
        importance="low",
        category="social",
        tags=["банк"],
        summary="Выплаты и пенсии",
    )
    card_life = StoryCard(
        id="s:5",
        topic="Выставка картин",
        importance="low",
        category="culture",
        tags=["музей"],
        summary="Выставка картин",
    )

    assert resolve_article_theme(card_infra, ()) == "infrastructure"
    assert resolve_article_theme(card_mob, ()) == "mobility"
    assert resolve_article_theme(card_comm, ()) == "communications"
    assert resolve_article_theme(card_civic, ()) == "civic_services"
    assert resolve_article_theme(card_life, ()) == "city_life"

    # Supports setup for fallback run:
    # Story 1 (infrastructure, rank 1, DEVELOP): two supports
    sup1_1 = _make_support(
        "s:1:sup:1",
        "s:1",
        "Авария на подстанции: обесточена нагорная часть города.",
        evidence_kind="established_fact",
    )
    sup1_2 = _make_support(
        "s:1:sup:2",
        "s:1",
        "Бригада РЭС ведет восстановительные работы.",
        evidence_kind="established_fact",
    )

    # Story 2 (mobility, rank 2, WEAVE): two community reports with identical fact to test dedup (9C) and attribution (9E)
    sup2_1 = _make_support(
        "s:2:sup:1",
        "s:2",
        "Автобус номер четыре курсирует примерно раз в час.",
        evidence_kind="community_report",
    )
    sup2_2 = _make_support(
        "s:2:sup:2",
        "s:2",
        "Автобус номер четыре курсирует примерно раз в час.",
        evidence_kind="community_report",
    )  # duplicate!
    sup2_3 = _make_support(
        "s:2:sup:3",
        "s:2",
        "Интервал движения маршруток увеличен.",
        evidence_kind="community_report",
    )

    # Story 3 (communications, rank 3, BRIEF):
    sup3_1 = _make_support(
        "s:3:sup:1",
        "s:3",
        "Житель запитал роутер от своего аккумулятора.",
        evidence_kind="community_report",
    )

    all_sups = (sup1_1, sup1_2, sup2_1, sup2_2, sup2_3, sup3_1)
    cards = (card_infra, card_mob, card_comm)
    context = ArticleEditorialContext(
        headline_candidates=("Авария на подстанции", "Движение автобусов"),
        support_index=all_sups,
        support_by_id={s.support_id: s for s in all_sups},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
        story_cards=cards,
    )

    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="s:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1_1.support_id, sup1_2.support_id),
            ),
            ArticleStoryCoverage(
                story_id="s:2",
                topic="Транспорт",
                rank=2,
                prominence="WEAVE",
                support_ids=(sup2_1.support_id, sup2_2.support_id, sup2_3.support_id),
            ),
            ArticleStoryCoverage(
                story_id="s:3",
                topic="Связь",
                rank=3,
                prominence="BRIEF",
                support_ids=(sup3_1.support_id,),
            ),
        )
    )

    composer = ArticleDeterministicComposer()
    draft = composer.render_full_fallback(context, plan, max_sections=5)

    # Test 9B: Sections ordered by min selection rank and <= max_sections
    section_headings = [sec.heading for sec in draft.sections]
    assert len(draft.sections) <= 5
    assert any(
        "инфраструктур" in h.lower() or "коммунал" in h.lower() or "жкх" in h.lower()
        for h in section_headings[:1]
    )

    # Test 9C: Exact conservative dedup: duplicate sentence is merged, and support IDs are unioned!
    mob_sec = next(
        sec
        for sec in draft.sections
        if any(w in sec.heading.lower() for w in ("транспорт", "маршрут", "мобильн"))
    )
    combined_mob_text = " ".join(p.text for p in mob_sec.paragraphs)
    assert combined_mob_text.lower().count("автобус номер четыре курсирует примерно раз в час") == 1
    mob_cited = {sid for p in mob_sec.paragraphs for sid in p.cited_support_ids}
    assert sup2_1.support_id in mob_cited
    assert sup2_2.support_id in mob_cited

    # Test 9D: Title and lead validate cleanly under validate_article_draft
    val_res = validate_article_draft(
        draft, context, PublicationEditorialConfig(article_min_words=5, article_min_sections=1)
    )
    blocking = [i for i in val_res.issues if i.blocking]
    assert val_res.is_valid is True, f"Validation failed with blocking issues: {blocking}"

    # Test 9E: No consecutive duplicate attribution phrases
    for sec in draft.sections:
        for p in sec.paragraphs:
            sentences = [s.strip() for s in p.text.split(".") if s.strip()]
            for i in range(len(sentences) - 1):
                s1_attr = sentences[i].startswith("По сообщениям жителей")
                s2_attr = sentences[i + 1].startswith("По сообщениям жителей")
                assert not (
                    s1_attr and s2_attr
                ), f"Consecutive duplicate attribution in paragraph: {p.text}"
