"""Article claim trace builder resolving draft units to support IDs and exact source provenance."""

from __future__ import annotations

from dataclasses import dataclass

from src.publication.article_claims import ConcreteClaim, extract_concrete_claims
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import StructuredArticleDraft


@dataclass(frozen=True)
class ArticleClaimTraceUnit:
    """Provenance trace for a single user-visible article unit."""

    unit_id: str
    text: str
    support_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    fragment_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    concrete_claims: tuple[ConcreteClaim, ...]


def build_article_claim_trace(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
) -> tuple[ArticleClaimTraceUnit, ...]:
    """Build exact unit -> support -> source provenance trace for an article draft."""
    support_map = context.support_by_id
    trace_units: list[ArticleClaimTraceUnit] = []

    # Sequence of (unit_id, text, cited_support_ids)
    raw_units: list[tuple[str, str, tuple[str, ...]]] = [
        ("TITLE", draft.title, draft.title_support_ids),
        ("LEAD", draft.lead, draft.lead_support_ids),
    ]

    p_idx = 1
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        raw_units.append((h_id, sec.heading, sec.heading_support_ids))
        for para in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            raw_units.append((p_id, para.text, para.cited_support_ids))
            p_idx += 1

    for unit_id, text, support_ids in raw_units:
        if not text.strip():
            continue

        refs: list[str] = []
        frag_ids: list[int] = []
        item_ids: list[int] = []

        for sid in support_ids:
            if sid in support_map:
                sup = support_map[sid]
                refs.extend(sup.source_refs)
                frag_ids.extend(sup.fragment_ids)
                item_ids.extend(sup.source_item_ids)

        concrete = extract_concrete_claims(text)

        trace_units.append(
            ArticleClaimTraceUnit(
                unit_id=unit_id,
                text=text,
                support_ids=tuple(dict.fromkeys(support_ids)),
                source_refs=tuple(dict.fromkeys(refs)),
                fragment_ids=tuple(dict.fromkeys(frag_ids)),
                source_item_ids=tuple(dict.fromkeys(item_ids)),
                concrete_claims=concrete,
            )
        )

    return tuple(trace_units)
