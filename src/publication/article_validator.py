"""Deterministic structural and evidence-bound validator for editorial articles."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.config_loader import PublicationEditorialConfig
from src.publication.article_claims import ConcreteClaim, find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import StructuredArticleDraft

_INTERNAL_HANDLE_PATTERN = re.compile(
    r"\[(?:story:\d+:evidence:\d+:frag:\d+|story:\d+|evidence:\d+:frag:\d+|op:[^\]]+|SUPPORT\s+[^\]]+)\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleValidationIssue:
    """A specific validation violation within a draft unit."""

    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...] = ()
    unsupported_claims: tuple[ConcreteClaim, ...] = ()


@dataclass(frozen=True)
class ArticleValidationResult:
    """Outcome of deterministic article draft validation."""

    is_valid: bool
    word_count: int
    section_count: int
    issues: tuple[ArticleValidationIssue, ...] = ()
    unknown_evidence_ids: tuple[str, ...] = ()

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(f"{iss.code}:{iss.unit_id}" for iss in self.issues)

    @property
    def unsupported_claims(self) -> tuple[ConcreteClaim, ...]:
        claims: list[ConcreteClaim] = []
        for iss in self.issues:
            claims.extend(iss.unsupported_claims)
        return tuple(claims)


def validate_article_draft(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig | None = None,
) -> ArticleValidationResult:
    """Validate structured article draft against support bounds, factual claims, and length constraints."""
    if config is None:
        config = PublicationEditorialConfig()

    issues: list[ArticleValidationIssue] = []
    unknown_evidence_ids: list[str] = []

    # 1. Structural constraints: Title and Lead presence
    if not draft.title or not draft.title.strip():
        issues.append(
            ArticleValidationIssue(
                code="EMPTY_TITLE",
                unit_id="TITLE",
                message="Draft title cannot be empty",
            )
        )
    if not draft.lead or not draft.lead.strip():
        issues.append(
            ArticleValidationIssue(
                code="EMPTY_LEAD",
                unit_id="LEAD",
                message="Draft lead cannot be empty",
            )
        )

    # 2. Section count and word count
    section_count = len(draft.sections)
    if section_count < config.article_min_sections:
        issues.append(
            ArticleValidationIssue(
                code="SECTION_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {section_count} sections, minimum required is {config.article_min_sections}",
            )
        )
    elif section_count > config.article_max_sections:
        issues.append(
            ArticleValidationIssue(
                code="SECTION_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {section_count} sections, maximum allowed is {config.article_max_sections}",
            )
        )

    word_count = draft.word_count
    if word_count < config.article_min_words:
        issues.append(
            ArticleValidationIssue(
                code="WORD_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {word_count} words, minimum required is {config.article_min_words}",
            )
        )
    elif word_count > config.article_max_words:
        issues.append(
            ArticleValidationIssue(
                code="WORD_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {word_count} words, maximum allowed is {config.article_max_words}",
            )
        )

    # 3. Unit-by-unit validation
    # Construct sequence of units: (unit_id, unit_type, text, cited_support_ids)
    units: list[tuple[str, str, str, tuple[str, ...]]] = []
    units.append(("TITLE", "title", draft.title, draft.title_support_ids))
    units.append(("LEAD", "lead", draft.lead, draft.lead_support_ids))

    p_idx = 1
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        units.append((h_id, "heading", sec.heading, sec.heading_support_ids))
        for para in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            units.append((p_id, "paragraph", para.text, para.cited_support_ids))
            p_idx += 1

    # Support map from context
    support_map = context.support_by_id

    for unit_id, unit_type, unit_text, cited_ids in units:
        if not unit_text.strip():
            continue

        # Check for internal handle leaks in raw text
        if _INTERNAL_HANDLE_PATTERN.search(unit_text):
            issues.append(
                ArticleValidationIssue(
                    code="INTERNAL_HANDLE_LEAK",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} contains internal evidence handle",
                )
            )

        # Check missing support IDs
        if not cited_ids:
            issues.append(
                ArticleValidationIssue(
                    code=f"MISSING_SUPPORT:{unit_type}",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} is missing support citation",
                )
            )
            continue

        valid_supports: list[ArticleSupport] = []
        has_unknown = False
        for sid in cited_ids:
            if sid not in support_map:
                unknown_evidence_ids.append(sid)
                has_unknown = True
                issues.append(
                    ArticleValidationIssue(
                        code="UNKNOWN_SUPPORT_ID",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} cites unknown support ID '{sid}'",
                        support_ids=(sid,),
                    )
                )
            else:
                valid_supports.append(support_map[sid])

        if has_unknown or not valid_supports:
            continue

        # Check publication policy
        # title, lead, heading require at least one PUBLISH support
        if unit_type in ("title", "lead", "heading"):
            if not any(s.publication_use == "PUBLISH" for s in valid_supports):
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) requires at least one PUBLISH support",
                        support_ids=cited_ids,
                    )
                )

        # Check unsupported concrete claims against cited support texts
        support_texts = [t for s in valid_supports for t in (s.text, s.source_text) if t]
        unsupported = find_unsupported_claims(unit_text, support_texts)
        if unsupported:
            # If paragraph has concrete claims, check that it has at least one PUBLISH support
            if unit_type == "paragraph" and not any(
                s.publication_use == "PUBLISH" for s in valid_supports
            ):
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Paragraph {unit_id} with concrete claims requires at least one PUBLISH support",
                        support_ids=cited_ids,
                    )
                )

            for claim in unsupported:
                if claim.kind == "causal_relation":
                    code = "UNSUPPORTED_CAUSAL_RELATION"
                elif claim.kind == "mechanism_relation":
                    code = "UNSUPPORTED_MECHANISM"
                else:
                    code = "UNSUPPORTED_CONCRETE_CLAIM"

                issues.append(
                    ArticleValidationIssue(
                        code=code,
                        unit_id=unit_id,
                        message=f"Unit {unit_id} contains unsupported {claim.kind} claim '{claim.raw}'",
                        support_ids=cited_ids,
                        unsupported_claims=(claim,),
                    )
                )

    is_valid = len(issues) == 0

    return ArticleValidationResult(
        is_valid=is_valid,
        word_count=word_count,
        section_count=section_count,
        issues=tuple(issues),
        unknown_evidence_ids=tuple(dict.fromkeys(unknown_evidence_ids)),
    )
