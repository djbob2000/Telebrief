"""Tests for fail-closed article generation policy and claim boundary fixes."""

from __future__ import annotations

import datetime as dt

import pytest

from src.article_generator import _ground_draft_in_coverage_plan
from src.config_loader import PublicationEditorialConfig
from src.publication.article_claims import (
    extract_concrete_claims,
    find_unsupported_claims,
    normalize_support_text,
)
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_editor import ArticleEditor
from src.publication.article_finalization import ArticleFinalizer
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_semantic_support import assess_semantic_support
from src.publication.article_validator import ArticleValidationIssue
from src.publication.errors import ArticlePublicationRejected
from tests.publication.test_article_recovery import RecordingAttemptObserver

_NOW = dt.datetime(2026, 9, 3, 19, 30, tzinfo=dt.timezone.utc)


@pytest.mark.unit
def test_grounding_does_not_assign_unrelated_develop_support_to_paragraph() -> None:
    """An unrelated sentence must not inherit the first DEVELOP story as fake provenance."""
    support_id = "story:1:evidence:0:frag:101"
    support = ArticleSupport(
        support_id=support_id,
        text="У провайдера интернет работает в полном объёме.",
        source_text="У провайдера интернет есть, он работает.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Интернет",),
        support_index=(support,),
        support_by_id={support_id: support},
        recurring_topics=(),
        edition_name="Бердянск",
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Интернет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support_id,),
            ),
        )
    )
    parsed = {
        "title": "Городская хроника",
        "lead": "",
        "sections": [
            {
                "heading": "Городская хроника",
                "paragraphs": [
                    {
                        "text": "Пожилая женщина не могла вспомнить свой адрес на остановке у рынка.",
                        "cited_support_ids": [support_id],
                        "claims": [
                            {
                                "text": "Пожилая женщина не могла вспомнить свой адрес на остановке у рынка.",
                                "cited_support_ids": [support_id],
                            }
                        ],
                    },
                ],
            }
        ],
    }

    grounded = _ground_draft_in_coverage_plan(parsed, plan, context)

    grounded_paragraph = grounded["sections"][0]["paragraphs"][0]
    assert grounded_paragraph["cited_support_ids"] == []
    assert grounded_paragraph["claims"][0]["cited_support_ids"] == []


@pytest.mark.unit
def test_editor_patch_does_not_preserve_old_support_for_unrelated_text() -> None:
    """Replacing a paragraph must invalidate its old provenance until re-grounded."""
    support_id = "story:1:evidence:0:frag:101"
    support = ArticleSupport(
        support_id=support_id,
        text="У провайдера интернет работает в полном объёме.",
        source_text="У провайдера интернет есть, он работает.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Интернет",),
        support_index=(support,),
        support_by_id={support_id: support},
        recurring_topics=(),
    )
    draft = StructuredArticleDraft(
        title="Интернет",
        title_support_ids=(support_id,),
        lead="Интернет работает.",
        lead_support_ids=(support_id,),
        sections=(
            ArticleSection(
                heading="Связь",
                heading_support_ids=(support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="Интернет работает.",
                        cited_support_ids=(support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Интернет работает.",
                                cited_support_ids=(support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    editor = ArticleEditor(provider=object(), model="test")  # type: ignore[arg-type]

    edited = editor.apply_patches(
        draft,
        {"P001": "Пожилая женщина не могла вспомнить свой адрес на остановке."},
        context=context,
    )

    assert not edited.sections


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_revalidates_draft_after_structural_finalization(monkeypatch) -> None:
    """The exact draft rendered to the publication must be validated after final mutations."""
    from src.publication import article_finalization as finalization

    support_id = "story:1:evidence:0:frag:101"
    support = ArticleSupport(
        support_id=support_id,
        text="В микрорайоне восстановили подачу электроэнергии.",
        source_text="В микрорайоне восстановили подачу электроэнергии.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=(support,),
        support_by_id={support_id: support},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support_id,),
            ),
        )
    )
    draft = StructuredArticleDraft(
        title="Восстановление электроснабжения",
        title_support_ids=(support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Восстановление электроснабжения",
                cited_support_ids=(support_id,),
            ),
        ),
        lead="В микрорайоне восстановили подачу электроэнергии.",
        lead_support_ids=(support_id,),
        lead_claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support_id,)),),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=support.text,
                        cited_support_ids=(support_id,),
                        claims=(
                            ArticleClaimAtom(text=support.text, cited_support_ids=(support_id,)),
                        ),
                    ),
                ),
            ),
        ),
    )

    def inject_unsupported_final_text(current):
        section = current.sections[0]
        bad = ArticleParagraph(
            text="Пожилая женщина не могла вспомнить свой адрес на остановке у рынка.",
            cited_support_ids=(support_id,),
            claims=(
                ArticleClaimAtom(
                    text="Пожилая женщина не могла вспомнить свой адрес на остановке у рынка.",
                    cited_support_ids=(support_id,),
                ),
            ),
        )
        return StructuredArticleDraft(
            title=current.title,
            title_support_ids=current.title_support_ids,
            title_claims=current.title_claims,
            lead=current.lead,
            lead_support_ids=current.lead_support_ids,
            lead_claims=current.lead_claims,
            sections=(
                ArticleSection(
                    heading=section.heading,
                    heading_support_ids=section.heading_support_ids,
                    heading_claims=section.heading_claims,
                    paragraphs=section.paragraphs + (bad,),
                ),
            ),
        )

    monkeypatch.setattr(finalization, "_merge_orphan_paragraphs", inject_unsupported_final_text)

    with pytest.raises(ArticlePublicationRejected):
        await ArticleFinalizer().finalize(
            writer_draft=draft,
            writer_error=None,
            writer_attempt_id=0,
            context=context,
            coverage_plan=plan,
            editorial_config=PublicationEditorialConfig(
                article_min_words=5,
                article_min_sections=1,
                article_allow_deterministic_fallback=False,
            ),
            length_profile=None,
        )


@pytest.mark.unit
def test_article_editor_receives_sanitized_primary_source_for_repair() -> None:
    """Targeted repair must see primary evidence, without leaking contact payload."""
    support_id = "story:1:evidence:0:frag:1"
    support = ArticleSupport(
        support_id=support_id,
        text="Работает пункт помощи.",
        source_text="Пункт помощи работает на улице Победы, 43. Телефон +79991234567.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Пункт помощи",),
        support_index=(support,),
        support_by_id={support_id: support},
        recurring_topics=(),
    )
    draft = StructuredArticleDraft(
        title="Пункт помощи",
        title_support_ids=(support_id,),
        lead="Пункт помощи работает.",
        lead_support_ids=(support_id,),
        sections=(
            ArticleSection(
                heading="Помощь жителям",
                heading_support_ids=(support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="Пункт помощи работает.",
                        cited_support_ids=(support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Пункт помощи работает.",
                                cited_support_ids=(support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    issue = ArticleValidationIssue(
        code="UNSUPPORTED_CLAIM_ATOM",
        unit_id="P001",
        message="test issue",
    )
    editor = ArticleEditor(provider=object(), model="test")  # type: ignore[arg-type]

    units = editor._build_unit_contexts(draft, {"P001": [issue]}, context)

    assert units[0]["supports"] == [
        "Работает пункт помощи.\n"
        "Первичный источник: Пункт помощи работает на улице Победы, 43. "
        "Телефон [contact omitted]."
    ]


@pytest.mark.unit
def test_proper_name_extraction_after_ellipsis_quote() -> None:
    """Words following quote ending with ellipsis (…) must not be misclassified as proper names."""
    text = (
        "На этом фоне звучит сдержанный оптимизм: "
        "«Пока впереди День города, есть повод для надежды. Наверное…» "
        "Ситуацию дополняют бытовые сложности водоканала."
    )
    supports = [
        "Пока впереди День города, есть повод для надежды. Наверное",
        "водоканал проводит ремонтные работы",
    ]
    signals = assess_semantic_support(text, supports, allowed_context_terms=("День города",))
    assert "ситуацию" not in signals.blocking_proper_names
    assert "ситуацию" not in signals.unmatched_proper_names


@pytest.mark.unit
def test_number_k_shorthand_normalization() -> None:
    """Sources with 3к / 3k / 3 тыс. correctly support written 3000 рублей."""
    support_text = "в больнице платите 3к± И в ПНД было 1400 по-моему"
    norm = normalize_support_text(support_text)
    assert "3000" in norm
    assert "1400" in norm

    text = "Стоимость медкомиссии жители оценивают примерно в 3000 рублей в больнице и около 1400 в ПНД."
    unsupported = find_unsupported_claims(text, [support_text])
    assert len(unsupported) == 0


@pytest.mark.unit
def test_date_does_not_extract_spurious_standalone_number() -> None:
    """Dates like '2 сентября' must not extract standalone number '2'."""
    text = "Вечер 2 сентября и ночь на 3 сентября жители встретили под звуки взрывов."
    claims = extract_concrete_claims(text)
    number_claims = [c for c in claims if c.kind == "number"]
    date_claims = [c for c in claims if c.kind == "date"]

    assert len(date_claims) == 2
    assert {c.raw for c in date_claims} == {"2 сентября", "3 сентября"}
    # The digits 2 and 3 inside the dates must NOT be emitted as naked quantity numbers
    assert len(number_claims) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_writer_failure_fails_closed_when_fallback_disabled() -> None:
    """When article_allow_deterministic_fallback=False, writer failure raises ArticlePublicationRejected."""
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Факт.",
        source_text="Факт.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Тема",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Тема",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup.support_id,),
                detail_support_ids=(),
            ),
        )
    )

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=None,
            writer_error=RuntimeError("AI model timeout"),
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    assert exc_info.value.reason == "writer_failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_invalid_draft_fails_closed_when_fallback_disabled() -> None:
    """When article_allow_deterministic_fallback=False, invalid validation raises ArticlePublicationRejected."""
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В городе ведутся работы.",
        source_text="В городе ведутся работы.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Тема",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Тема",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup.support_id,),
                detail_support_ids=(),
            ),
        )
    )

    # Draft with completely unsupported invented fact
    invalid_draft = StructuredArticleDraft(
        title="Заголовок [story:1]",
        title_support_ids=(sup.support_id,),
        lead="Лид с выдуманным городом Париж и номером +79991234567.",
        lead_support_ids=(sup.support_id,),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=(sup.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Париже зафиксированы аномалии.",
                        cited_support_ids=(sup.support_id,),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=invalid_draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    assert exc_info.value.reason == "validation_failed"
