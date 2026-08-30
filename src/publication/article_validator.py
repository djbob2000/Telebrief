"""Deterministic structural and evidence-bound validator for editorial articles."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from src.config_loader import PublicationEditorialConfig
from src.publication.article_claim_support import assess_claim_against_supports
from src.publication.article_claims import ConcreteClaim, find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import ArticleClaimAtom, StructuredArticleDraft

_INTERNAL_HANDLE_PATTERN = re.compile(
    r"\[(?:story:\d+:evidence:\d+:frag:\d+|story:\d+|evidence:\d+:frag:\d+|op:[^\]]+|SUPPORT\s+[^\]]+)\]",
    re.IGNORECASE,
)

_EXPANSION_RE = re.compile(
    r"\b(?:хроник[а-я]*\s+недел[а-я]*|итог[а-я]*\s+недел[а-я]*|событи[а-я]*\s+недел[а-я]*|обзор[а-я]*\s+недел[а-я]*|за\s+недел[а-я]*|итог[а-я]*\s+месяц[а-я]*|событи[а-я]*\s+месяц[а-я]*|обзор[а-я]*\s+месяц[а-я]*|за\s+месяц[а-я]*)\b",
    re.IGNORECASE,
)

_CONTINUATION_RE = re.compile(
    r"\b(?:продолжа[а-я]+|сохраня[а-я]+|по-прежнему|ранее|с начала|до этого|прежде)\b",
    re.IGNORECASE,
)

_FUTURE_MARKER_RE = re.compile(
    r"\b(?:будет|будут|запланирован[а-я]*|предстоит|ожидает[а-я]*|намечен[а-я]*|планирует[а-я]*)\b|\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|\b\d{1,2}\.\d{2}\b",
    re.IGNORECASE,
)

_CURRENT_STATE_OUTAGE_RE = re.compile(
    r"\b(?:отключен[оаыи]|отключен|не\s+работа[а-я]+|отсутству[а-я]+|прекращен[оаыи]|прекращен|обесточен[оаыи]|обесточен)\b",
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

    # 3. Reporting window expansion check (for windows <= 48h)
    is_short_window = True
    if context.publication_window is not None:
        delta = context.publication_window.snapshot_at - context.publication_window.lookback_start
        if delta > dt.timedelta(hours=48):
            is_short_window = False

    if is_short_window:
        if _EXPANSION_RE.search(draft.title):
            issues.append(
                ArticleValidationIssue(
                    code="REPORTING_WINDOW_EXPANSION",
                    unit_id="TITLE",
                    message=f"Draft title expands reporting window beyond configured lookback: '{draft.title}'",
                )
            )
        if _EXPANSION_RE.search(draft.lead):
            issues.append(
                ArticleValidationIssue(
                    code="REPORTING_WINDOW_EXPANSION",
                    unit_id="LEAD",
                    message=f"Draft lead expands reporting window beyond configured lookback: '{draft.lead}'",
                )
            )

    # 4. Unit-by-unit validation
    # Construct sequence of units: (unit_id, unit_type, text, cited_support_ids, claim_atoms)
    units: list[tuple[str, str, str, tuple[str, ...], tuple[ArticleClaimAtom, ...]]] = []
    units.append(("TITLE", "title", draft.title, draft.title_support_ids, draft.title_claims))
    units.append(("LEAD", "lead", draft.lead, draft.lead_support_ids, draft.lead_claims))

    p_idx = 1
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        units.append((h_id, "heading", sec.heading, sec.heading_support_ids, sec.heading_claims))
        for para in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            units.append((p_id, "paragraph", para.text, para.cited_support_ids, para.claims))
            p_idx += 1

    # Support map from context
    support_map = context.support_by_id

    for unit_id, unit_type, unit_text, cited_ids, claim_atoms in units:
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

        # Check claim atoms existence
        if not claim_atoms:
            issues.append(
                ArticleValidationIssue(
                    code="MISSING_CLAIM_ATOMS",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} ({unit_type}) must contain at least one claim atom",
                    support_ids=cited_ids,
                )
            )
        else:
            unit_sids = set(cited_ids)
            claim_sids = {sid for c in claim_atoms for sid in c.cited_support_ids}
            if unit_sids != claim_sids:
                issues.append(
                    ArticleValidationIssue(
                        code="CLAIM_SUPPORT_MISMATCH",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} support IDs {sorted(unit_sids)} do not match claim atom support IDs {sorted(claim_sids)}",
                        support_ids=cited_ids,
                    )
                )

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

        # Also verify claim atoms' support IDs
        for claim in claim_atoms:
            for csid in claim.cited_support_ids:
                if csid not in support_map:
                    unknown_evidence_ids.append(csid)
                    has_unknown = True
                    issues.append(
                        ArticleValidationIssue(
                            code="UNKNOWN_CLAIM_SUPPORT_ID",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} claim '{claim.text}' cites unknown support ID '{csid}'",
                            support_ids=(csid,),
                        )
                    )

        if has_unknown or not valid_supports:
            continue

        # Check publication policy and temporal roles for title and lead
        if unit_type in ("title", "lead"):
            has_publish_current = any(
                s.publication_use == "PUBLISH" and s.temporal_role == "CURRENT_WINDOW"
                for s in valid_supports
            )
            if not has_publish_current:
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) requires at least one PUBLISH support with CURRENT_WINDOW temporal role",
                        support_ids=cited_ids,
                    )
                )
        elif unit_type == "heading":
            if not any(s.publication_use == "PUBLISH" for s in valid_supports):
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) requires at least one PUBLISH support",
                        support_ids=cited_ids,
                    )
                )

        # Temporal framing checks
        # 1. Historical context framing
        has_hist = any(s.temporal_role == "HISTORICAL_CONTEXT" for s in valid_supports)
        if has_hist:
            if unit_type in ("title", "lead"):
                has_curr = any(s.temporal_role == "CURRENT_WINDOW" for s in valid_supports)
                has_continuation = bool(_CONTINUATION_RE.search(unit_text))
                if not (has_curr and has_continuation):
                    issues.append(
                        ArticleValidationIssue(
                            code="HISTORICAL_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} cites historical context without current window evidence and continuation framing",
                            support_ids=cited_ids,
                        )
                    )

        # 2. Future scheduled framing
        all_future = bool(valid_supports) and all(
            s.temporal_role == "FUTURE_SCHEDULED" for s in valid_supports
        )
        if all_future:
            has_active_outage_desc = bool(_CURRENT_STATE_OUTAGE_RE.search(unit_text))
            if unit_type in ("lead", "paragraph"):
                has_future_marker = bool(_FUTURE_MARKER_RE.search(unit_text))
                if not has_future_marker or has_active_outage_desc:
                    issues.append(
                        ArticleValidationIssue(
                            code="FUTURE_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} ({unit_type}) with future scheduled supports lacks explicit future marker or describes outage as current",
                            support_ids=cited_ids,
                        )
                    )
            elif unit_type == "heading":
                if has_active_outage_desc:
                    issues.append(
                        ArticleValidationIssue(
                            code="FUTURE_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Heading {unit_id} with future scheduled supports describes outage as currently active",
                            support_ids=cited_ids,
                        )
                    )

        # Check claim atoms against their cited supports
        for claim in claim_atoms:
            c_supports = [support_map[sid] for sid in claim.cited_support_ids if sid in support_map]
            if c_supports:
                assessment = assess_claim_against_supports(
                    claim.text,
                    c_supports,
                    min_content_coverage=config.article_claim_min_content_coverage,
                )
                if not assessment.supported:
                    issues.append(
                        ArticleValidationIssue(
                            code="UNSUPPORTED_CLAIM_ATOM",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} claim atom '{claim.text}' is not supported: missing stems {assessment.unsupported_content_stems}",
                            support_ids=claim.cited_support_ids,
                            unsupported_claims=assessment.unsupported_concrete_claims,
                        )
                    )

        # Defense in depth: Check unsupported concrete claims against cited support texts
        support_texts = [t for s in valid_supports for t in (s.text, s.source_text) if t]
        unsupported = find_unsupported_claims(unit_text, support_texts)
        if unsupported:
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

            for claim_item in unsupported:
                if claim_item.kind == "causal_relation":
                    code = "UNSUPPORTED_CAUSAL_RELATION"
                elif claim_item.kind == "mechanism_relation":
                    code = "UNSUPPORTED_MECHANISM"
                else:
                    code = "UNSUPPORTED_CONCRETE_CLAIM"

                issues.append(
                    ArticleValidationIssue(
                        code=code,
                        unit_id=unit_id,
                        message=f"Unit {unit_id} contains unsupported {claim_item.kind} claim '{claim_item.raw}'",
                        support_ids=cited_ids,
                        unsupported_claims=(claim_item,),
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
