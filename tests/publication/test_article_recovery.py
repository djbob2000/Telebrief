"""Unit and acceptance tests for Event-First article deterministic recovery and finalizer."""

from __future__ import annotations

import datetime as dt

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
