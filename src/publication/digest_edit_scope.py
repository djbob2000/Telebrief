"""Immutable, fingerprint-bound authorization for whole thematic block edits."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from src.publication.digest_narrative import DigestNarrativeDraft, DigestNarrativePlan


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(asdict(value), sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class DigestBlockEditScope:
    base_fingerprint: str
    plan_fingerprint: str
    block_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    memberships: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]


def build_digest_block_edit_scope(
    draft: DigestNarrativeDraft, *, plan: DigestNarrativePlan, block_ids: Sequence[str]
) -> DigestBlockEditScope:
    from src.publication.digest_editor import DigestRecompositionError

    allowed = tuple(dict.fromkeys(block_ids))
    planned = {b.block_id: b for b in plan.blocks}
    actual = {b.block_id: b for b in draft.blocks}
    if not allowed or not set(allowed).issubset(planned.keys() & actual.keys()):
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_UNKNOWN_BLOCK")
    memberships = []
    item_ids: list[str] = []
    for bid in allowed:
        block = actual[bid]
        items = block.items
        item_ids.extend(i.item_id for i in items)
        planned_block = planned[bid]
        facts = tuple(
            sorted(
                str(fact_id)
                for unit in planned_block.composition_units
                for fact_id in unit.fact_ids
            )
        )
        summaries = tuple(
            sorted(
                str(unit.unit_id) for unit in planned_block.composition_units if not unit.fact_ids
            )
        )
        memberships.append(
            (
                bid,
                facts,
                summaries,
            )
        )
    if len(item_ids) != len(set(item_ids)) or any(not i for i in item_ids):
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_ITEM_IDS")
    return DigestBlockEditScope(
        _fingerprint(draft), _fingerprint(plan), allowed, tuple(item_ids), tuple(memberships)
    )


def validate_digest_block_replacement(
    base: DigestNarrativeDraft,
    replacement: DigestNarrativeDraft,
    *,
    scope: DigestBlockEditScope,
    plan: DigestNarrativePlan,
    allow_incomplete_fact_coverage: bool = False,
    allow_incomplete_summary_coverage: bool = False,
    allow_duplicate_fact_coverage: bool = False,
) -> None:
    from src.publication.digest_editor import DigestRecompositionError

    if _fingerprint(base) != scope.base_fingerprint or _fingerprint(plan) != scope.plan_fingerprint:
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_FINGERPRINT")
    if tuple(b.block_id for b in base.blocks) != tuple(b.block_id for b in replacement.blocks):
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_BLOCK_ORDER")
    for old, new in zip(base.blocks, replacement.blocks, strict=True):
        if old.block_id not in scope.block_ids and old != new:
            raise DigestRecompositionError("DIGEST_EDIT_SCOPE_UNAUTHORIZED_BLOCK")
    plan_blocks = {block.block_id: block for block in plan.blocks}
    replacement_blocks = {block.block_id: block for block in replacement.blocks}
    for block_id, allowed_facts, allowed_summaries in scope.memberships:
        block = replacement_blocks[block_id]
        summary_unit_ids = {
            str(unit.unit_id)
            for unit in plan_blocks[block_id].composition_units
            if not unit.fact_ids
        }
        received_facts = [str(fact_id) for item in block.items for fact_id in item.covered_fact_ids]
        received_summaries = [
            str(unit_id)
            for item in block.items
            for unit_id in (
                item.composition_unit_ids
                or ((item.composition_unit_id,) if item.composition_unit_id else ())
            )
            if str(unit_id) in summary_unit_ids
        ]
        if allow_incomplete_fact_coverage:
            facts_match = (
                set(received_facts) <= set(allowed_facts)
                if allow_duplicate_fact_coverage
                else Counter(received_facts) <= Counter(allowed_facts)
                and len(received_facts) == len(set(received_facts))
            )
        else:
            facts_match = Counter(allowed_facts) == Counter(received_facts)
        if allow_incomplete_summary_coverage:
            summaries_match = Counter(received_summaries) <= Counter(allowed_summaries) and len(
                received_summaries
            ) == len(set(received_summaries))
        else:
            summaries_match = Counter(allowed_summaries) == Counter(received_summaries)
        if not facts_match or not summaries_match:
            raise DigestRecompositionError("DIGEST_EDIT_SCOPE_MEMBERSHIP")
