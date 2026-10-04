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
    memberships: tuple[
        tuple[
            str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
        ],
        ...,
    ]


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
        summaries = {u.unit_id for u in planned[bid].composition_units if not u.fact_ids}
        memberships.append(
            (
                bid,
                tuple(sorted(fid for i in items for fid in i.covered_fact_ids)),
                tuple(sorted({uid for i in items for uid in i.composition_unit_ids})),
                tuple(
                    sorted(uid for i in items for uid in i.composition_unit_ids if uid in summaries)
                ),
                tuple(sorted({sid for i in items for sid in i.covered_story_ids})),
                tuple(sorted({sid for i in items for sid in i.cited_support_ids})),
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
) -> None:
    from src.publication.digest_editor import DigestRecompositionError

    if _fingerprint(base) != scope.base_fingerprint or _fingerprint(plan) != scope.plan_fingerprint:
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_FINGERPRINT")
    if tuple(b.block_id for b in base.blocks) != tuple(b.block_id for b in replacement.blocks):
        raise DigestRecompositionError("DIGEST_EDIT_SCOPE_BLOCK_ORDER")
    for old, new in zip(base.blocks, replacement.blocks, strict=True):
        if old.block_id not in scope.block_ids and old != new:
            raise DigestRecompositionError("DIGEST_EDIT_SCOPE_UNAUTHORIZED_BLOCK")
    updated = build_digest_block_edit_scope(replacement, plan=plan, block_ids=scope.block_ids)
    for before, after in zip(scope.memberships, updated.memberships, strict=True):
        if (
            before[0] != after[0]
            or Counter(before[1]) != Counter(after[1])
            or before[2:] != after[2:]
        ):
            raise DigestRecompositionError("DIGEST_EDIT_SCOPE_MEMBERSHIP")
