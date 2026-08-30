from __future__ import annotations

from dataclasses import dataclass

from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_writer_context import _PHONE_RE, _URL_RE


@dataclass(frozen=True)
class ArticleCoverageDiagnostics:
    planned_story_count: int
    covered_story_count: int
    uncovered_story_ids: tuple[str, ...]
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


def diagnose_article_coverage(
    draft: StructuredArticleDraft,
    plan: ArticleCoveragePlan,
) -> ArticleCoverageDiagnostics:
    """Compute non-blocking coverage, prominence ratios, microdetail retention, and contact leaks."""
    cited_ids = _collect_cited_support_ids(draft)

    planned_story_count = len(plan.stories)
    covered_story_ids: list[str] = []
    uncovered_story_ids: list[str] = []

    for item in plan.stories:
        if any(sup_id in cited_ids for sup_id in item.support_ids):
            covered_story_ids.append(item.story_id)
        else:
            uncovered_story_ids.append(item.story_id)

    covered_story_count = len(covered_story_ids)

    # Prominence ratios
    def _ratio_for_prominence(prominence: str) -> float:
        subset = [s for s in plan.stories if s.prominence == prominence]
        if not subset:
            return 1.0
        cov = sum(1 for s in subset if any(sup_id in cited_ids for sup_id in s.support_ids))
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
        if did in cited_ids:
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
        uncovered_story_ids=tuple(uncovered_story_ids),
        develop_story_coverage=develop_coverage,
        weave_story_coverage=weave_coverage,
        brief_story_coverage=brief_coverage,
        planned_detail_support_count=planned_detail_count,
        covered_detail_support_count=covered_detail_count,
        uncovered_detail_support_ids=tuple(uncovered_details),
        detail_support_coverage=detail_coverage,
        leaked_contact_payloads=leaked_contacts,
    )
