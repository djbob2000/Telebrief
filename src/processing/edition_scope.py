"""Edition geographic scope types, hashing, resolver, and contract definition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

import psycopg

from src.config_loader import Config, EditionScopeConfig
from src.domain.edition_geography import (
    EditionGeographyContext,
    resolve_edition_geography,
)

SCOPE_VERSION = "v1"
EditionScopeClass = Literal["LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"]


@dataclass(frozen=True)
class EditionScopeDecision:
    story_id: int
    scope: EditionScopeClass
    confidence: float
    reason: str


def scope_config_payload(scope: EditionScopeConfig) -> dict[str, object]:
    return {
        "name": scope.name,
        "focus_places": list(scope.focus_places),
        "direct_impact_only": scope.direct_impact_only,
        "notes": list(scope.notes),
    }


def scope_config_hash(scope: EditionScopeConfig) -> str:
    raw = json.dumps(
        scope_config_payload(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def resolve_edition_scope(
    conn: psycopg.AsyncConnection,
    config: Config,
    edition_id: int,
) -> tuple[str, EditionScopeConfig]:
    cur = await conn.execute("SELECT slug, name FROM editions WHERE id = %s", (edition_id,))
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"edition {edition_id} not found")
    slug = str(row[0])
    name = str(row[1]) if len(row) > 1 and row[1] else slug.capitalize()
    scope = config.settings.edition_scopes.get(slug)
    if scope is None:
        geo = resolve_edition_geography(slug, name)
        scope = EditionScopeConfig(
            name=geo.edition_name,
            focus_places=geo.target_locations,
            direct_impact_only=False,
            notes=(),
        )
    return slug, scope


def build_scope_contract(
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None = None,
) -> str:
    focus_list = ", ".join(scope.focus_places)
    notes_block = ""
    if scope.notes:
        notes_block = "\n" + "\n".join(f"- {note}" for note in scope.notes)

    geo_block = ""
    if geo_context is not None:
        geo_block = "\n\n" + geo_context.to_prompt_section()

    return f"""GEOGRAPHIC SCOPE RULES:
- LOCAL: event occurs in one of the configured focus places ({focus_list}).
- DIRECT_IMPACT: event occurs outside the focus places but evidence explicitly states a concrete consequence inside them.
- OUT_OF_SCOPE: external event with no explicit concrete local consequence.
- UNCERTAIN: available evidence is insufficient to establish LOCAL or DIRECT_IMPACT.

CRITICAL INVARIANTS:
1. Source membership alone is not evidence of locality.
2. Same oblast/region/nation, broad strategic relevance, and similarly named military directions are not DIRECT_IMPACT by themselves (same region is not enough).
3. Explicit external geography overrides a local-source assumption unless a concrete local consequence is stated.
Target Edition: {scope.name}
Focus Places: {focus_list}{notes_block}{geo_block}"""
