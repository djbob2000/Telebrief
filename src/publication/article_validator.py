"""Deterministic structural and evidence-bound validator for editorial articles."""

from __future__ import annotations

from dataclasses import dataclass

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import StructuredArticleDraft


@dataclass(frozen=True)
class ArticleValidationResult:
    """Outcome of deterministic article draft validation."""

    is_valid: bool
    word_count: int
    section_count: int
    unknown_evidence_ids: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


def validate_article_draft(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig | None = None,
) -> ArticleValidationResult:
    """Validate structured article draft against evidence bounds and length constraints."""
    if config is None:
        config = PublicationEditorialConfig()

    violations: list[str] = []
    unknown_evidence_ids: list[str] = []

    # 1. Validate cited evidence IDs against context
    for evi_id in draft.cited_evidence_ids:
        if evi_id not in context.evidence_by_id:
            unknown_evidence_ids.append(evi_id)
            violations.append(f"Draft cites unknown evidence ID '{evi_id}'")

    # 2. Validate section count
    section_count = len(draft.sections)
    if section_count < config.article_min_sections:
        violations.append(
            f"Draft has {section_count} sections, minimum required is {config.article_min_sections}"
        )
    elif section_count > config.article_max_sections:
        violations.append(
            f"Draft has {section_count} sections, maximum allowed is {config.article_max_sections}"
        )

    # 3. Validate word count
    word_count = draft.word_count
    if word_count < config.article_min_words:
        violations.append(
            f"Draft has {word_count} words, minimum required is {config.article_min_words}"
        )
    elif word_count > config.article_max_words:
        violations.append(
            f"Draft has {word_count} words, maximum allowed is {config.article_max_words}"
        )

    # 4. Validate title and lead presence
    if not draft.title:
        violations.append("Draft title cannot be empty")
    if not draft.lead:
        violations.append("Draft lead cannot be empty")

    is_valid = len(violations) == 0

    return ArticleValidationResult(
        is_valid=is_valid,
        word_count=word_count,
        section_count=section_count,
        unknown_evidence_ids=tuple(unknown_evidence_ids),
        unsupported_claims=(),
        violations=tuple(violations),
    )
