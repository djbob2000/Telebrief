from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

ArticleRejectionReason = Literal[
    "validation_failed",
    "writer_failed",
]

_ERROR_KIND_BY_REASON: dict[ArticleRejectionReason, str] = {
    "validation_failed": "article_validation_rejected",
    "writer_failed": "article_writer_rejected",
}


class ArticlePublicationRejected(RuntimeError):
    """Terminal, non-publishable outcome of the one-call Event-First article writer."""

    def __init__(
        self,
        *,
        reason: ArticleRejectionReason,
        message: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.error_kind = _ERROR_KIND_BY_REASON[reason]
        self.metadata = dict(metadata or {})


class ArticleFinalizationInvariantError(RuntimeError):
    """Terminal failure after deterministic Event-First article recovery is exhausted."""


class DigestCoverageInvariantError(RuntimeError):
    """Terminal failure when digest coverage requirements or invariants are violated."""


class UnsupportedFrozenSemanticVersion(ValueError):
    """Raised when frozen publication policy specifies an unsupported semantic version."""
