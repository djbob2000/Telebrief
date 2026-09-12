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


def _claim_supports_story(
    claim_text: str,
    subset_supports: Sequence[Any],
    story_discriminative_stems: set[str] | None = None,
    story_discriminative_nums: set[str] | None = None,
) -> bool:
    """Verify that a claim text actually represents/supports the story via semantic grounding.

    Requires:
    1. Base semantic grounding: >= 2 shared stems, or (1 shared stem + 1 shared num), or >= 2 shared nums.
    2. Story-discriminative anchoring (when discriminatory anchors exist):
       The claim must contain at least one stem or numeric anchor specific to this story
       (i.e. not shared across multiple stories) to prevent generic claims over-crediting omitted stories.
    """
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

    has_base_grounding = (
        len(shared_stems) >= 2
        or (bool(shared_stems) and bool(shared_nums))
        or len(shared_nums) >= 2
    )
    if not has_base_grounding:
        return False

    # Check story-discriminative anchors if available
    if story_discriminative_stems:
        if not (claim_stems & story_discriminative_stems):
            return False
    elif story_discriminative_nums and claim_nums:
        if not (claim_nums & story_discriminative_nums):
            return False

    return True


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
    tok_re = re.compile(r"[\w-]+", re.UNICODE)

    # Pre-extract discriminative anchors per story if context supports are available
    story_disc_stems: dict[str, set[str]] = {}
    story_disc_nums: dict[str, set[str]] = {}

    if context is not None and getattr(context, "support_by_id", None):
        story_all_stems: dict[str, set[str]] = {}
        story_all_nums: dict[str, set[str]] = {}
        for item in plan.stories:
            s_sids = set(item.support_ids) | set(item.detail_support_ids)
            s_texts: list[str] = [item.topic] if item.topic else []
            for sid in s_sids:
                if sid in context.support_by_id:
                    sup = context.support_by_id[sid]
                    if getattr(sup, "text", ""):
                        s_texts.append(sup.text)
                    if getattr(sup, "source_text", ""):
                        s_texts.append(sup.source_text)
            combined_s_text = " ".join(s_texts)
            words = tok_re.findall(combined_s_text)
            stems = {_stem(w.lower()) for w in words if len(w) >= 3}
            nums = set(re.findall(r"\b\d+\b", combined_s_text))
            story_all_stems[item.story_id] = stems
            story_all_nums[item.story_id] = nums

        for item in plan.stories:
            sid = item.story_id
            cur_stems = story_all_stems.get(sid, set())
            cur_nums = story_all_nums.get(sid, set())
            other_stems: set[str] = set()
            other_nums: set[str] = set()
            for other_id in plan.stories:
                if other_id.story_id != sid:
                    other_stems.update(story_all_stems.get(other_id.story_id, set()))
                    other_nums.update(story_all_nums.get(other_id.story_id, set()))
            disc_s = cur_stems - other_stems
            disc_n = cur_nums - other_nums
            if disc_s or disc_n:
                story_disc_stems[sid] = disc_s
                story_disc_nums[sid] = disc_n

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
        disc_s = story_disc_stems.get(item.story_id, set())
        disc_n = story_disc_nums.get(item.story_id, set())
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
                if _claim_supports_story(
                    claim.text,
                    subset_supports,
                    story_discriminative_stems=disc_s,
                    story_discriminative_nums=disc_n,
                ):
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
