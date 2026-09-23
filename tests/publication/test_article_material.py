from __future__ import annotations

from dataclasses import replace

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_material import (
    ArticleMaterialProjection,
    materialize_article_validation_context,
    project_article_material,
)


def make_support(story_id, evidence_kind, text, source_text):
    support_id = f"{story_id}:evidence:0:frag:1"
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=source_text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(f"ref:{story_id}",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind=evidence_kind,
        story_id=story_id,
    )


def make_context(supports):
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=tuple(supports),
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )


def test_project_article_material_keeps_useful_fact_and_strips_directory_sentences():
    support = make_support(
        story_id="story:enrollment",
        evidence_kind="community_report",
        text="Спортивная школа открыла бесплатную запись детей перед учебным годом.",
        source_text=(
            "Открылась бесплатная запись детей перед учебным годом. "
            "Звоните по номеру +79900000000, бронирование по ссылке https://example.test."
        ),
    )

    projection = project_article_material(make_context((support,)))

    projected = projection.text_by_support_id[support.support_id]
    assert "бесплатную запись детей" in projected
    assert "+79900000000" not in projected
    assert "https://example.test" not in projected
    assert support.source_text.endswith("https://example.test.")


def test_project_article_material_suppresses_only_high_confidence_promotion():
    ad = make_support(
        story_id="story:property-listing",
        evidence_kind="community_report",
        text="Продаётся квартира. Подробности, цена и бронирование по телефону +79900000000.",
        source_text="Продаётся квартира. Подробности, цена и бронирование по телефону +79900000000.",
    )
    report = make_support(
        story_id="story:outage",
        evidence_kind="community_report",
        text="На улице Садовой нет света.",
        source_text="На улице Садовой нет света.",
    )

    projection = project_article_material(make_context((ad, report)))

    assert "story:property-listing" in projection.suppressed_story_ids
    assert "story:outage" not in projection.suppressed_story_ids
    assert projection.text_by_support_id[report.support_id] == report.text


def test_project_article_material_does_not_rescue_classified_ad_with_address():
    ad = make_support(
        story_id="story:property-address",
        evidence_kind="community_report",
        text=(
            "Продаётся квартира на улице Садовой. Цена 100 рублей. "
            "Звоните +79900000000, бронирование по ссылке https://example.test."
        ),
        source_text=(
            "Продаётся квартира на улице Садовой. Цена 100 рублей. "
            "Звоните +79900000000, бронирование по ссылке https://example.test."
        ),
    )

    projection = project_article_material(make_context((ad,)))

    assert "story:property-address" in projection.suppressed_story_ids
    assert projection.actions_by_support_id[ad.support_id] == "SUPPRESS_PROMOTION_ONLY"


def test_project_article_material_keeps_priced_service_access_fact():
    support = make_support(
        story_id="story:water-kiosk",
        evidence_kind="service_access",
        text="Вода на розлив по 3 ₽/литр в киоске на Восточном.",
        source_text="Вода на розлив по 3 ₽/литр в киоске на Восточном.",
    )

    projection = project_article_material(make_context((support,)))

    assert "story:water-kiosk" not in projection.suppressed_story_ids
    assert projection.actions_by_support_id[support.support_id] == "KEEP"
    assert "Вода на розлив" in projection.text_by_support_id[support.support_id]


def test_project_article_material_keeps_one_source_community_report():
    support = make_support(
        story_id="story:water",
        evidence_kind="community_report",
        text="На улице Садовой второй час нет воды.",
        source_text="На улице Садовой второй час нет воды.",
    )

    projection = project_article_material(make_context((support,)))

    assert support.support_id not in projection.trimmed_support_ids
    assert projection.text_by_support_id[support.support_id] == support.text
    assert projection.actions_by_support_id[support.support_id] == "KEEP"


def test_projection_preserves_useful_source_detail_without_contact_payload():
    support = make_support(
        story_id="story:generator",
        evidence_kind="community_report",
        text="Жильцы подключили генератор.",
        source_text=(
            "Жильцы подключили генератор. Он питает оборудование провайдера, "
            "и Wi-Fi появился в доме. Звоните +79900000000 для подробностей."
        ),
    )

    projection = project_article_material(make_context((support,)))

    projected = projection.text_by_support_id[support.support_id]
    assert projected.count("Жильцы подключили генератор.") == 1
    assert "оборудование провайдера" in projected
    assert "Wi-Fi появился" in projected
    assert "+79900000000" not in projected


def test_projection_keeps_added_clause_from_more_specific_source_sentence():
    support = make_support(
        story_id="story:pump",
        evidence_kind="community_report",
        text="На улице Садовой нет воды.",
        source_text=("На улице Садовой нет воды, потому что остановилась насосная станция."),
    )

    projected = project_article_material(make_context((support,))).text_by_support_id[
        support.support_id
    ]

    assert "нет воды" in projected
    assert "остановилась насосная станция" in projected


def test_project_article_material_does_not_suppress_story_with_useful_and_promotional_supports():
    useful = make_support(
        story_id="story:club",
        evidence_kind="community_report",
        text="Бесплатная секция открыла набор детей с 1 сентября.",
        source_text="Бесплатная секция открыла набор детей с 1 сентября.",
    )
    promotion = make_support(
        story_id="story:club",
        evidence_kind="commercial_offer",
        text="Запись по телефону +79900000000 и через https://example.test.",
        source_text="Запись по телефону +79900000000 и через https://example.test.",
    )
    promotion = replace(promotion, support_id="story:club:evidence:1:frag:2")

    projection = project_article_material(make_context((useful, promotion)))

    assert "story:club" not in projection.suppressed_story_ids
    assert projection.actions_by_support_id[useful.support_id] == "KEEP"
    assert projection.actions_by_support_id[promotion.support_id] == "TRIM_DIRECTORY"
    assert "+79900000000" not in projection.text_by_support_id[promotion.support_id]


def test_projection_metadata_contains_only_stable_ids_actions_and_reasons():
    support = make_support(
        story_id="story:property-listing",
        evidence_kind="commercial_offer",
        text="Продаётся квартира, цена 100 рублей, звоните +79900000000.",
        source_text="Продаётся квартира, цена 100 рублей, звоните +79900000000.",
    )

    metadata = project_article_material(make_context((support,))).to_metadata()
    serialized = repr(metadata)

    assert support.support_id in metadata["actions_by_support_id"]
    assert "story:property-listing" in metadata["suppressed_story_ids"]
    assert "+79900000000" not in serialized
    assert "100 рублей" not in serialized


def test_materialize_validation_context_uses_only_surviving_projected_supports():
    kept = make_support(
        story_id="story:kept",
        evidence_kind="community_report",
        text="Исходная деталь о подаче воды.",
        source_text="Исходный источник о подаче воды.",
    )
    trimmed = make_support(
        story_id="story:trimmed",
        evidence_kind="community_report",
        text="Исходная деталь с телефоном.",
        source_text="Исходный источник с телефоном.",
    )
    suppressed = make_support(
        story_id="story:suppressed",
        evidence_kind="commercial_offer",
        text="Объявление с исходным payload.",
        source_text="Объявление с исходным payload.",
    )
    excluded = replace(
        make_support(
            story_id="story:excluded",
            evidence_kind="community_report",
            text="Контекстная деталь.",
            source_text="Контекстная деталь.",
        ),
        publication_use="EXCLUDE",
    )
    context = make_context((kept, trimmed, suppressed, excluded))
    projection = ArticleMaterialProjection(
        text_by_support_id={
            kept.support_id: "Проецированная деталь о подаче воды.",
            trimmed.support_id: "Проецированная деталь без телефона.",
            suppressed.support_id: "Текст suppression не должен попасть в validator.",
            excluded.support_id: "Исключённый текст.",
        },
        actions_by_support_id={
            kept.support_id: "KEEP",
            trimmed.support_id: "TRIM_DIRECTORY",
            suppressed.support_id: "KEEP",
            excluded.support_id: "KEEP",
        },
        reasons_by_support_id={},
        suppressed_story_ids=("story:suppressed",),
    )

    projected = materialize_article_validation_context(context, projection)

    assert tuple(s.support_id for s in projected.support_index) == (
        kept.support_id,
        trimmed.support_id,
    )
    assert projected.support_by_id[kept.support_id].text == "Проецированная деталь о подаче воды."
    assert projected.support_by_id[trimmed.support_id].source_text == (
        "Проецированная деталь без телефона."
    )
    assert projected.support_by_id[kept.support_id].source_refs == kept.source_refs
    assert suppressed.support_id not in projected.support_by_id
    assert excluded.support_id not in projected.support_by_id
