"""Unit tests for ArticleEditor targeted copy-editing and fact-checking."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_editor import ArticleEditor
from src.publication.article_material import ArticleMaterialProjection, project_article_material
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality import (
    ArticleReaderQualityFinding,
    ArticleReaderQualityReport,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
    validate_article_draft,
)

_NOW = dt.datetime(2026, 9, 5, 10, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def sample_context() -> ArticleEditorialContext:
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="По информации жителей города, на Восточном проспекте продолжаются работы по замене водовода.",
        source_text="По информации жителей города, на Восточном проспекте продолжаются работы по замене водовода.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("test_ch",),
        fragment_ids=(101,),
        source_item_ids=(101,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:1",
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:102",
        text="Движение автобусов по маршруту №4 осуществляется с интервалом около одного часа.",
        source_text="Движение автобусов по маршруту №4 осуществляется с интервалом около одного часа.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("test_ch",),
        fragment_ids=(102,),
        source_item_ids=(102,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:2",
    )
    supports = (sup1, sup2)
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("бердянск", "бердянске", "бердянска"),
        story_cards=(),
    )


@pytest.fixture
def sample_draft() -> StructuredArticleDraft:
    sup1_id = "story:1:evidence:0:frag:101"
    sup2_id = "story:2:evidence:0:frag:102"
    return StructuredArticleDraft(
        title="Городские работы и движение транспорта в Бердянске",
        title_support_ids=(sup1_id,),
        title_claims=(
            ArticleClaimAtom(text="Городские работы в Бердянске", cited_support_ids=(sup1_id,)),
        ),
        lead="В городе продолжаются инфраструктурные работы: «Мы всё починим к вечеру».",
        lead_support_ids=(sup1_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе продолжаются инфраструктурные работы", cited_support_ids=(sup1_id,)
            ),
        ),
        sections=(
            ArticleSection(
                heading="Водоснабжение на Восточном",
                heading_support_ids=(sup1_id,),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Водоснабжение на Восточном", cited_support_ids=(sup1_id,)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="По решению Минобразования на Восточном проспекте рабочие меняют трубы.",
                        cited_support_ids=(sup1_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="На Восточном проспекте меняют трубы",
                                cited_support_ids=(sup1_id,),
                            ),
                        ),
                    ),
                ),
            ),
            ArticleSection(
                heading="Городской транспорт",
                heading_support_ids=(sup2_id,),
                heading_claims=(
                    ArticleClaimAtom(text="Городской транспорт", cited_support_ids=(sup2_id,)),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Автобус №4 ходит примерно раз в час.",
                        cited_support_ids=(sup2_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Автобус №4 ходит раз в час", cited_support_ids=(sup2_id,)
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


@pytest.mark.unit
def test_apply_patches_preserves_structure_and_provenance(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    patches = {
        "LEAD": "На Восточном проспекте продолжаются работы по замене водовода.",
        "P001": "На Восточном проспекте коммунальные службы продолжают замену водовода.",
    }

    patched = editor.apply_patches(sample_draft, patches, context=sample_context)

    assert patched.title == sample_draft.title
    assert patched.lead == "На Восточном проспекте продолжаются работы по замене водовода."
    assert (
        patched.sections[0].paragraphs[0].text
        == "На Восточном проспекте коммунальные службы продолжают замену водовода."
    )
    # Provenance preserved:
    assert (
        patched.sections[0].paragraphs[0].cited_support_ids
        == sample_draft.sections[0].paragraphs[0].cited_support_ids
    )
    # Unpatched paragraph untouched:
    assert patched.sections[1].paragraphs[0].text == sample_draft.sections[1].paragraphs[0].text


@pytest.mark.unit
def test_apply_patches_updates_claim_atoms_for_multisentence_units(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    sup_id = sample_draft.lead_support_ids[0]

    # Create a draft where lead has 2 sentences and lead_claims has 2 atoms
    sent1 = "В городе продолжаются инфраструктурные работы."
    sent2 = "Отключение продлится до конца года по непроверенным данным."
    draft = StructuredArticleDraft(
        title=sample_draft.title,
        title_support_ids=sample_draft.title_support_ids,
        title_claims=sample_draft.title_claims,
        lead=f"{sent1} {sent2}",
        lead_support_ids=sample_draft.lead_support_ids,
        lead_claims=(
            ArticleClaimAtom(text=sent1, cited_support_ids=(sup_id,)),
            ArticleClaimAtom(text=sent2, cited_support_ids=(sup_id,)),
        ),
        sections=sample_draft.sections,
        cited_evidence_ids=sample_draft.cited_evidence_ids,
        word_count=sample_draft.word_count,
    )

    clean_lead = "На Восточном проспекте продолжаются работы по замене водовода."
    patched = editor.apply_patches(draft, {"LEAD": clean_lead}, context=sample_context)

    assert patched.lead == clean_lead
    # Ensure the old invalid sentence atom was removed and new atoms are built from clean_lead
    assert not any("конца года" in c.text for c in patched.lead_claims)
    assert any("замене водовода" in c.text for c in patched.lead_claims)
    assert all(c.cited_support_ids == draft.lead_support_ids for c in patched.lead_claims)


@pytest.mark.unit
@pytest.mark.parametrize("unit_id", ["TITLE", "LEAD", "P002"])
def test_failed_strict_regrounding_rejects_replacement_patch(
    unit_id: str,
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    """One shared number cannot rescue unsupported replacement prose."""
    support_id = sample_context.supports[1].support_id
    original_paragraph = sample_draft.sections[1].paragraphs[0]
    draft = replace(
        sample_draft,
        title="Автобус №4 ходит раз в час",
        title_support_ids=(support_id,),
        title_claims=(
            ArticleClaimAtom(text="Автобус №4 ходит раз в час", cited_support_ids=(support_id,)),
        ),
        lead="Автобус №4 ходит раз в час.",
        lead_support_ids=(support_id,),
        lead_claims=(
            ArticleClaimAtom(text="Автобус №4 ходит раз в час.", cited_support_ids=(support_id,)),
        ),
    )
    patch = "Авария произошла в 4:00."

    edited = ArticleEditor(provider=AsyncMock(), model="test-model").apply_patches(
        draft,
        {unit_id: patch},
        context=sample_context,
    )

    if unit_id == "TITLE":
        assert edited.title == draft.title
        assert edited.title_support_ids == draft.title_support_ids
        assert edited.title_claims == draft.title_claims
    elif unit_id == "LEAD":
        assert edited.lead == draft.lead
        assert edited.lead_support_ids == draft.lead_support_ids
        assert edited.lead_claims == draft.lead_claims
    else:
        assert edited.sections[1].paragraphs[0] is original_paragraph
        assert edited.sections[1].paragraphs[0].text == original_paragraph.text
        assert edited.sections[1].paragraphs[0].cited_support_ids == (
            original_paragraph.cited_support_ids
        )
        assert edited.sections[1].paragraphs[0].claims == original_paragraph.claims


@pytest.mark.unit
def test_contextless_patch_cannot_retain_old_support_ids(
    sample_draft: StructuredArticleDraft,
) -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    edited = editor.apply_patches(
        sample_draft,
        {
            "TITLE": "Неподтвержденный заголовок",
            "LEAD": "Неподтвержденный лид.",
            "P001": "Неподтвержденный абзац.",
        },
        context=None,
    )

    assert edited.title == sample_draft.title
    assert edited.title_support_ids == sample_draft.title_support_ids
    assert edited.title_claims == sample_draft.title_claims
    assert edited.lead == sample_draft.lead
    assert edited.lead_support_ids == sample_draft.lead_support_ids
    assert edited.lead_claims == sample_draft.lead_claims
    assert edited.sections[0].paragraphs[0] is sample_draft.sections[0].paragraphs[0]


@pytest.mark.unit
def test_unpatched_paragraph_preserves_distinct_unit_and_claim_supports(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    water_support, transport_support = (support.support_id for support in sample_context.supports)
    untouched = replace(
        sample_draft.sections[1].paragraphs[0],
        cited_support_ids=(water_support,),
        claims=(
            ArticleClaimAtom(
                text="Автобус №4 ходит раз в час.", cited_support_ids=(transport_support,)
            ),
        ),
    )
    draft = replace(
        sample_draft,
        sections=(
            sample_draft.sections[0],
            replace(sample_draft.sections[1], paragraphs=(untouched,)),
        ),
    )

    edited = ArticleEditor(provider=AsyncMock(), model="test-model").apply_patches(
        draft,
        {"P001": "На Восточном проспекте меняют водовод."},
        context=sample_context,
    )

    preserved = edited.sections[1].paragraphs[0]
    assert preserved is untouched
    assert preserved.cited_support_ids == (water_support,)
    assert preserved.claims == untouched.claims
    assert preserved.claims[0].cited_support_ids == (transport_support,)


@pytest.mark.unit
def test_parse_editor_response_handles_various_formats() -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")

    # 1. Plain json object with "units"
    res1 = json.dumps({"units": {"LEAD": "Текст лида", "P002": "Текст второго абзаца"}})
    assert editor._parse_editor_response(res1) == {
        "LEAD": "Текст лида",
        "P002": "Текст второго абзаца",
    }

    # 2. Markdown wrapped
    res2 = f"```json\n{res1}\n```"
    assert editor._parse_editor_response(res2) == {
        "LEAD": "Текст лида",
        "P002": "Текст второго абзаца",
    }

    # 3. Direct mapping without "units" key
    res3 = json.dumps({"P001": "Текст абзаца 1"})
    assert editor._parse_editor_response(res3) == {"P001": "Текст абзаца 1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_editor_resolves_validation_issues(
    sample_draft: StructuredArticleDraft, sample_context: ArticleEditorialContext
) -> None:
    mock_provider = AsyncMock()
    # Mock LLM editor response that cleans quotes and genericizes names
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "LEAD": "В городе продолжаются работы, на Восточном проспекте специалисты продолжают замену водовода.",
                "P001": "По информации жителей, на Восточном проспекте рабочие продолжают замену водовода.",
            }
        }
    )

    editor = ArticleEditor(provider=mock_provider, model="test-model")

    from src.config_loader import PublicationEditorialConfig

    test_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    # Initial draft has issues in LEAD and P001
    support_id = sample_draft.sections[0].paragraphs[0].cited_support_ids[0]
    sample_draft = replace(
        sample_draft,
        title="Замена водовода на Восточном проспекте",
        title_claims=(
            ArticleClaimAtom(
                text="Замена водовода на Восточном проспекте",
                cited_support_ids=(support_id,),
            ),
        ),
    )
    val_res = validate_article_draft(sample_draft, sample_context, config=test_config)
    val_res = replace(
        val_res,
        is_valid=False,
        issues=(
            *val_res.issues,
            ArticleValidationIssue(
                code="UNSUPPORTED_PROPER_NAME",
                unit_id="P001",
                message="Удалите неподтвержденное название.",
                support_ids=sample_draft.sections[0].paragraphs[0].cited_support_ids,
                blocking=True,
            ),
        ),
    )

    edited_draft, edited_val = await editor.edit_draft(
        sample_draft,
        val_res,
        sample_context,
        config=test_config,
        max_attempts=1,
    )

    assert mock_provider.chat_completion.call_count == 1
    assert "«" not in edited_draft.lead
    assert "Минобразования" not in edited_draft.sections[0].paragraphs[0].text
    assert edited_val.is_valid


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_editor_repairs_quality_only_and_revalidates_facts(
    sample_context: ArticleEditorialContext,
) -> None:
    support = sample_context.supports[0]
    draft = StructuredArticleDraft(
        title=support.text,
        title_support_ids=(support.support_id,),
        title_claims=(
            ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),
        ),
        lead=support.text,
        lead_support_ids=(support.support_id,),
        lead_claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),),
        sections=(
            ArticleSection(
                heading="Городские работы",
                heading_support_ids=(support.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=support.text,
                        cited_support_ids=(support.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text=support.text, cited_support_ids=(support.support_id,)
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    from src.config_loader import PublicationEditorialConfig

    config = PublicationEditorialConfig(article_min_sections=1, article_min_words=1)
    validation = validate_article_draft(draft, sample_context, config)
    assert validation.is_valid
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id=support.story_id,
                topic="Работы на водоводе",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support.support_id,),
            ),
        )
    )
    quality = ArticleReaderQualityReport(
        findings=(
            ArticleReaderQualityFinding(
                code="QUOTE_ROLL_PARAGRAPH",
                unit_id="P001",
                message="Свяжите соседние сообщения.",
                support_ids=(support.support_id,),
                severity="repair",
            ),
        )
    )
    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "P001": "По информации жителей, на Восточном проспекте продолжаются работы по замене водовода."
            }
        }
    )

    edited_draft, edited_val = await ArticleEditor(provider, "test-model").edit_draft(
        draft,
        validation,
        sample_context,
        config=config,
        max_attempts=1,
        quality_report=quality,
        coverage_plan=plan,
        material_projection=project_article_material(sample_context),
    )

    assert provider.chat_completion.call_count == 1
    assert edited_val.is_valid
    assert "замене водовода" in edited_draft.sections[0].paragraphs[0].text
    assert edited_draft.lead == draft.lead


@pytest.mark.unit
def test_quality_support_ids_are_prioritized_and_suppressed_projection_is_hidden(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    existing = tuple(
        replace(
            sample_context.supports[0],
            support_id=f"story:existing:evidence:0:frag:{index}",
            text=f"Подтверждённая деталь {index}.",
            source_text=f"Подтверждённая деталь {index}.",
            story_id="story:existing",
        )
        for index in range(1, 7)
    )
    required = replace(
        sample_context.supports[0],
        support_id="story:required:evidence:0:frag:99",
        text="Нужная деталь о насосе.",
        source_text="Нужная деталь о насосе.",
        story_id="story:required",
    )
    context = replace(
        sample_context,
        support_index=existing + (required,),
        support_by_id={support.support_id: support for support in existing + (required,)},
    )
    draft = replace(
        sample_draft,
        sections=(
            replace(
                sample_draft.sections[0],
                paragraphs=(
                    replace(
                        sample_draft.sections[0].paragraphs[0],
                        cited_support_ids=tuple(s.support_id for s in existing),
                    ),
                ),
            ),
            *sample_draft.sections[1:],
        ),
    )
    finding = ArticleReaderQualityFinding(
        code="MISSING_DEVELOP_STORY",
        unit_id="P001",
        message="Восстановите ключевую деталь.",
        support_ids=(required.support_id,),
        severity="blocking",
    )
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    units = editor._build_unit_contexts(
        draft,
        {"P001": [finding]},
        context,
        material_projection=ArticleMaterialProjection(
            text_by_support_id={required.support_id: required.text},
            actions_by_support_id={
                required.support_id: "KEEP",
                existing[0].support_id: "SUPPRESS_PROMOTION_ONLY",
            },
            reasons_by_support_id={
                required.support_id: "supported_material_retained",
                existing[0].support_id: "high_confidence_promotion_only",
            },
        ),
    )

    assert units[0]["support_ids"][0] == required.support_id
    assert units[0]["supports"][0] == "Нужная деталь о насосе."
    prompt = editor._build_user_prompt(units)
    # Required evidence comes before six existing citations, so it is visible
    # inside the bounded five-support prompt slice, while suppressed material
    # is never projected to the editor.
    assert "Нужная деталь о насосе." in prompt
    assert "Подтверждённая деталь 1." not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editor_patch_cannot_reattach_suppressed_support(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    suppressed_id = sample_draft.sections[0].paragraphs[0].cited_support_ids[0]
    projection = ArticleMaterialProjection(
        text_by_support_id={suppressed_id: "Исходная деталь, удалённая из промо-проекции."},
        actions_by_support_id={suppressed_id: "SUPPRESS_PROMOTION_ONLY"},
        reasons_by_support_id={suppressed_id: "promotion_only"},
        suppressed_story_ids=("story:1",),
    )
    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps(
        {"units": {"P001": "Восстановили подачу по прежнему объявлению."}}
    )
    initial_validation = ArticleValidationResult(
        is_valid=False,
        word_count=20,
        section_count=2,
        issues=(
            ArticleValidationIssue(
                code="UNSUPPORTED_CONCRETE_CLAIM",
                unit_id="P001",
                message="Исправьте абзац.",
                support_ids=(suppressed_id,),
                blocking=True,
            ),
        ),
    )

    edited, _ = await ArticleEditor(provider, "test-model").edit_draft(
        sample_draft,
        initial_validation,
        sample_context,
        max_attempts=1,
        material_projection=projection,
    )

    # The patched unit has no citable material in the projected context, so it
    # is dropped instead of inheriting the old suppressed citation.
    assert all(
        suppressed_id not in paragraph.cited_support_ids
        for section in edited.sections
        for paragraph in section.paragraphs
        if paragraph.text == "Восстановили подачу по прежнему объявлению."
    )


@pytest.mark.unit
def test_article_editor_paragraph_deletion(
    sample_draft: StructuredArticleDraft,
) -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    # Explicit deletion is honored; the paragraph in the next section remains exact.
    edited = editor.apply_patches(sample_draft, {"P001": "[DELETE]"})
    assert len(edited.sections) == 1
    assert edited.sections[0].paragraphs[0] is sample_draft.sections[1].paragraphs[0]


@pytest.mark.unit
def test_editor_context_includes_neighbors_without_their_support_ids(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    support_1, support_2 = (support.support_id for support in sample_context.supports)
    paragraphs = (
        ArticleParagraph("Предыдущий абзац про водовод.", (support_1,)),
        ArticleParagraph("Целевой абзац про транспорт.", (support_2,)),
        ArticleParagraph("Следующий абзац про водовод.", (support_1,)),
    )
    draft = replace(
        sample_draft,
        sections=(replace(sample_draft.sections[0], paragraphs=paragraphs),),
    )
    finding = ArticleReaderQualityFinding(
        code="OVERLOADED_ROSTER_PARAGRAPH",
        unit_id="P002",
        message="Сгруппируйте адреса.",
        support_ids=(support_2,),
        severity="blocking",
    )

    editor = ArticleEditor(AsyncMock(), "test-model")
    units = editor._build_unit_contexts(draft, {"P002": [finding]}, sample_context)
    prompt = editor._build_user_prompt(units)

    assert len(units) == 1
    assert units[0]["support_ids"] == [support_2]
    assert units[0]["reader_context"] == {
        "section_heading": "Водоснабжение на Восточном",
        "previous_paragraph": "Предыдущий абзац про водовод.",
        "next_paragraph": "Следующий абзац про водовод.",
    }
    assert "Неизменяемый контекст" in prompt
    assert "Предыдущий абзац про водовод." in prompt
    assert "Следующий абзац про водовод." in prompt
    assert support_1 not in units[0]["support_ids"]


@pytest.mark.unit
def test_heading_editor_context_includes_comparison_headings_without_new_targets(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    target_support = sample_context.supports[0].support_id
    finding = ArticleReaderQualityFinding(
        code="DUPLICATE_ARTICLE_HEADING",
        unit_id="H001",
        message="Заголовок повторяет название статьи.",
        support_ids=(target_support,),
        severity="blocking",
    )

    editor = ArticleEditor(AsyncMock(), "test-model")
    units = editor._build_unit_contexts(sample_draft, {"H001": [finding]}, sample_context)
    prompt = editor._build_user_prompt(units)

    assert len(units) == 1
    assert units[0]["unit_id"] == "H001"
    assert units[0]["support_ids"] == [target_support]
    assert units[0]["reader_context"] == {
        "article_title": sample_draft.title,
        "other_section_headings": (sample_draft.sections[1].heading,),
    }
    assert "Заголовок статьи: Городские работы и движение транспорта в Бердянске" in prompt
    assert "Заголовок другой главы: Городской транспорт" in prompt
    assert "Неизменяемый контекст" in prompt
    assert "DUPLICATE_ARTICLE_HEADING" in prompt
    assert target_support not in units[0]["reader_context"].values()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected_instruction"),
    (
        ("OVERLOADED_ROSTER_PARAGRAPH", "поддержанные исключения"),
        ("CROSS_SECTION_REPETITION", "лучше всего подтверждено"),
        ("DUPLICATE_ARTICLE_HEADING", "только этот заголовок"),
        ("UNDEVELOPED_LEAD_PROMISE", "исправьте лид"),
        ("ARTICLE_INVENTORY_RHYTHM", "естественным переходом"),
        ("QUOTE_ROLL_PARAGRAPH", "косвенной речи"),
        ("CONTRADICTORY_SERVICE_STATE", "явный контраст"),
    ),
)
def test_reader_quality_codes_get_specific_repair_instructions(
    code: str,
    expected_instruction: str,
) -> None:
    prompt = ArticleEditor(AsyncMock(), "test-model")._build_user_prompt(
        [
            {
                "unit_id": "P001",
                "unit_type": "paragraph",
                "text": "Текст фрагмента.",
                "support_ids": [],
                "supports": [],
                "issues": [
                    ArticleReaderQualityFinding(
                        code=code,
                        unit_id="P001",
                        message="Исправьте фрагмент.",
                        severity="blocking",
                    )
                ],
            }
        ]
    )

    assert expected_instruction in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editor_ignores_unrequested_patch_ids_and_restricts_regrounding(
    sample_context: ArticleEditorialContext,
    sample_draft: StructuredArticleDraft,
) -> None:
    target_support = sample_draft.sections[0].paragraphs[0].cited_support_ids[0]
    unrelated_support = sample_draft.sections[1].paragraphs[0].cited_support_ids[0]
    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "P001": "Пожилая женщина не смогла найти нужный адрес.",
                "LEAD": "Незапрошенная правка лида.",
                "H002": "Незапрошенный заголовок.",
            }
        }
    )
    validation = ArticleValidationResult(
        is_valid=False,
        word_count=30,
        section_count=2,
        issues=(
            ArticleValidationIssue(
                code="UNSUPPORTED_CONCRETE_CLAIM",
                unit_id="P001",
                message="Проверьте абзац.",
                support_ids=(target_support,),
                blocking=True,
            ),
        ),
    )

    edited, _ = await ArticleEditor(provider, "test-model").edit_draft(
        sample_draft,
        validation,
        sample_context,
        max_attempts=1,
    )

    assert edited.lead == sample_draft.lead
    surviving_paragraphs = [
        paragraph for section in edited.sections for paragraph in section.paragraphs
    ]
    assert all(
        paragraph.text != "Пожилая женщина не смогла найти нужный адрес."
        for paragraph in surviving_paragraphs
    )
    # The editor rejects an ungrounded replacement and retains the original
    # invalid unit so final validation can still fail closed.
    assert any(
        paragraph is sample_draft.sections[0].paragraphs[0]
        and target_support in paragraph.cited_support_ids
        for paragraph in surviving_paragraphs
    )
    assert any(
        paragraph.text == sample_draft.sections[1].paragraphs[0].text
        and unrelated_support in paragraph.cited_support_ids
        for paragraph in surviving_paragraphs
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editor_revalidates_factual_claims_added_to_quality_patch(
    sample_context: ArticleEditorialContext,
) -> None:
    support = sample_context.supports[0]
    paragraph = ArticleParagraph(
        text=support.text,
        cited_support_ids=(support.support_id,),
        claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),),
    )
    draft = StructuredArticleDraft(
        title="Работы на водоводе",
        title_support_ids=(support.support_id,),
        title_claims=(
            ArticleClaimAtom(text="Работы на водоводе", cited_support_ids=(support.support_id,)),
        ),
        lead=support.text,
        lead_support_ids=(support.support_id,),
        lead_claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),),
        sections=(
            ArticleSection(
                heading="Водоснабжение",
                heading_support_ids=(support.support_id,),
                paragraphs=(paragraph,),
            ),
        ),
    )
    config = pytest.importorskip("src.config_loader").PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=1,
    )
    initial_validation = validate_article_draft(draft, sample_context, config=config)
    assert initial_validation.is_valid
    finding = ArticleReaderQualityFinding(
        code="OVERLOADED_ROSTER_PARAGRAPH",
        unit_id="P001",
        message="Сгруппируйте адреса.",
        support_ids=(support.support_id,),
        severity="blocking",
    )
    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "P001": (
                    "На Восточном проспекте произошла авария на водоводе, "
                    "подачу воды восстановят через 19 часов."
                )
            }
        }
    )

    _, edited_validation = await ArticleEditor(provider, "test-model").edit_draft(
        draft,
        initial_validation,
        sample_context,
        config=config,
        max_attempts=1,
        quality_report=ArticleReaderQualityReport(findings=(finding,)),
    )

    assert not edited_validation.is_valid
    assert edited_validation.issues
    assert any(issue.blocking for issue in edited_validation.issues)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quality_diagnostics_are_recomputed_after_each_patch(
    monkeypatch: pytest.MonkeyPatch,
    sample_context: ArticleEditorialContext,
) -> None:
    support = sample_context.supports[0]
    draft = StructuredArticleDraft(
        title="Работы на водоводе",
        title_support_ids=(support.support_id,),
        title_claims=(
            ArticleClaimAtom(text="Работы на водоводе", cited_support_ids=(support.support_id,)),
        ),
        lead=support.text,
        lead_support_ids=(support.support_id,),
        lead_claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),),
        sections=(
            ArticleSection(
                heading="Водоснабжение",
                heading_support_ids=(support.support_id,),
                paragraphs=(ArticleParagraph(support.text, (support.support_id,)),),
            ),
        ),
    )
    from src.config_loader import PublicationEditorialConfig

    config = PublicationEditorialConfig(article_min_sections=1, article_min_words=1)
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id=support.story_id,
                topic="Работы на водоводе",
                rank=1,
                prominence="BRIEF",
                support_ids=(support.support_id,),
            ),
        )
    )
    finding = ArticleReaderQualityFinding(
        code="OVERLOADED_ROSTER_PARAGRAPH",
        unit_id="P001",
        message="Сгруппируйте адреса.",
        support_ids=(support.support_id,),
        severity="blocking",
    )
    validation = validate_article_draft(draft, sample_context, config=config)
    provider = AsyncMock()
    provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "P001": "По сообщениям жителей, на Восточном проспекте продолжаются работы по замене водовода."
            }
        }
    )
    quality_calls: list[str] = []

    def recompute_quality(edited_draft, *args, **kwargs):
        quality_calls.append(edited_draft.sections[0].paragraphs[0].text)
        return ArticleReaderQualityReport()

    monkeypatch.setattr(
        "src.publication.article_editor.diagnose_article_quality", recompute_quality
    )
    editor = ArticleEditor(provider, "test-model")

    edited, edited_validation = await editor.edit_draft(
        draft,
        validation,
        sample_context,
        config=config,
        max_attempts=2,
        quality_report=ArticleReaderQualityReport(findings=(finding,)),
        coverage_plan=plan,
    )

    assert edited_validation.is_valid
    assert len(quality_calls) == 1
    assert provider.chat_completion.call_count == 1
    assert editor.last_patched_unit_ids == ("P001",)
    assert edited.sections[0].paragraphs[0].text.startswith("По сообщениям жителей")
