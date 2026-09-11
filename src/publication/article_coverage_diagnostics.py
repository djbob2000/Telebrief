from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from src.publication.article_claims import _stem
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_writer_context import _PHONE_RE, _URL_RE


@dataclass(frozen=True)
class ArticleCoverageDiagnostics:
    planned_story_count: int
    covered_story_count: int
    covered_story_ids: tuple[str, ...]
    uncovered_story_ids: tuple[str, ...]
    story_coverage: float
    develop_story_coverage: float
    weave_story_coverage: float
    brief_story_coverage: float
    planned_detail_support_count: int
    covered_detail_support_count: int
    uncovered_detail_support_ids: tuple[str, ...]
    detail_support_coverage: float
    leaked_contact_payloads: tuple[str, ...]


def _collect_cited_support_ids(draft: StructuredArticleDraft) -> set[str]:
    cited: set[str] = set()
    cited.update(draft.title_support_ids)
    for c in draft.title_claims:
        cited.update(c.cited_support_ids)

    cited.update(draft.lead_support_ids)
    for c in draft.lead_claims:
        cited.update(c.cited_support_ids)

    for sec in draft.sections:
        cited.update(sec.heading_support_ids)
        for c in sec.heading_claims:
            cited.update(c.cited_support_ids)
        for p in sec.paragraphs:
            cited.update(p.cited_support_ids)
            for c in p.claims:
                cited.update(c.cited_support_ids)

    return cited


def _extract_contact_leaks(draft: StructuredArticleDraft) -> tuple[str, ...]:
    texts = [draft.title, draft.lead]
    for sec in draft.sections:
        texts.append(sec.heading)
        for p in sec.paragraphs:
            texts.append(p.text)

    full_text = "\n".join(texts)
    leaks: list[str] = []
    for match in _PHONE_RE.finditer(full_text):
        m_str = match.group(0).strip()
        if m_str and m_str not in leaks:
            leaks.append(m_str)
    for match in _URL_RE.finditer(full_text):
        m_str = match.group(0).strip()
        if m_str and m_str not in leaks:
            leaks.append(m_str)
    return tuple(leaks)


def _claim_supports_story(claim_text: str, subset_supports: Sequence[Any]) -> bool:
    """Verify that a claim text actually represents/supports the story via semantic grounding."""
    if not subset_supports or not claim_text:
        return False

    tok_re = re.compile(r"[\w-]+", re.UNICODE)

    claim_words = tok_re.findall(claim_text)
    claim_stems = {_stem(w.lower()) for w in claim_words if len(w) >= 3}
    claim_nums = set(re.findall(r"\b\d+\b", claim_text))

    support_texts = [getattr(s, "text", "") for s in subset_supports if getattr(s, "text", "")] + [
        getattr(s, "source_text", "") for s in subset_supports if getattr(s, "source_text", "")
    ]
    all_sup_text = " ".join(support_texts)
    sup_words = tok_re.findall(all_sup_text)
    sup_stems = {_stem(w.lower()) for w in sup_words if len(w) >= 3}
    sup_nums = set(re.findall(r"\b\d+\b", all_sup_text))

    shared_stems = claim_stems & sup_stems
    shared_nums = claim_nums & sup_nums

    return (
        len(shared_stems) >= 2
        or (bool(shared_stems) and bool(shared_nums))
        or len(shared_nums) >= 2
    )


def diagnose_article_coverage(
    draft: StructuredArticleDraft,
    plan: ArticleCoveragePlan,
    context: Any | None = None,
) -> ArticleCoverageDiagnostics:
    """Compute non-blocking coverage, prominence ratios, microdetail retention, and contact leaks.

    Coverage invariant: Citation attachment != story coverage.
    Story coverage credit is derived exclusively from validated claim -> support/story edges.
    Title, headings, and ungrounded wrapper citations do not grant story coverage.
    """
    # Collect candidate claims for story coverage (body paragraphs and lead claims only;
    # title and section heading provenance NEVER grant story coverage).
    candidate_claims = list(draft.lead_claims)
    for sec in draft.sections:
        for p in sec.paragraphs:
            candidate_claims.extend(p.claims)

    planned_story_count = len(plan.stories)
    covered_story_ids_set: set[str] = set()
    covered_claim_sids: set[str] = set()

    for item in plan.stories:
        story_sids = set(item.support_ids) | set(item.detail_support_ids)
        for claim in candidate_claims:
            matching_sids = [sid for sid in claim.cited_support_ids if sid in story_sids]
            if not matching_sids:
                continue

            if context is not None and getattr(context, "support_by_id", None):
                subset_supports = [
                    context.support_by_id[sid]
                    for sid in matching_sids
                    if sid in context.support_by_id
                ]
                if not subset_supports:
                    continue
                if _claim_supports_story(claim.text, subset_supports):
                    covered_story_ids_set.add(item.story_id)
                    covered_claim_sids.update(matching_sids)
            else:
                covered_story_ids_set.add(item.story_id)
                covered_claim_sids.update(matching_sids)

    covered_story_ids = [s.story_id for s in plan.stories if s.story_id in covered_story_ids_set]
    uncovered_story_ids = [
        s.story_id for s in plan.stories if s.story_id not in covered_story_ids_set
    ]
    covered_story_count = len(covered_story_ids)
    story_coverage = covered_story_count / planned_story_count if planned_story_count > 0 else 1.0

    # Prominence ratios
    def _ratio_for_prominence(prominence: str) -> float:
        subset = [s for s in plan.stories if s.prominence == prominence]
        if not subset:
            return 1.0
        cov = sum(1 for s in subset if s.story_id in covered_story_ids_set)
        return cov / len(subset)

    develop_coverage = _ratio_for_prominence("DEVELOP")
    weave_coverage = _ratio_for_prominence("WEAVE")
    brief_coverage = _ratio_for_prominence("BRIEF")

    # Detail supports
    all_planned_details: list[str] = []
    for item in plan.stories:
        all_planned_details.extend(item.detail_support_ids)

    planned_detail_count = len(all_planned_details)
    covered_details: list[str] = []
    uncovered_details: list[str] = []

    for did in all_planned_details:
        if did in covered_claim_sids:
            covered_details.append(did)
        else:
            uncovered_details.append(did)

    covered_detail_count = len(covered_details)
    detail_coverage = (
        covered_detail_count / planned_detail_count if planned_detail_count > 0 else 1.0
    )

    leaked_contacts = _extract_contact_leaks(draft)

    return ArticleCoverageDiagnostics(
        planned_story_count=planned_story_count,
        covered_story_count=covered_story_count,
        covered_story_ids=tuple(covered_story_ids),
        uncovered_story_ids=tuple(uncovered_story_ids),
        story_coverage=story_coverage,
        develop_story_coverage=develop_coverage,
        weave_story_coverage=weave_coverage,
        brief_story_coverage=brief_coverage,
        planned_detail_support_count=planned_detail_count,
        covered_detail_support_count=covered_detail_count,
        uncovered_detail_support_ids=tuple(uncovered_details),
        detail_support_coverage=detail_coverage,
        leaked_contact_payloads=leaked_contacts,
    )
