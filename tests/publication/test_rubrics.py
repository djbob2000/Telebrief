"""Tests for DigestRubricClassifier (Plan 5 Task 5)."""

from __future__ import annotations

from typing import Any, Sequence

import pytest

from src.config_loader import DigestRubricConfig, DigestRubricsConfig
from src.editorial_models import StoryCard
from src.publication.rubrics import (
    DigestRubricClassifier,
    cosine_similarity,
    rubric_classification_text,
    story_classification_text,
)


class FakeEmbeddingProvider:
    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        default_vector: list[float] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.vectors = vectors or {}
        self.default_vector = default_vector or [1.0, 0.0]
        self.error = error
        self.calls: list[list[str]] = []

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
        purpose: str | None = None,
    ) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        self.calls.append(list(texts))
        result = []
        for t in texts:
            vec = self.vectors.get(t, self.default_vector)
            result.append(list(vec))
        return result


def make_test_rubrics(min_similarity: float = 0.38) -> DigestRubricsConfig:
    return DigestRubricsConfig(
        min_similarity=min_similarity,
        items=(
            DigestRubricConfig(
                id="infrastructure",
                name="Инфраструктура и ЖКХ",
                description="Электричество, вода, газ, тепло",
                emoji="⚡️",
                fallback=False,
            ),
            DigestRubricConfig(
                id="environment",
                name="Погода и экология",
                description="море пляжи экология медузы",
                emoji="🌦",
                fallback=False,
            ),
            DigestRubricConfig(
                id="other",
                name="Другое",
                description="другие события",
                emoji="📌",
                fallback=True,
            ),
        ),
    )


def make_card(
    id: str = "story:1",
    topic: str = "Тема",
    summary: str = "Описание",
    tags: list[str] | None = None,
    category: str = "",
) -> StoryCard:
    return StoryCard(
        id=id,
        topic=topic,
        importance="medium",
        summary=summary,
        tags=tags or [],
        category=category,
        representative_source_refs=["ref:1"],
    )


def make_classifier(provider: Any) -> DigestRubricClassifier:
    return DigestRubricClassifier(
        provider=provider,
        provider_name="test_emb",
        model="test-embedding-model",
        dimensions=2,
    )


@pytest.mark.unit
def test_classification_helpers():
    card = make_card("story:1", "Тема", "Описание", ["тег1", "тег2"])
    text = story_classification_text(card)
    assert text == "Тема\nОписание\nтег1\nтег2"

    rubric = DigestRubricConfig(id="inf", name="Инфра", description="Свет и вода")
    r_text = rubric_classification_text(rubric)
    assert r_text == "Инфра\nСвет и вода"

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == -1.0


@pytest.mark.asyncio
async def test_classifier_maps_unknown_future_topic_semantically():
    rubrics = make_test_rubrics(min_similarity=0.5)
    card = make_card(
        id="story:7",
        topic="Пляж временно закрыли из-за большого количества медуз",
        tags=["море", "медузы", "пляж"],
    )
    provider = FakeEmbeddingProvider(
        vectors={
            rubric_classification_text(rubrics.items[0]): [0.0, 1.0],  # infrastructure
            rubric_classification_text(rubrics.items[1]): [1.0, 0.0],  # environment
            story_classification_text(card): [0.95, 0.05],
        }
    )

    cards, assignments = await make_classifier(provider).classify([card], rubrics=rubrics)

    assert cards[0].rubric_id == "environment"
    assert assignments[0].rubric_id == "environment"
    assert assignments[0].method == "semantic"
    assert assignments[0].score is not None
    assert assignments[0].score > 0.9


@pytest.mark.asyncio
async def test_low_similarity_goes_to_configured_fallback():
    rubrics = make_test_rubrics(min_similarity=0.80)
    card = make_card("story:1", "Необычное локальное событие", tags=["редкая тема"])
    provider = FakeEmbeddingProvider(
        vectors={
            rubric_classification_text(rubrics.items[0]): [1.0, 0.0],
            rubric_classification_text(rubrics.items[1]): [1.0, 0.0],
            story_classification_text(card): [0.0, 1.0],
        }
    )
    result, assignments = await make_classifier(provider).classify([card], rubrics=rubrics)
    assert result[0].rubric_id == rubrics.fallback.id
    assert assignments[0].method == "fallback"


@pytest.mark.asyncio
async def test_embedding_failure_keeps_all_cards_and_uses_fallback():
    rubrics = make_test_rubrics()
    cards = [make_card("story:1", "A"), make_card("story:2", "B")]
    provider = FakeEmbeddingProvider(error=RuntimeError("provider down"))
    result, assignments = await make_classifier(provider).classify(cards, rubrics=rubrics)
    assert [c.id for c in result] == ["story:1", "story:2"]
    assert all(c.rubric_id == rubrics.fallback.id for c in result)
    assert len(assignments) == 2


@pytest.mark.asyncio
async def test_cold_classification_uses_one_embed_many_call_for_rubrics_and_cards():
    rubrics = make_test_rubrics()
    provider = FakeEmbeddingProvider(default_vector=[1.0, 0.0])
    await make_classifier(provider).classify([make_card("story:1", "A")], rubrics=rubrics)
    assert len(provider.calls) == 1
    # 2 non-fallback rubrics + 1 card = 3 texts
    assert len(provider.calls[0]) == 3


@pytest.mark.asyncio
async def test_warm_classifier_reuses_rubric_vectors_and_embeds_only_cards():
    rubrics = make_test_rubrics()
    provider = FakeEmbeddingProvider(default_vector=[1.0, 0.0])
    classifier = make_classifier(provider)
    await classifier.classify([make_card("story:1", "A")], rubrics=rubrics)
    first_call_count = len(provider.calls)
    await classifier.classify([make_card("story:2", "B")], rubrics=rubrics)
    assert len(provider.calls) == first_call_count + 1
    assert len(provider.calls[-1]) == 1


@pytest.mark.asyncio
async def test_matching_legacy_category_can_be_used_as_compatibility_hint_without_embedding():
    rubrics = make_test_rubrics()
    provider = FakeEmbeddingProvider(default_vector=[1.0, 0.0])
    card = make_card("story:1", "Legacy")
    card.category = rubrics.items[0].id
    result, assignments = await make_classifier(provider).classify([card], rubrics=rubrics)
    assert result[0].rubric_id == rubrics.items[0].id
    assert assignments[0].method == "legacy_hint"
    assert provider.calls == []


def test_rubric_classifier_version_is_v2():
    from src.publication.rubrics import RUBRIC_CLASSIFIER_VERSION

    assert RUBRIC_CLASSIFIER_VERSION == "digest-rubric-embedding-v2"


def test_story_classification_text_includes_facts_and_omits_uncertainties():
    from src.editorial_models import StoryElement

    card = make_card("story:1", topic="Главная тема", summary="Краткая суть", tags=["тег1"])
    card.hard_facts = [
        StoryElement(text="Факт 1", source_refs=["r1"], status="established"),
        StoryElement(text="Факт 2", source_refs=["r2"], status="established"),
    ]
    card.useful_details = [
        StoryElement(text="Полезная деталь", source_refs=["r3"], status="established"),
    ]
    card.community_observations = [
        StoryElement(text="Наблюдение жителей", source_refs=["r4"], status="attributed"),
    ]
    card.uncertainties = [
        StoryElement(text="Вопрос: работает ли банк?", source_refs=["r5"], status="attributed"),
    ]

    text = story_classification_text(card)
    assert "Главная тема" in text
    assert "Краткая суть" in text
    assert "Факт 1" in text
    assert "Факт 2" in text
    assert "Полезная деталь" in text
    assert "Наблюдение жителей" in text
    assert "Вопрос: работает ли банк?" not in text


@pytest.mark.asyncio
async def test_borderline_telecom_routes_via_family_fallback():
    """Card with sub-threshold similarity (0.2 < 0.380) and telecom family routes to communications."""
    rubrics_cfg = DigestRubricsConfig(
        min_similarity=0.38,
        items=(
            DigestRubricConfig(
                id="infrastructure",
                name="Инфраструктура",
                description="жкх свет вода",
                emoji="⚡️",
                fallback=False,
            ),
            DigestRubricConfig(
                id="communications",
                name="Связь",
                description="интернет связь провайдер",
                emoji="📱",
                fallback=False,
            ),
            DigestRubricConfig(
                id="other",
                name="Другое",
                description="прочее",
                emoji="📌",
                fallback=True,
            ),
        ),
    )
    # Card vector orthogonal to rubrics so similarity is < 0.38
    provider = FakeEmbeddingProvider(
        vectors={
            "Инфраструктура\nжкх свет вода": [1.0, 0.0],
            "Связь\nинтернет связь провайдер": [0.95, 0.30],
        },
        default_vector=[0.0, 1.0],
    )
    classifier = DigestRubricClassifier(provider=provider, dimensions=2)

    card = make_card(
        "story:854",
        topic="Связь и интернет",
        summary="Провайдер Миранда проводит работы на улицах Морозова и Гайдара, интернет временно отключен",
        tags=["интернет", "миранда", "связь"],
    )

    result_cards, assignments = await classifier.classify([card], rubrics=rubrics_cfg)
    assert len(assignments) == 1
    assert assignments[0].rubric_id == "communications"
    assert assignments[0].method == "family_fallback"
    assert result_cards[0].rubric_id == "communications"
