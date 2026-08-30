"""Unit tests for Event-First article fail-closed rejection semantics."""

from __future__ import annotations

from src.publication.errors import ArticlePublicationRejected


def test_article_publication_rejected_exposes_stable_reason_and_metadata():
    exc = ArticlePublicationRejected(
        reason="validation_failed",
        message="draft failed deterministic validation",
        metadata={"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]},
    )

    assert exc.reason == "validation_failed"
    assert exc.error_kind == "article_validation_rejected"
    assert exc.metadata == {"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]}
    assert str(exc) == "draft failed deterministic validation"


def test_article_publication_rejected_maps_writer_failure_to_stable_error_kind():
    exc = ArticlePublicationRejected(
        reason="writer_failed",
        message="provider failed",
        metadata={"exception_type": "TimeoutError"},
    )

    assert exc.error_kind == "article_writer_rejected"
