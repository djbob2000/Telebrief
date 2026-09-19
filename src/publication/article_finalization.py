"""Article finalizer managing single-call validation, deterministic recovery, and claim tracing."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import (
    ArticleCoverageDiagnostics,
    diagnose_article_coverage,
)
from src.publication.article_length import ArticleLengthProfile
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_for_dedup,
    _split_sentences_safe,
)
from src.publication.article_recovery import ArticleDeterministicComposer
from src.publication.article_trace import (
    ArticleClaimTraceUnit,
    build_article_claim_trace,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
    validate_article_draft,
)
from src.publication.errors import (
    ArticleFinalizationInvariantError,
    ArticlePublicationRejected,
)

logger = logging.getLogger(__name__)


class GenerationAttemptObserver(Protocol):
    async def attempt_started(self, kind: str, **kwargs: Any) -> int: ...
    async def attempt_finished(self, attempt_id: int, status: str, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class ArticleFinalizationResult:
    draft: StructuredArticleDraft
    claim_trace: tuple[ArticleClaimTraceUnit, ...]
    writer_status: Literal["passed", "rejected", "failed"]
    recovery_mode: Literal["none", "supplement", "full_fallback"]
    ai_covered_story_ids: tuple[str, ...]
    supplemented_story_ids: tuple[str, ...]
    final_covered_story_ids: tuple[str, ...]
    metadata: dict[str, Any]


def _build_final_metadata(
    *,
    winning_kind: str,
    writer_status: str,
    recovery_mode: str,
    coverage_plan: ArticleCoveragePlan,
    ai_covered_story_ids: Sequence[str],
    supplemented_story_ids: Sequence[str],
    final_covered_story_ids: Sequence[str],
    ai_diag: ArticleCoverageDiagnostics | None,
    final_diag: ArticleCoverageDiagnostics,
    trace: Sequence[ArticleClaimTraceUnit],
) -> dict[str, Any]:
    planned_story_count = len(coverage_plan.story_ids)
    ai_story_coverage = (
        len(ai_covered_story_ids) / planned_story_count if planned_story_count else 1.0
    )
    origin_counts = Counter(unit.generation_origin for unit in trace)
    trace_meta = [
        {
            "unit_id": unit.unit_id,
            "support_ids": list(unit.support_ids),
            "source_refs": list(unit.source_refs),
            "fragment_ids": list(unit.fragment_ids),
            "source_item_ids": list(unit.source_item_ids),
            "temporal_roles": list(unit.temporal_roles),
            "generation_origin": unit.generation_origin,
            "claim_atoms": [
                {
                    "text": atom.text,
                    "support_ids": list(atom.support_ids),
                    "temporal_roles": list(atom.temporal_roles),
                }
                for atom in unit.claim_atoms
            ],
        }
        for unit in trace
    ]
    return {
        "status": "writer_success" if writer_status == "passed" else "fallback_success",
        "winning_kind": winning_kind,
        "writer_status": writer_status,
        "recovery_mode": recovery_mode,
        "planned_story_count": planned_story_count,
        "ai_covered_story_count": len(ai_covered_story_ids),
        "supplemented_story_count": len(supplemented_story_ids),
        "final_covered_story_count": len(final_covered_story_ids),
        "ai_story_coverage": ai_story_coverage,
        "final_story_coverage": final_diag.story_coverage,
        "planned_detail_support_count": final_diag.planned_detail_support_count,
        "ai_detail_support_coverage": ai_diag.detail_support_coverage
        if ai_diag is not None
        else 0.0,
        "final_detail_support_coverage": final_diag.detail_support_coverage,
        "coverage": {
            "planned_story_count": final_diag.planned_story_count,
            "covered_story_count": final_diag.covered_story_count,
            "uncovered_story_ids": list(final_diag.uncovered_story_ids),
            "develop_story_coverage": final_diag.develop_story_coverage,
            "weave_story_coverage": final_diag.weave_story_coverage,
            "brief_story_coverage": final_diag.brief_story_coverage,
            "planned_detail_support_count": final_diag.planned_detail_support_count,
            "covered_detail_support_count": final_diag.covered_detail_support_count,
            "uncovered_detail_support_ids": list(final_diag.uncovered_detail_support_ids),
            "detail_support_coverage": final_diag.detail_support_coverage,
            "leaked_contact_payloads": list(final_diag.leaked_contact_payloads),
        },
        "generation_origin_counts": {
            "AI": origin_counts.get("AI", 0),
            "SUPPLEMENT": origin_counts.get("SUPPLEMENT", 0),
            "FALLBACK": origin_counts.get("FALLBACK", 0),
        },
        "validation": {
            "is_valid": True,
            "unsupported_claim_count": 0,
            "unit_count": len(trace),
        },
        "unsupported_final_claim_count": 0,
        "leaked_directory_payload_count": len(final_diag.leaked_contact_payloads),
        "claim_trace": trace_meta,
    }


def _sanitize_unsupported_quotes(
    draft: StructuredArticleDraft,
    quote_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext,
) -> StructuredArticleDraft:
    """Deterministically convert unsupported direct quote marks into indirect speech without quotes."""
    bad_spans = {c.raw for iss in quote_violations for c in iss.unsupported_claims if c.raw}
    if not bad_spans:
        return draft

    def _strip_spans(text: str) -> str:
        res = text
        for span in bad_spans:
            inner = span.strip(' «»"“”')
            res = res.replace(span, inner)
        return res

    new_sections = []
    for sec in draft.sections:
        new_heading = _strip_spans(sec.heading)
        new_paras = []
        for p in sec.paragraphs:
            new_p_text = _strip_spans(p.text)
            new_paras.append(
                ArticleParagraph(
                    text=new_p_text,
                    cited_support_ids=p.cited_support_ids,
                    claims=p.claims,
                    generation_origin=p.generation_origin,
                )
            )
        new_sections.append(
            ArticleSection(
                heading=new_heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(new_paras),
                heading_generation_origin=sec.heading_generation_origin,
            )
        )

    return StructuredArticleDraft(
        title=_strip_spans(draft.title),
        title_support_ids=draft.title_support_ids,
        lead=_strip_spans(draft.lead),
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _merge_orphan_paragraphs(
    draft: StructuredArticleDraft,
) -> StructuredArticleDraft:
    """Merge single-sentence orphan paragraphs into preceding paragraph (AGENTS.md §0.9).

    A paragraph is considered an orphan if it contains only one sentence
    (≤1 sentence-ending punctuation mark) and is not the only paragraph in its section.
    Orphan paragraphs are appended to the preceding paragraph text with a space separator.
    """
    changed = False
    new_sections = []
    for sec in draft.sections:
        if len(sec.paragraphs) <= 1:
            new_sections.append(sec)
            continue
        merged_paras: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            sentences = _split_sentences_safe(p.text)
            is_orphan = len(sentences) <= 1 and len(merged_paras) > 0
            if is_orphan:
                prev = merged_paras[-1]
                p_sentences = _split_sentences_safe(p.text)
                prev_sentences = _split_sentences_safe(prev.text)
                prev_norms = {_normalize_for_dedup(s) for s in prev_sentences}
                kept_p = [s for s in p_sentences if _normalize_for_dedup(s) not in prev_norms]
                if not kept_p:
                    changed = True
                    continue
                merged_text = prev.text.rstrip() + " " + " ".join(kept_p)
                merged_supports = list(prev.cited_support_ids) + [
                    sid for sid in p.cited_support_ids if sid not in prev.cited_support_ids
                ]
                merged_claims = tuple(list(prev.claims) + list(p.claims))
                merged_paras[-1] = ArticleParagraph(
                    text=merged_text,
                    cited_support_ids=tuple(merged_supports),
                    claims=merged_claims,
                    generation_origin=prev.generation_origin,
                )
                changed = True
                logger.info(
                    "Merged orphan single-sentence paragraph into preceding: %.60s...",
                    p.text[:60],
                )
            else:
                merged_paras.append(p)
        new_sections.append(
            ArticleSection(
                heading=sec.heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(merged_paras),
                heading_generation_origin=sec.heading_generation_origin,
            )
        )
    if not changed:
        return draft
    return StructuredArticleDraft(
        title=draft.title,
        title_support_ids=draft.title_support_ids,
        lead=draft.lead,
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _sanitize_phantom_heading_topics(
    draft: StructuredArticleDraft,
    heading_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext | None = None,
) -> StructuredArticleDraft:
    """Deterministically sanitize section headings that have phantom topics, invalid support policies, or question overclaims."""
    bad_h_ids = {
        iss.unit_id
        for iss in heading_violations
        if iss.code
        in ("PHANTOM_HEADING_TOPIC", "INVALID_SUPPORT_POLICY", "QUESTION_CONTEXT_OVERCLAIM")
    }
    if not bad_h_ids:
        return draft

    changed = False
    new_sections: list[ArticleSection] = []
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        if h_id in bad_h_ids:
            cur_heading = sec.heading
            cur_sups: tuple[str, ...] = tuple(sec.heading_support_ids)

            # 1. If heading has phantom topic after colon, sanitize prefix
            has_phantom = any(
                iss.unit_id == h_id and iss.code == "PHANTOM_HEADING_TOPIC"
                for iss in heading_violations
            )
            if has_phantom and ":" in cur_heading:
                prefix = cur_heading.split(":", 1)[0].strip()
                if prefix and len(prefix) >= 5:
                    cur_heading = prefix
                    changed = True

            # 2. If heading has INVALID_SUPPORT_POLICY or QUESTION_CONTEXT_OVERCLAIM,
            # borrow valid PUBLISH / CURRENT_WINDOW supports from paragraphs in this section
            needs_sup_repair = any(
                iss.unit_id == h_id
                and iss.code in ("INVALID_SUPPORT_POLICY", "QUESTION_CONTEXT_OVERCLAIM")
                for iss in heading_violations
            )
            if needs_sup_repair and context is not None:
                valid_para_sups = []
                for p in sec.paragraphs:
                    for sid in p.cited_support_ids:
                        if sid in context.support_by_id:
                            sup = context.support_by_id[sid]
                            if sup.publication_use == "PUBLISH":
                                valid_para_sups.append(sid)
                if valid_para_sups:
                    cur_sups = tuple(dict.fromkeys(valid_para_sups[:3]))
                    changed = True
                    logger.info(
                        "Re-anchored heading %s supports from section paragraphs: %s",
                        h_id,
                        cur_sups,
                    )

            if changed:
                new_claims = tuple(
                    c for c in sec.heading_claims if c.text and c.text in cur_heading
                )
                new_sections.append(
                    ArticleSection(
                        heading=cur_heading,
                        heading_support_ids=cur_sups,
                        heading_claims=new_claims,
                        paragraphs=sec.paragraphs,
                        cited_evidence_ids=sec.cited_evidence_ids,
                        heading_generation_origin=sec.heading_generation_origin,
                    )
                )
                logger.info(
                    "Sanitized heading %s: '%s' with %d supports",
                    h_id,
                    cur_heading,
                    len(cur_sups),
                )
                continue
        new_sections.append(sec)

    if not changed:
        return draft

    return StructuredArticleDraft(
        title=draft.title,
        title_support_ids=draft.title_support_ids,
        lead=draft.lead,
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _deduplicate_draft_content(
    draft: StructuredArticleDraft,
) -> StructuredArticleDraft:
    """Deduplicate repeated sentences within paragraphs and duplicate paragraphs across sections."""
    from src.publication.article_claims import _stem

    tok_re = re.compile(r"[\w-]+", re.UNICODE)
    changed = False
    all_prior_paragraphs: list[ArticleParagraph] = []
    new_sections: list[ArticleSection] = []

    for sec in draft.sections:
        new_paras: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            # 1. Deduplicate sentences within paragraph
            sentences = _split_sentences_safe(p.text)
            if len(sentences) > 1:
                seen_sent_norms: set[str] = set()
                deduped_s: list[str] = []
                for s in sentences:
                    s_norm = _normalize_for_dedup(s)
                    if s_norm in seen_sent_norms:
                        changed = True
                        continue
                    seen_sent_norms.add(s_norm)
                    deduped_s.append(s)
                if len(deduped_s) < len(sentences):
                    new_text = " ".join(deduped_s)
                    new_claims = tuple(
                        ArticleClaimAtom(text=s, cited_support_ids=p.cited_support_ids)
                        for s in deduped_s
                    )
                    p = ArticleParagraph(
                        text=new_text,
                        cited_support_ids=p.cited_support_ids,
                        claims=new_claims,
                        generation_origin=p.generation_origin,
                    )

            # 2. Check for duplicate paragraph across current section and previous sections
            p_norm = _normalize_for_dedup(p.text)
            p_stems = {_stem(w.lower()) for w in tok_re.findall(p_norm) if len(w) >= 3}
            is_dup = False
            for existing in all_prior_paragraphs + new_paras:
                e_norm = _normalize_for_dedup(existing.text)
                if p_norm == e_norm or (len(p_norm) >= 20 and p_norm in e_norm):
                    is_dup = True
                    break
                e_stems = {_stem(w.lower()) for w in tok_re.findall(e_norm) if len(w) >= 3}
                if len(p_stems) >= 4 and len(e_stems) >= 4:
                    p_nums = set(re.findall(r"\b\d+\b", p.text))
                    e_nums = set(re.findall(r"\b\d+\b", existing.text))
                    if not (p_nums and e_nums and p_nums != e_nums):
                        overlap = len(p_stems & e_stems) / len(p_stems)
                        if p_stems.issubset(e_stems) or overlap >= 0.85:
                            is_dup = True
                            break

            if is_dup and len(sec.paragraphs) > 1:
                changed = True
                logger.info(
                    "Omitted duplicate cross-section or in-section paragraph: %.60s...",
                    p.text[:60],
                )
            else:
                new_paras.append(p)

        if not new_paras and sec.paragraphs:
            new_paras.append(sec.paragraphs[0])

        all_prior_paragraphs.extend(new_paras)
        new_sections.append(
            ArticleSection(
                heading=sec.heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(new_paras),
                cited_evidence_ids=sec.cited_evidence_ids,
                heading_generation_origin=sec.heading_generation_origin,
            )
        )

    if not changed:
        return draft

    return StructuredArticleDraft(
        title=draft.title,
        title_support_ids=draft.title_support_ids,
        lead=draft.lead,
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


_QUOTE_RE = re.compile(r"\u00ab[^\u00bb]+\u00bb")


def _detect_chat_roll_paragraphs(
    draft: StructuredArticleDraft,
    max_quotes_per_paragraph: int = 3,
) -> list[tuple[int, int, int]]:
    """Detect paragraphs with excessive consecutive direct quotes (AGENTS.md §0.6).

    Returns list of (section_idx, paragraph_idx, quote_count) for paragraphs
    exceeding the quote threshold. These are logged as warnings for editorial review.
    """
    violations: list[tuple[int, int, int]] = []
    for si, sec in enumerate(draft.sections):
        for pi, para in enumerate(sec.paragraphs):
            quote_count = len(_QUOTE_RE.findall(para.text))
            if quote_count > max_quotes_per_paragraph:
                violations.append((si, pi, quote_count))
                logger.warning(
                    "Chat-roll detected: section %d paragraph %d has %d direct quotes "
                    "(max %d per AGENTS.md 0.6). Consider rewriting with indirect speech. "
                    "Text preview: %.80s...",
                    si,
                    pi,
                    quote_count,
                    max_quotes_per_paragraph,
                    para.text[:80],
                )
    return violations


def _prune_unsupported_paragraph_claims(
    draft: StructuredArticleDraft,
    claim_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
) -> StructuredArticleDraft:
    """Deterministically prune ungrounded speculative sentences or invalid paragraphs (AGENTS.md 0.7)."""
    bad_claims_by_unit: dict[str, set[str]] = {}
    problematic_units: set[str] = set()
    for iss in claim_violations:
        if not iss.blocking or not iss.unit_id.startswith("P"):
            continue
        problematic_units.add(iss.unit_id)
        bad_text = getattr(iss, "claim_text", None)
        if not bad_text:
            match = re.search(r"claim(?: atom)? '([^']+)'", iss.message)
            if match:
                bad_text = match.group(1)
        if bad_text:
            bad_claims_by_unit.setdefault(iss.unit_id, set()).add(bad_text.strip())
        if getattr(iss, "unsupported_claims", None):
            for uc in iss.unsupported_claims:
                raw = getattr(uc, "raw", None)
                if raw:
                    bad_claims_by_unit.setdefault(iss.unit_id, set()).add(str(raw).strip())

    if not problematic_units:
        return draft

    p_idx = 1
    new_sections: list[ArticleSection] = []
    all_prior_paragraphs: list[ArticleParagraph] = []
    pruned_count = 0
    for sec in draft.sections:
        new_paragraphs: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            p_idx += 1
            if p_id not in problematic_units:
                new_paragraphs.append(p)
                continue

            bad_texts = bad_claims_by_unit.get(p_id, set())
            sentences = _split_sentences_safe(p.text)
            kept_sentences: list[str] = []
            if len(sentences) > 1:
                for s in sentences:
                    s_clean = s.strip()
                    is_bad = bool(bad_texts) and any(
                        bad == s_clean or bad in s_clean or s_clean in bad for bad in bad_texts
                    )
                    if is_bad:
                        pruned_count += 1
                    else:
                        kept_sentences.append(s_clean)

                # Deduplicate identical sentences within the paragraph
                seen_sent_norms: set[str] = set()
                deduped_kept: list[str] = []
                for s in kept_sentences:
                    s_norm = _normalize_for_dedup(s)
                    if s_norm in seen_sent_norms:
                        pruned_count += 1
                        continue
                    seen_sent_norms.add(s_norm)
                    deduped_kept.append(s)
                kept_sentences = deduped_kept

            has_unsupported_name = any(
                getattr(iss, "code", "") == "UNSUPPORTED_PROPER_NAME"
                for iss in claim_violations
                if iss.unit_id == p_id
            )

            # If no sentences kept (or single-sentence invalid paragraph), synthesize from support evidence
            # unless it has an unverified proper name which must fail closed per AGENTS.md 0.7 & test_case_8.
            if not kept_sentences and not has_unsupported_name and context and p.cited_support_ids:
                from src.publication.article_recovery import _clean_support_text_for_reader

                existing_norms = {
                    _normalize_for_dedup(para.text) for s in new_sections for para in s.paragraphs
                } | {_normalize_for_dedup(para.text) for para in new_paragraphs}

                from src.publication.article_claims import _stem

                tok_re = re.compile(r"[\w-]+", re.UNICODE)
                sec_stems = {
                    _stem(w.lower())
                    for para in new_paragraphs
                    for w in tok_re.findall(para.text)
                    if len(w) >= 3
                }

                sup_texts = [
                    context.support_by_id[sid].text
                    for sid in p.cited_support_ids
                    if sid in context.support_by_id and context.support_by_id[sid].text
                ]
                safe_s = []
                for st in sup_texts[:2]:
                    norm = _normalize_for_dedup(st)
                    if norm in existing_norms:
                        continue
                    st_stems = {_stem(w.lower()) for w in tok_re.findall(norm) if len(w) >= 3}
                    if len(st_stems) >= 2 and st_stems.issubset(sec_stems):
                        # Topic already thoroughly covered in this section
                        continue
                    cleaned = _clean_support_text_for_reader(st)
                    if cleaned:
                        safe_s.append(cleaned)

                if safe_s:
                    kept_sentences = safe_s
                    pruned_count += 1

            if kept_sentences:
                new_text = " ".join(kept_sentences)
                # Deduplicate against existing paragraphs in the section:
                # If new_text is substantially redundant or already expressed, omit it.
                new_norm = _normalize_for_dedup(new_text)
                is_duplicate = False
                for ep in new_paragraphs:
                    ep_norm = _normalize_for_dedup(ep.text)
                    if new_norm in ep_norm or ep_norm in new_norm:
                        is_duplicate = True
                        break
                    from src.publication.article_claims import _stem

                    tok_re = re.compile(r"[\w-]+", re.UNICODE)
                    new_stems = {_stem(w.lower()) for w in tok_re.findall(new_norm) if len(w) >= 3}
                    ep_stems = {_stem(w.lower()) for w in tok_re.findall(ep_norm) if len(w) >= 3}
                    if len(new_stems) >= 3 and new_stems.issubset(ep_stems):
                        is_duplicate = True
                        break

                if is_duplicate:
                    pruned_count += 1
                    continue

                new_claims = tuple(
                    ArticleClaimAtom(text=s, cited_support_ids=p.cited_support_ids)
                    for s in kept_sentences
                )
                new_paragraphs.append(
                    ArticleParagraph(
                        text=new_text,
                        cited_support_ids=p.cited_support_ids,
                        claims=new_claims,
                        generation_origin=p.generation_origin,
                    )
                )
                continue

            # Check if paragraph can be omitted if it is entirely invalid.
            # Fail-closed for DEVELOP stories or unverified proper names (remains to reject per test_case_8).
            # Non-DEVELOP stories with unsupported claim atoms in a substantial draft can be pruned
            # to uphold the Evidence Boundary and allow deterministic recovery per AGENTS.md 0.7.
            is_develop_para = False
            if coverage_plan and getattr(coverage_plan, "stories", None):
                dev_sups = {
                    sid
                    for s in coverage_plan.stories
                    if getattr(s, "prominence", "") == "DEVELOP"
                    for sid in s.support_ids
                }
                if any(sid in dev_sups for sid in p.cited_support_ids):
                    is_develop_para = True

            if not is_develop_para and not has_unsupported_name and len(draft.sections) >= 2:
                # Do not prune entire paragraph if it is the only paragraph in the section,
                # as that would drop the whole section and its topic.
                if len(sec.paragraphs) > 1:
                    pruned_count += 1
                    logger.info(
                        "Pruned entire invalid non-DEVELOP paragraph %s (%d chars) to uphold Evidence Boundary",
                        p_id,
                        len(p.text),
                    )
                    continue

            # Cannot prune DEVELOP paragraph or unverified proper name without violating fail-closed boundary
            new_paragraphs.append(p)

        if new_paragraphs:
            # Deduplicate any duplicate paragraphs within the section and across earlier sections
            from src.publication.article_claims import _stem

            tok_re = re.compile(r"[\w-]+", re.UNICODE)
            deduped_paragraphs: list[ArticleParagraph] = []
            for para in new_paragraphs:
                p_norm = _normalize_for_dedup(para.text)
                dup = False
                for existing in all_prior_paragraphs + deduped_paragraphs:
                    e_norm = _normalize_for_dedup(existing.text)
                    if p_norm in e_norm or e_norm in p_norm:
                        dup = True
                        break
                    p_stems = {_stem(w.lower()) for w in tok_re.findall(p_norm) if len(w) >= 3}
                    e_stems = {_stem(w.lower()) for w in tok_re.findall(e_norm) if len(w) >= 3}
                    if len(p_stems) >= 3 and len(e_stems) >= 3:
                        overlap = len(p_stems & e_stems) / len(p_stems)
                        if p_stems.issubset(e_stems) or overlap >= 0.75:
                            dup = True
                            break
                if not dup or (len(new_paragraphs) == 1 and not deduped_paragraphs):
                    deduped_paragraphs.append(para)
                else:
                    pruned_count += 1

            if deduped_paragraphs:
                all_prior_paragraphs.extend(deduped_paragraphs)
                new_sections.append(
                    ArticleSection(
                        heading=sec.heading,
                        heading_support_ids=sec.heading_support_ids,
                        heading_claims=sec.heading_claims,
                        paragraphs=tuple(deduped_paragraphs),
                        cited_evidence_ids=sec.cited_evidence_ids,
                        heading_generation_origin=sec.heading_generation_origin,
                    )
                )

    if pruned_count == 0:
        return draft

    return StructuredArticleDraft(
        title=draft.title,
        title_support_ids=draft.title_support_ids,
        lead=draft.lead,
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=0,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


class ArticleFinalizer:
    """State machine that finalizes the single writer output and performs deterministic recovery."""

    def __init__(self, composer: ArticleDeterministicComposer | None = None) -> None:
        self.composer = composer or ArticleDeterministicComposer()

    async def finalize(
        self,
        *,
        writer_draft: StructuredArticleDraft | None,
        writer_error: Exception | None,
        writer_attempt_id: int,
        context: ArticleEditorialContext,
        coverage_plan: ArticleCoveragePlan,
        editorial_config: PublicationEditorialConfig,
        length_profile: ArticleLengthProfile | None = None,
        attempt_observer: GenerationAttemptObserver | None = None,
        writer_metadata: dict[str, Any] | None = None,
        writer_validation: ArticleValidationResult | None = None,
    ) -> ArticleFinalizationResult:
        """Validate writer output, trigger recovery if needed, and assert final invariants.

        ``writer_validation`` is produced immediately after parsing the writer
        response. Reusing it avoids repeating the expensive Evidence Boundary
        pass when no writer-side repair changed the draft.
        """
        # 1. Handle writer failure / error
        if writer_error is not None or writer_draft is None:
            if attempt_observer:
                err_meta: dict[str, Any] = {
                    "writer_status": "failed",
                    "error": str(writer_error) if writer_error else "empty writer response",
                }
                if writer_metadata:
                    err_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata=err_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="writer_failed",
                    message=f"Article writer failed: {writer_error}",
                    metadata={
                        "error": str(writer_error) if writer_error else "empty writer response"
                    },
                )
            return await self._run_full_fallback(
                writer_status="failed",
                ai_diag=None,
                ai_covered_story_ids=(),
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

        # 2. Validate writer draft
        if writer_validation is None:
            writer_validation = validate_article_draft(
                writer_draft,
                context,
                config=editorial_config,
                length_profile=length_profile,
            )

        if not writer_validation.is_valid:
            # Deterministic quote repair: convert unverified quotes to indirect speech without quotes
            quote_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking and iss.code == "UNSUPPORTED_DIRECT_QUOTE"
            ]
            if quote_violations:
                repaired_draft = _sanitize_unsupported_quotes(
                    writer_draft, quote_violations, context
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if repaired_val.is_valid:
                    logger.info(
                        "Article writer draft repaired by converting %d unverified quote(s) to indirect speech",
                        len(quote_violations),
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val

        if not writer_validation.is_valid:
            # Deterministic sentence pruning per AGENTS.md 0.7:
            # Prune ungrounded speculative sentences from multi-sentence paragraphs if verified sentences remain.
            claim_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking and iss.unit_id.startswith("P")
            ]
            if claim_violations:
                repaired_draft = _prune_unsupported_paragraph_claims(
                    writer_draft,
                    claim_violations,
                    context,
                    coverage_plan=coverage_plan,
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if repaired_val.is_valid or len(repaired_val.violations) < len(
                    writer_validation.violations
                ):
                    logger.info(
                        "Article writer draft repaired by pruning unsupported claim atom(s) from multi-sentence paragraph(s)"
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val
                else:
                    logger.warning(
                        "Article writer draft pruning attempted but repaired draft still invalid: %s",
                        list(repaired_val.violations),
                    )

        if not writer_validation.is_valid:
            # Deterministic heading repair: sanitize headings with phantom topics, invalid support policies or question overclaims
            heading_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking
                and iss.unit_id.startswith("H")
                and iss.code
                in ("PHANTOM_HEADING_TOPIC", "INVALID_SUPPORT_POLICY", "QUESTION_CONTEXT_OVERCLAIM")
            ]
            if heading_violations:
                repaired_draft = _sanitize_phantom_heading_topics(
                    writer_draft, heading_violations, context=context
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if repaired_val.is_valid or len(repaired_val.violations) < len(
                    writer_validation.violations
                ):
                    logger.info(
                        "Article writer draft repaired by sanitizing %d heading violation(s)",
                        len(heading_violations),
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val

        if not writer_validation.is_valid:
            # Deterministic title repair: sanitize or adopt verified section heading and clean up supports
            title_violations = [
                iss for iss in writer_validation.issues if iss.blocking and iss.unit_id == "TITLE"
            ]
            if title_violations:
                candidate_title = None
                candidate_sups = writer_draft.title_support_ids
                if writer_draft.sections and writer_draft.sections[0].heading:
                    candidate_title = writer_draft.sections[0].heading
                    candidate_sups = writer_draft.sections[0].heading_support_ids or candidate_sups
                if not candidate_title or candidate_title.upper() in ("[DELETE]", "DELETE", ""):
                    candidate_title = re.sub(r"\[.*?\]|\b\d+\b", "", writer_draft.title).strip()
                if candidate_title:
                    candidate_title = re.sub(r"\s+", " ", candidate_title).strip(" -:;,.")
                    from src.publication.article_validator import _CONTINUATION_RE

                    if not _CONTINUATION_RE.search(candidate_title):
                        clean_sups = tuple(
                            sid
                            for sid in candidate_sups
                            if sid in context.support_by_id
                            and context.support_by_id[sid].publication_use == "PUBLISH"
                            and context.support_by_id[sid].temporal_role == "CURRENT_WINDOW"
                        )
                        if not clean_sups:
                            clean_sups = tuple(
                                s.support_id
                                for s in context.supports
                                if s.publication_use == "PUBLISH"
                                and s.temporal_role == "CURRENT_WINDOW"
                            )[:2]
                        candidate_sups = clean_sups or candidate_sups
                    repaired_draft = StructuredArticleDraft(
                        title=candidate_title,
                        title_support_ids=candidate_sups,
                        lead=writer_draft.lead,
                        lead_support_ids=writer_draft.lead_support_ids,
                        sections=writer_draft.sections,
                        title_claims=(
                            ArticleClaimAtom(
                                text=candidate_title, cited_support_ids=candidate_sups
                            ),
                        ),
                        lead_claims=writer_draft.lead_claims,
                        word_count=writer_draft.word_count,
                    )
                    repaired_val = validate_article_draft(
                        repaired_draft,
                        context,
                        config=editorial_config,
                        length_profile=length_profile,
                    )
                    if repaired_val.is_valid:
                        logger.info(
                            "Article writer draft repaired by replacing invalid title with verified heading"
                        )
                        writer_draft = repaired_draft
                        writer_validation = repaired_val

        if not writer_validation.is_valid:
            logger.info(
                "Article writer draft failed validation: %s",
                list(writer_validation.violations),
            )
            if attempt_observer:
                val_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    "violations": list(writer_validation.violations),
                    "draft": writer_draft.to_dict(),
                }
                if writer_metadata:
                    val_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata=val_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message=f"Article writer draft failed validation: {list(writer_validation.violations)}",
                    metadata={
                        "violations": list(writer_validation.violations),
                        "draft": writer_draft.to_dict(),
                    },
                )
            return await self._run_full_fallback(
                writer_status="rejected",
                ai_diag=None,
                ai_covered_story_ids=(),
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

        # 3. Writer draft is valid; apply deterministic structural improvements
        # 3a. Deduplicate identical sentences within paragraphs and duplicate cross-section paragraphs
        writer_draft = _deduplicate_draft_content(writer_draft)

        # 3b. Merge single-sentence orphan paragraphs (AGENTS.md §0.9)
        writer_draft = _merge_orphan_paragraphs(writer_draft)

        # Structural finalization changes the reader-facing draft. Never reuse
        # the validation result from the pre-merge object: the exact object
        # rendered below must pass the Evidence Boundary itself.
        final_validation = validate_article_draft(
            writer_draft,
            context,
            config=editorial_config,
            length_profile=length_profile,
        )
        if not final_validation.is_valid:
            logger.warning(
                "Finalized article draft failed re-validation: %s",
                list(final_validation.violations),
            )
            if attempt_observer:
                final_val_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    "violations": list(final_validation.violations),
                    "draft": writer_draft.to_dict(),
                    "stage": "post_finalization_validation",
                }
                if writer_metadata:
                    final_val_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata=final_val_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message=(
                        "Finalized article draft failed Evidence Boundary validation: "
                        f"{list(final_validation.violations)}"
                    ),
                    metadata={
                        "violations": list(final_validation.violations),
                        "stage": "post_finalization_validation",
                        "draft": writer_draft.to_dict(),
                    },
                )
            return await self._run_full_fallback(
                writer_status="rejected",
                ai_diag=None,
                ai_covered_story_ids=(),
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )
        writer_validation = final_validation

        # 3b. Detect chat-roll patterns for editorial logging (AGENTS.md §0.6)
        _detect_chat_roll_paragraphs(writer_draft, max_quotes_per_paragraph=3)

        # 3c. Diagnose coverage
        ai_diag = diagnose_article_coverage(writer_draft, coverage_plan, context=context)
        ai_covered = tuple(ai_diag.covered_story_ids)

        if set(ai_covered) == set(coverage_plan.story_ids):
            # Complete AI coverage!
            trace = build_article_claim_trace(writer_draft, context)
            final_diag = ai_diag
            meta = _build_final_metadata(
                winning_kind="event_article_writer",
                writer_status="passed",
                recovery_mode="none",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
            )
            if writer_metadata:
                meta.update(writer_metadata)
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=writer_draft,
                claim_trace=trace,
                writer_status="passed",
                recovery_mode="none",
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                metadata=meta,
            )

        # Coverage is a quality diagnostic, not a second factual-verification
        # gate.  Do not append raw fragments or reject a grounded city-life
        # article merely because the writer did not mention every BRIEF/WEAVE
        # item in a single cohesive narrative.
        else:
            logger.info(
                "Accepting grounded article with partial coverage "
                "(coverage=%.2f, missing=%d); coverage recorded diagnostically",
                ai_diag.story_coverage,
                len(ai_diag.uncovered_story_ids),
            )
            trace = build_article_claim_trace(writer_draft, context)
            meta = _build_final_metadata(
                winning_kind="event_article_writer",
                writer_status="passed",
                recovery_mode="none",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                ai_diag=ai_diag,
                final_diag=ai_diag,
                trace=trace,
            )
            meta["coverage_only_diagnostic"] = True
            if writer_metadata:
                meta.update(writer_metadata)
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=writer_draft,
                claim_trace=trace,
                writer_status="passed",
                recovery_mode="none",
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                metadata=meta,
            )

    async def _run_full_fallback(
        self,
        *,
        writer_status: Literal["passed", "rejected", "failed"],
        ai_diag: ArticleCoverageDiagnostics | None,
        ai_covered_story_ids: tuple[str, ...],
        context: ArticleEditorialContext,
        coverage_plan: ArticleCoveragePlan,
        editorial_config: PublicationEditorialConfig,
        length_profile: ArticleLengthProfile | None,
        attempt_observer: GenerationAttemptObserver | None,
    ) -> ArticleFinalizationResult:
        fb_attempt_id = 0
        if attempt_observer:
            fb_attempt_id = await attempt_observer.attempt_started("deterministic_fallback")

        try:
            fallback = self.composer.render_full_fallback(
                context, coverage_plan, max_sections=editorial_config.article_max_sections
            )
            fb_validation = validate_article_draft(
                fallback,
                context,
                config=editorial_config,
                length_profile=length_profile,
            )
            if not fb_validation.is_valid:
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback failed validation: {list(fb_validation.violations)}"
                )

            trace = build_article_claim_trace(fallback, context)
            final_diag = diagnose_article_coverage(fallback, coverage_plan, context=context)
            final_covered = tuple(final_diag.covered_story_ids)

            if (
                set(final_covered) != set(coverage_plan.story_ids)
                or final_diag.story_coverage != 1.0
            ):
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback coverage incomplete: {final_covered} vs {coverage_plan.story_ids}"
                )

            meta = _build_final_metadata(
                winning_kind="event_article_deterministic_fallback",
                writer_status=writer_status,
                recovery_mode="full_fallback",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered_story_ids,
                supplemented_story_ids=(),
                final_covered_story_ids=final_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
            )
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    fb_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=fallback,
                claim_trace=trace,
                writer_status=writer_status,
                recovery_mode="full_fallback",
                ai_covered_story_ids=ai_covered_story_ids,
                supplemented_story_ids=(),
                final_covered_story_ids=final_covered,
                metadata=meta,
            )
        except Exception as exc:
            if attempt_observer and fb_attempt_id:
                await attempt_observer.attempt_finished(
                    fb_attempt_id,
                    status="failed",
                    metadata={"error": str(exc)},
                )
            if isinstance(exc, ArticleFinalizationInvariantError):
                raise
            raise ArticleFinalizationInvariantError(
                f"Failed to finalize deterministic article fallback: {exc}"
            ) from exc
