from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from src.city_context import CityContextResolver
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryAssignment,
    ArticleStoryCoverage,
    ArticleThematicSection,
)
from src.publication.article_material import (
    ArticleMaterialProjection,
    project_article_material,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality import (
    _patch_target_for_missing_story,
    diagnose_article_quality,
)


def _article_case(paragraphs: tuple[tuple[str, str], ...]):
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    supports = tuple(
        ArticleSupport(
            support_id=f"{story_id}:evidence:0:frag:{index}",
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref:{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=now,
            evidence_kind="community_report",
            story_id=story_id,
        )
        for index, (story_id, text) in enumerate(paragraphs, start=1)
    )
    stories = tuple(
        ArticleStoryCoverage(
            story_id=support.story_id,
            topic=support.text,
            rank=index,
            prominence="BRIEF",
            support_ids=(support.support_id,),
            detail_support_ids=(support.support_id,),
        )
        for index, support in enumerate(supports, start=1)
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(story.topic for story in stories),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    first_support = supports[0].support_id
    draft = StructuredArticleDraft(
        title="Городская хроника",
        title_support_ids=(first_support,),
        title_claims=(
            ArticleClaimAtom(text="Городская хроника", cited_support_ids=(first_support,)),
        ),
        lead="В городе продолжают происходить локальные изменения.",
        lead_support_ids=(first_support,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе продолжают происходить локальные изменения.",
                cited_support_ids=(first_support,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Повседневная обстановка",
                heading_support_ids=(first_support,),
                paragraphs=tuple(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=(f"{story_id}:evidence:0:frag:{index}",),
                        claims=(
                            ArticleClaimAtom(
                                text=text,
                                cited_support_ids=(f"{story_id}:evidence:0:frag:{index}",),
                            ),
                        ),
                    )
                    for index, (story_id, text) in enumerate(paragraphs, start=1)
                ),
            ),
        ),
    )
    return draft, ArticleCoveragePlan(stories=stories), context


def test_quality_report_flags_repeated_single_fact_paragraphs_as_article_inventory():
    draft, plan, context = _article_case(
        (
            ("story:1", "На улице Садовой нет света."),
            ("story:2", "На улице Морской подача воды ограничена."),
            ("story:3", "Автобус № 4 ходит раз в час."),
        )
    )

    report = diagnose_article_quality(draft, plan, context)

    assert report.needs_edit
    assert any(
        finding.code == "ARTICLE_INVENTORY_RHYTHM" and finding.unit_id.startswith("P")
        for finding in report.findings
    )


def test_quality_report_does_not_flag_one_short_paragraph_as_a_long_read_failure():
    draft, plan, context = _article_case(
        (("story:1", "На улице Садовой восстановили свет. В соседних домах проверяют напряжение."),)
    )

    report = diagnose_article_quality(draft, plan, context)

    assert not any(f.code == "ARTICLE_INVENTORY_RHYTHM" for f in report.findings)


def test_quality_report_flags_a_quote_roll_in_one_paragraph():
    draft, plan, context = _article_case(
        (("story:1", "Жители говорят: «нет света», «нет воды» и «нет связи»."),)
    )

    report = diagnose_article_quality(draft, plan, context)

    assert any(f.code == "QUOTE_ROLL_PARAGRAPH" for f in report.findings)


def test_quality_report_points_to_support_for_a_missing_develop_story():
    draft, plan, context = _article_case((("story:1", "На улице Садовой нет света."),))
    missing = ArticleSupport(
        support_id="story:2:evidence:0:frag:2",
        text="На улице Морской не было воды весь день.",
        source_text="На улице Морской не было воды весь день.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc),
        evidence_kind="community_report",
        story_id="story:2",
    )
    plan = ArticleCoveragePlan(
        stories=(
            *plan.stories,
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Вода на Морской",
                rank=2,
                prominence="DEVELOP",
                support_ids=(missing.support_id,),
            ),
        )
    )
    context = replace(
        context,
        support_index=(*context.support_index, missing),
        support_by_id={**context.support_by_id, missing.support_id: missing},
    )

    report = diagnose_article_quality(draft, plan, context)

    finding = next(f for f in report.findings if f.code == "MISSING_DEVELOP_STORY")
    assert finding.support_ids == (missing.support_id,)


def test_quality_report_only_flags_opposite_service_states_at_same_place_and_time():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Садовой подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )

    report = diagnose_article_quality(draft, plan, context)

    assert any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)
    assert sum(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings) == 1
    assert report.blocking_findings


def test_quality_report_checks_conflicts_even_without_coverage_stories():
    draft, _plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Садовой подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(support, effective_from=start, effective_until=end)
        for support in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )

    report = diagnose_article_quality(draft, ArticleCoveragePlan(stories=()), context)

    assert sum(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings) == 1


@pytest.mark.parametrize(
    ("first_location", "second_location"),
    [
        ("Центральной", "Карла Маркса"),
        ("Горбенко", "Лютеранской"),
    ],
)
def test_quality_report_resolves_street_aliases_from_the_edition_profile(
    first_location: str,
    second_location: str,
):
    draft, plan, context = _article_case(
        (
            ("story:water-a", f"На улице {first_location} подачу воды восстановили."),
            ("story:water-b", f"На улице {second_location} подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(support, effective_from=start, effective_until=end)
        for support in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    generic_edition_report = diagnose_article_quality(draft, plan, context)
    report = diagnose_article_quality(draft, plan, context, place_resolver=resolver)

    assert not any(
        finding.code == "CONTRADICTORY_SERVICE_STATE" for finding in generic_edition_report.findings
    )
    assert sum(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "На улице Садовой утром подачу воды восстановили.",
            "На улице Садовой вечером подача воды остаётся ограниченной.",
        ),
        (
            "На улице Садовой вчера подачу воды восстановили.",
            "На улице Садовой сегодня подача воды остаётся ограниченной.",
        ),
        (
            "На улице Садовой ранее подачу воды восстановили.",
            "На улице Садовой сейчас подача воды остаётся ограниченной.",
        ),
    ],
)
def test_quality_report_allows_explicit_temporal_variation(first: str, second: str):
    draft, plan, context = _article_case((("story:water-a", first), ("story:water-b", second)))
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )

    report = diagnose_article_quality(draft, plan, context)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_does_not_flag_different_streets_with_overlapping_times():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Морской подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )

    report = diagnose_article_quality(draft, plan, context)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_trusts_a_supported_local_contrast_in_the_actual_prose():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой, дом № 1, подачу воды восстановили."),
            ("story:water-b", "На улице Садовой, дом № 2, подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )
    first, _second = draft.sections[0].paragraphs
    contrasted = replace(
        draft,
        sections=(
            replace(
                draft.sections[0],
                paragraphs=(
                    replace(
                        first,
                        text="На улице Садовой, дом № 1, воду восстановили. На Садовой в доме № 2 подача остаётся ограниченной.",
                        cited_support_ids=(
                            "story:water-a:evidence:0:frag:1",
                            "story:water-b:evidence:0:frag:2",
                        ),
                        claims=(
                            replace(
                                first.claims[0],
                                text="На улице Садовой, дом № 1, воду восстановили.",
                                cited_support_ids=("story:water-a:evidence:0:frag:1",),
                            ),
                            ArticleClaimAtom(
                                text="На Садовой в доме № 2 подача остаётся ограниченной.",
                                cited_support_ids=("story:water-b:evidence:0:frag:2",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    report = diagnose_article_quality(contrasted, plan, context)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_does_not_trust_unsubstantiated_other_house_claim():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Садовой подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )
    first, second = draft.sections[0].paragraphs
    rewritten = replace(
        draft,
        sections=(
            replace(
                draft.sections[0],
                paragraphs=(
                    first,
                    replace(
                        second,
                        text="На улице Садовой в другом доме подача воды остаётся ограниченной.",
                        claims=(
                            replace(
                                second.claims[0],
                                text="На улице Садовой в другом доме подача воды остаётся ограниченной.",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    report = diagnose_article_quality(rewritten, plan, context)

    assert any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_ignores_suppressed_story_in_service_conflict_checks():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Садовой подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )
    suppressed_id = supports[1].support_id
    projection = ArticleMaterialProjection(
        text_by_support_id={support.support_id: support.text for support in supports},
        actions_by_support_id={
            supports[0].support_id: "KEEP",
            suppressed_id: "SUPPRESS_PROMOTION_ONLY",
        },
        reasons_by_support_id={
            supports[0].support_id: "supported_material_retained",
            suppressed_id: "high_confidence_promotion_only",
        },
        suppressed_story_ids=("story:water-b",),
    )

    report = diagnose_article_quality(draft, plan, context, material_projection=projection)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_does_not_block_generated_claim_atoms_for_different_houses():
    """Separate writer claim atoms may accurately distinguish supply points."""
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой в доме № 1 подачу воды восстановили."),
            ("story:water-b", "На улице Садовой в доме № 2 подача воды отсутствует."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )

    report = diagnose_article_quality(draft, plan, context)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_does_not_block_separate_claim_atoms_with_reported_variation():
    draft, plan, context = _article_case(
        (
            ("story:water-a", "На улице Садовой подачу воды восстановили."),
            ("story:water-b", "На улице Садовой подача воды остаётся ограниченной."),
        )
    )
    start = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    supports = tuple(
        replace(s, effective_from=start, effective_until=end) for s in context.support_index
    )
    context = replace(
        context,
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
    )
    first, second = draft.sections[0].paragraphs
    contrasted = replace(
        draft,
        sections=(
            replace(
                draft.sections[0],
                paragraphs=(
                    replace(
                        first,
                        text=(
                            "На улице Садовой подачу воды восстановили, тогда как "
                            "подача воды остаётся ограниченной."
                        ),
                        claims=(
                            replace(
                                first.claims[0], text="На улице Садовой подачу воды восстановили."
                            ),
                            replace(
                                second.claims[0],
                                text="На улице Садовой подача воды остаётся ограниченной.",
                            ),
                        ),
                        cited_support_ids=(
                            "story:water-a:evidence:0:frag:1",
                            "story:water-b:evidence:0:frag:2",
                        ),
                    ),
                ),
            ),
        ),
    )

    report = diagnose_article_quality(contrasted, plan, context)

    assert not any(f.code == "CONTRADICTORY_SERVICE_STATE" for f in report.findings)


def test_quality_report_emits_optional_detail_repair_for_covered_story():
    draft, plan, context = _article_case((("story:1", "На улице Садовой нет света."),))
    detail = replace(
        context.supports[0],
        support_id="story:1:evidence:0:frag:detail",
        text="Жильцы подключили генератор во дворе.",
        source_text="Жильцы подключили генератор во дворе.",
    )
    context = replace(
        context,
        support_index=(*context.support_index, detail),
        support_by_id={**context.support_by_id, detail.support_id: detail},
    )
    plan = replace(
        plan,
        stories=(replace(plan.stories[0], detail_support_ids=(detail.support_id,)),),
    )

    report = diagnose_article_quality(draft, plan, context)

    finding = next(f for f in report.findings if f.code == "MISSING_DETAIL_SUPPORT")
    assert finding.severity == "repair"
    assert finding.unit_id.startswith(("P", "L", "T"))
    assert finding.support_ids == (detail.support_id,)


def test_quality_report_does_not_request_a_detail_removed_by_material_projection():
    draft, plan, context = _article_case((("story:1", "На улице Садовой нет света."),))
    useful = context.supports[0]
    contact_only = ArticleSupport(
        support_id="story:1:evidence:0:frag:contact",
        text="Пишите в личку.",
        source_text="Пишите в личку.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:contact",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=useful.observed_at,
        evidence_kind="community_report",
        story_id="story:1",
    )
    context = replace(
        context,
        support_index=(*context.support_index, contact_only),
        support_by_id={
            **context.support_by_id,
            contact_only.support_id: contact_only,
        },
    )
    plan = replace(
        plan,
        stories=(
            replace(
                plan.stories[0],
                support_ids=(useful.support_id,),
                detail_support_ids=(contact_only.support_id,),
            ),
        ),
    )
    projection = project_article_material(context)
    assert projection.actions_by_support_id[contact_only.support_id] == "TRIM_DIRECTORY"
    assert not projection.text_by_support_id[contact_only.support_id]
    assert "story:1" not in projection.suppressed_story_ids

    report = diagnose_article_quality(draft, plan, context, material_projection=projection)

    assert not any(
        finding.code == "MISSING_DETAIL_SUPPORT" and contact_only.support_id in finding.support_ids
        for finding in report.findings
    )


def test_quality_report_does_not_request_unpatchable_empty_develop_material():
    draft, plan, context = _article_case((("story:existing", "На улице Садовой нет света."),))
    empty = ArticleSupport(
        support_id="story:empty:evidence:0:frag:2",
        text="Пишите в личку.",
        source_text="Пишите в личку.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:empty",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=context.supports[0].observed_at,
        evidence_kind="community_report",
        story_id="story:empty",
    )
    context = replace(
        context,
        support_index=(*context.support_index, empty),
        support_by_id={**context.support_by_id, empty.support_id: empty},
    )
    plan = replace(
        plan,
        stories=(
            *plan.stories,
            ArticleStoryCoverage(
                story_id="story:empty",
                topic="Сообщение без содержания после очистки",
                rank=2,
                prominence="DEVELOP",
                support_ids=(empty.support_id,),
            ),
        ),
    )
    projection = project_article_material(context)
    assert projection.actions_by_support_id[empty.support_id] == "TRIM_DIRECTORY"
    assert not projection.text_by_support_id[empty.support_id]
    assert "story:empty" not in projection.suppressed_story_ids

    report = diagnose_article_quality(draft, plan, context, material_projection=projection)

    assert not any(finding.code == "MISSING_DEVELOP_STORY" for finding in report.findings)


def test_missing_develop_target_never_points_to_empty_heading():
    draft, plan, _context = _article_case((("story:1", "На улице Садовой нет света."),))
    empty_section = ArticleSection(heading="Пустая глава", paragraphs=())
    draft = replace(draft, sections=(empty_section, draft.sections[0]))
    plan = replace(
        plan,
        sections=(
            # The matching draft heading exists but has no paragraph target.
            ArticleThematicSection(
                section_id="utilities",
                title="Пустая глава",
                lead_story_id="story:1",
                story_assignments=(
                    ArticleStoryAssignment(
                        story_id="story:1",
                        section_id="utilities",
                        depth="DEVELOP",
                        rank=1,
                        primary_evidence_ids=plan.stories[0].support_ids,
                    ),
                ),
                narrative_intent="develop the utility story",
            ),
        ),
    )

    target = _patch_target_for_missing_story(draft, plan, "story:1")

    assert target == "P001"


def test_missing_develop_target_uses_lead_when_draft_has_no_body_units():
    draft, plan, _context = _article_case((("story:1", "На улице Садовой нет света."),))
    draft = replace(draft, lead="", lead_support_ids=(), lead_claims=(), sections=())

    target = _patch_target_for_missing_story(draft, plan, "story:1")

    assert target == "LEAD"


def test_quality_report_excludes_only_explicitly_suppressed_story_ids():
    draft, plan, context = _article_case((("story:1", "На улице Садовой нет света."),))
    projection = type("Projection", (), {"suppressed_story_ids": ("story:2",)})()
    report = diagnose_article_quality(draft, plan, context, material_projection=projection)
    assert not any(f.code == "MISSING_DEVELOP_STORY" for f in report.findings)
