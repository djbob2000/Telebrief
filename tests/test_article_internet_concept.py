# ruff: noqa: S101
"""Fixed-line internet access is the internet concept (run 995): wording without
the word "интернет" must not look like an unsupported critical term."""

from __future__ import annotations

from src.publication.article_semantic_lexicon import canonical_semantic_concepts


def test_fiber_and_provider_wording_is_the_internet_concept():
    assert "concept:internet" in canonical_semantic_concepts(
        "миранда по нашему адресу волокно не ведет"
    )
    assert "concept:internet" in canonical_semantic_concepts("провайдер не подключает оптоволокно")


def test_power_sentence_is_not_an_internet_claim():
    assert "concept:internet" not in canonical_semantic_concepts("на Пушкина нет света")
