"""Article claim trace builder resolving draft units to support IDs and exact source provenance."""

from __future__ import annotations

from dataclasses import dataclass

from src.publication.article_claims import ConcreteClaim, extract_concrete_claims
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import ArticleClaimAtom, StructuredArticleDraft


@dataclass(frozen=True)
class ArticleClaimTraceAtom:
    """Exact provenance trace for a single claim atom."""

    text: str
    support_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    fragment_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    temporal_roles: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    source_roles: tuple[str, ...]
    concrete_claims: tuple[ConcreteClaim, ...]


@dataclass(frozen=True)
class ArticleClaimTraceUnit:
    """Provenance trace for a single user-visible article unit."""

    unit_id: str
    text: str
    support_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    fragment_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    temporal_roles: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    source_roles: tuple[str, ...]
    concrete_claims: tuple[ConcreteClaim, ...]
    claim_atoms: tuple[ArticleClaimTraceAtom, ...] = ()


def build_article_claim_trace(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
) -> tuple[ArticleClaimTraceUnit, ...]:
    """Build exact unit -> support -> source provenance trace for an article draft."""
    support_map = context.support_by_id
    trace_units: list[ArticleClaimTraceUnit] = []

    # Sequence of (unit_id, text, cited_support_ids, claim_atoms)
    raw_units: list[tuple[str, str, tuple[str, ...], tuple[ArticleClaimAtom, ...]]] = [
        ("TITLE", draft.title, draft.title_support_ids, draft.title_claims),
        ("LEAD", draft.lead, draft.lead_support_ids, draft.lead_claims),
    ]

    p_idx = 1
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        raw_units.append((h_id, sec.heading, sec.heading_support_ids, sec.heading_claims))
        for para in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            raw_units.append((p_id, para.text, para.cited_support_ids, para.claims))
            p_idx += 1

    for unit_id, text, support_ids, claim_atoms in raw_units:
        if not text.strip():
            continue

        refs: list[str] = []
        frag_ids: list[int] = []
        item_ids: list[int] = []
        roles: list[str] = []
        evidence_kinds: list[str] = []
        source_roles: list[str] = []

        for sid in support_ids:
            if sid in support_map:
                sup = support_map[sid]
                refs.extend(sup.source_refs)
                frag_ids.extend(sup.fragment_ids)
                item_ids.extend(sup.source_item_ids)
                roles.append(sup.temporal_role)
                evidence_kinds.append(sup.evidence_kind)
                source_roles.extend(sup.source_roles)

        concrete = extract_concrete_claims(text)

        trace_atoms: list[ArticleClaimTraceAtom] = []
        for atom in claim_atoms:
            atom_refs: list[str] = []
            atom_frags: list[int] = []
            atom_items: list[int] = []
            atom_roles: list[str] = []
            atom_evidence_kinds: list[str] = []
            atom_source_roles: list[str] = []
            for asid in atom.cited_support_ids:
                if asid in support_map:
                    asup = support_map[asid]
                    atom_refs.extend(asup.source_refs)
                    atom_frags.extend(asup.fragment_ids)
                    atom_items.extend(asup.source_item_ids)
                    atom_roles.append(asup.temporal_role)
                    atom_evidence_kinds.append(asup.evidence_kind)
                    atom_source_roles.extend(asup.source_roles)
            atom_concrete = extract_concrete_claims(atom.text)
            trace_atoms.append(
                ArticleClaimTraceAtom(
                    text=atom.text,
                    support_ids=tuple(dict.fromkeys(atom.cited_support_ids)),
                    source_refs=tuple(dict.fromkeys(atom_refs)),
                    fragment_ids=tuple(dict.fromkeys(atom_frags)),
                    source_item_ids=tuple(dict.fromkeys(atom_items)),
                    temporal_roles=tuple(dict.fromkeys(atom_roles)),
                    evidence_kinds=tuple(dict.fromkeys(atom_evidence_kinds)),
                    source_roles=tuple(dict.fromkeys(atom_source_roles)),
                    concrete_claims=atom_concrete,
                )
            )

        trace_units.append(
            ArticleClaimTraceUnit(
                unit_id=unit_id,
                text=text,
                support_ids=tuple(dict.fromkeys(support_ids)),
                source_refs=tuple(dict.fromkeys(refs)),
                fragment_ids=tuple(dict.fromkeys(frag_ids)),
                source_item_ids=tuple(dict.fromkeys(item_ids)),
                temporal_roles=tuple(dict.fromkeys(roles)),
                evidence_kinds=tuple(dict.fromkeys(evidence_kinds)),
                source_roles=tuple(dict.fromkeys(source_roles)),
                concrete_claims=concrete,
                claim_atoms=tuple(trace_atoms),
            )
        )

    return tuple(trace_units)
