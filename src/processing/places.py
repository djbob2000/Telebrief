"""Place resolution processing (Plan 3 Task 8): policy identity, the
deterministic alias resolver with an optional LLM assist hook, the
mention-resolution orchestrator, legacy metadata backfill, and the
Berdyansk city-profile importer.

Rulings implemented here:

* :func:`normalize_place_text` is THE single normalization contract:
  lowercase (explicit Cyrillic map — C-locale databases fold nothing beyond
  ASCII), every non-word character becomes a space, whitespace collapses.
  Alias rows store this normalized form; lookups normalize the mention text
  through the same function, so «Округ №32» and «округ 32» collide on one
  key deterministically.
* Deterministic alias lookup is the primary resolver. Multiple candidates
  resolve by preferring an exact canonical_name match and then the lowest
  place id; no candidate at all is a valid explicit ``unresolved`` outcome
  (``place_id IS NULL``), never a failure. The LLM assist stays an optional
  stub: it may only refine among deterministic candidates and inherits the
  same unresolved contract.
* Resolution never mutates Claim rows and always preserves the original
  mention text. A completed result (resolved OR explicit unresolved) under
  the current policy satisfies that mention's side of the story-matching
  prerequisite barrier; after each visible result the orchestrator hands
  the claim to ``StoryMatchingPrerequisiteService.maybe_schedule`` on the
  SAME connection so matching queues exactly when its barrier opens.
* The importer seeds places + aliases from ``data/city_profiles/berdyansk.yaml``
  idempotently (external-id metadata keys + per-place NOT EXISTS aliases).
  The YAML is a SEED source only — never a runtime lookup dependency after
  Places are persisted.

Repositories never commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.places import (
    Place,
    PlaceResolutionPolicyVersion,
    PlaceResolutionRun,
    normalize_place_text,
)
from src.repositories.places import (
    PlaceRepository,
    PlaceResolutionPolicyRepository,
    PlaceResolutionRunRepository,
)

logger = logging.getLogger(__name__)

PLACE_RESOLUTION_PROMPT_VERSION = "places-2026-08-v1"

_ENSURE_CURRENT_RACE_ATTEMPTS = 3


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def place_resolution_config_hash() -> str:
    """Stable identity of resolver-affecting configuration.

    The shipped resolver is fully deterministic (no verdict-affecting runtime
    setting exists); when the optional LLM assist starts shaping outcomes its
    model/prompt identity joins this hash so results stay attributable.
    """
    canonical = json.dumps(
        {"schema": 1, "stage": "place_resolution"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PlaceResolutionPolicyService:
    """Resolve-or-create the edition's current place-resolution policy version.

    Identity is (edition_id, config_hash, prompt_version) with latest-
    version-wins semantics; concurrent creators are arbitrated by
    uq_place_resolution_policy_edition_version via a savepoint re-read loop.
    The nullable ``editions.current_place_policy_id`` pointer records the
    edition's active choice (documented mutable exception).
    """

    def __init__(self, repo: PlaceResolutionPolicyRepository | None = None) -> None:
        self._repo = repo or PlaceResolutionPolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str | None = None,
        prompt_version: str = PLACE_RESOLUTION_PROMPT_VERSION,
    ) -> PlaceResolutionPolicyVersion:
        resolved_hash = config_hash or place_resolution_config_hash()
        for _ in range(_ENSURE_CURRENT_RACE_ATTEMPTS):
            policies = await self._repo.list_for_edition(conn, edition_id)
            for policy in policies:
                if policy.config_hash == resolved_hash and policy.prompt_version == prompt_version:
                    await self._repo.set_edition_pointer(
                        conn, edition_id=edition_id, policy_id=policy.id
                    )
                    return policy

            next_version = max((policy.version for policy in policies), default=0) + 1
            try:
                async with conn.transaction():
                    policy = await self._repo.insert(
                        conn,
                        edition_id=edition_id,
                        version=next_version,
                        config_hash=resolved_hash,
                        prompt_version=prompt_version,
                    )
            except psycopg.errors.UniqueViolation:
                # A concurrent creator inserted version MAX+1 first; converge.
                continue
            await self._repo.set_edition_pointer(conn, edition_id=edition_id, policy_id=policy.id)
            return policy
        raise RuntimeError(
            f"could not ensure current place resolution policy for edition {edition_id} "
            f"after {_ENSURE_CURRENT_RACE_ATTEMPTS} attempts"
        )


class ResolutionChoice:
    """One resolver verdict: a place id (NULL = explicit unresolved) plus
    provenance metadata for the result row."""

    __slots__ = ("place_id", "confidence", "reason")

    def __init__(
        self,
        *,
        place_id: int | None,
        confidence: float | None,
        reason: str,
    ) -> None:
        self.place_id = place_id
        self.confidence = confidence
        self.reason = reason


class MentionResolver(Protocol):
    """The resolver contract: choose among candidates, never mutate anything."""

    async def choose(
        self,
        mention: Any,
        candidates: list,
        *,
        policy_id: int,
    ) -> ResolutionChoice: ...


class AliasResolver:
    """Deterministic primary resolver over seeded alias candidates.

    Scoring is stable and explainable: an alias hit whose place
    ``canonical_name`` equals the normalized mention text wins outright;
    otherwise the lowest place id (repository order) takes it. Zero
    candidates produce the explicit unresolved outcome — resolution stays a
    soft signal and NULL place_id is a COMPLETED result.
    """

    UNRESOLVED_CONFIDENCE = 0.0
    ALIAS_HIT_CONFIDENCE = 0.9
    CANONICAL_HIT_CONFIDENCE = 1.0

    async def choose(self, mention, candidates, *, policy_id: int) -> ResolutionChoice:
        del policy_id  # deterministic scoring needs no policy context yet
        if not candidates:
            return ResolutionChoice(
                place_id=None,
                confidence=self.UNRESOLVED_CONFIDENCE,
                reason="no_alias_match",
            )
        mention_key = normalize_place_text(mention.original_text)
        chosen = candidates[0]
        reason, confidence = "alias_match", self.ALIAS_HIT_CONFIDENCE
        for candidate in candidates:
            if normalize_place_text(candidate.canonical_name) == mention_key:
                chosen = candidate
                reason, confidence = "canonical_alias_match", self.CANONICAL_HIT_CONFIDENCE
                break
        return ResolutionChoice(place_id=chosen.id, confidence=confidence, reason=reason)


class LLMAssistResolver(AliasResolver):
    """Optional refinement hook (STUB, not wired by default).

    Contract when activated: receive the deterministic candidate shortlist
    plus mention context, MAY reorder/annotate among those candidates or
    return the explicit unresolved outcome — it must NEVER invent place ids
    outside the shortlist, and provider outages surface as
    :class:`ProviderUnavailableError` so the task retry loop treats them like
    any other transient AI failure.
    """

    def __init__(self, *, provider: Any, model: str) -> None:  # pragma: no cover
        super().__init__()
        self.provider = provider
        self.model = model


class PlaceResolutionService:
    """Resolve one immutable claim place mention under one exact policy.

    Boundary layout mirrors Task 7: reads happen in their own short
    transactions, the resolver call is pure/local by default (the optional
    LLM assist runs OUTSIDE any transaction once wired), and ONE write
    transaction persists run + canonical result and evaluates the story-
    matching prerequisite barrier on the SAME connection. Duplicate
    executions replay the canonical winner without new rows.
    """

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        resolver: MentionResolver | None = None,
        places: PlaceRepository | None = None,
        policies: PlaceResolutionPolicyRepository | None = None,
        runs: PlaceResolutionRunRepository | None = None,
    ) -> None:
        self.uow = uow
        self._resolver = resolver or AliasResolver()
        self._places = places or PlaceRepository()
        self._policies = policies or PlaceResolutionPolicyRepository()
        self._runs = runs or PlaceResolutionRunRepository()

    async def resolve_mention(self, mention_id: int, policy_id: int):
        """Resolve one mention under the exact queued policy.

        Returns the canonical :class:`PlaceResolutionResult` — either freshly
        written or replayed from an earlier completed execution. ``place_id``
        is None for explicit unresolved outcomes (still COMPLETED).
        """
        async with self.uow.transaction() as conn:
            mention = await self._places.get_mention(conn, mention_id)
            if mention is None:
                raise ValueError(f"claim place mention {mention_id} does not exist")
            policy = await self._policies.get(conn, policy_id)
            if policy is None:
                raise ValueError(f"place resolution policy {policy_id} does not exist")
            existing = await self._places.find_canonical_result(
                conn, mention_id=mention.id, policy_id=policy.id
            )
            if existing is not None:
                logger.info(
                    "place resolution replay mention=%s policy=%s status=%s",
                    mention.id,
                    policy.id,
                    existing.status,
                )
                return existing

        normalized_alias = normalize_place_text(mention.original_text)
        async with self.uow.transaction() as conn:
            candidates = await self._places.alias_candidates(
                conn, normalized_alias=normalized_alias
            )
        choice = await self._resolver.choose(mention, candidates, policy_id=policy.id)

        async with self.uow.transaction() as conn:
            run = await self._runs.insert_running(
                conn,
                mention_id=mention.id,
                edition_id=policy.edition_id,
                policy_id=policy.id,
            )
            outcome_status = "resolved" if choice.place_id is not None else "unresolved"
            result, inserted = await self._places.insert_resolution_result(
                conn,
                run_id=run.id,
                mention_id=mention.id,
                policy_id=policy.id,
                place_id=choice.place_id,  # None is a valid unresolved
                status=outcome_status,
                confidence=choice.confidence,
                reason=choice.reason,
            )
            await self._runs.mark_succeeded(conn, run.id, completed_at=_now())
            if inserted:
                # Barrier may open exactly here: evaluate on the SAME conn.
                await self._maybe_schedule_matching(conn, mention)
            return result

    async def finalize_provider_failure(self, mention_id: int, policy_id: int):
        """Terminal fail-open write after retries are exhausted: the open run
        completes failed(provider_unavailable) through the guarded write that
        never demotes a succeeded winner; returns operationally."""
        async with self.uow.transaction() as conn:
            run = await self._runs.latest_running(conn, mention_id=mention_id, policy_id=policy_id)
            if run is None:
                return None
            demoted = await self._runs.mark_failed(
                conn, run.id, error_kind="provider_unavailable", completed_at=_now()
            )
            cursor = await conn.execute(
                """
                SELECT id, mention_id, edition_id, policy_id, started_at,
                       completed_at, status, error_kind, metadata
                FROM place_resolution_runs WHERE id = %s
                """,
                (run.id,),
            )
            fetched = await cursor.fetchone()
        final = PlaceResolutionRun.from_row(fetched) if fetched else None
        if demoted:
            logger.warning(
                "place resolution degraded provider_unavailable run=%s mention=%s",
                run.id,
                mention_id,
            )
        else:
            logger.warning("provider failure for place run=%s lost the race; converging", run.id)
        return final

    async def _maybe_schedule_matching(self, conn: psycopg.AsyncConnection, mention) -> None:
        """Hand the owning claim to the prerequisite barrier on OUR connection."""
        # Lazy on purpose: src.jobs.processing imports this module's siblings.
        from src.processing.story_matching import StoryMatchingPrerequisiteService

        scheduled = await StoryMatchingPrerequisiteService().maybe_schedule(
            conn, claim_id=mention.claim_id
        )
        if scheduled:
            logger.info(
                "prerequisite barrier opened; match_claim deferred claim=%s",
                mention.claim_id,
            )


# ---------------------------------------------------------------------------
# Legacy metadata backfill (bounded, idempotent, metadata untouched)
# ---------------------------------------------------------------------------


def staging_strings(metadata: dict[str, Any], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


async def backfill_place_mentions_rows(
    conn: psycopg.AsyncConnection,
    *,
    batch_size: int = 500,
) -> int:
    """Migrate T4-staging mentions/entities from claims.metadata into rows.

    Bounded slice; safe to re-run (per-place NOT EXISTS inserts). Claims
    metadata is NEVER modified — it remains the original staging record.
    """
    places = PlaceRepository()
    created = 0
    after_id: int | None = 0
    while True:
        claims = await places.list_claims_with_metadata_evidence(
            conn, after_claim_id=after_id, limit=batch_size
        )
        if not claims:
            break
        for claim_id, metadata in claims:
            after_id = claim_id
            for raw in staging_strings(metadata, "place_mentions"):
                _, inserted = await places.create_mention(
                    conn, claim_id=claim_id, original_text=raw
                )
                created += int(inserted)
            for raw in staging_strings(metadata, "entities"):
                _, inserted = await places.create_entity(
                    conn,
                    claim_id=claim_id,
                    normalized_text=normalize_place_text(raw),
                )
                created += int(inserted)
        if len(claims) < batch_size:
            break
    return created


# ---------------------------------------------------------------------------
# City profile importer: seed places + aliases idempotently
# ---------------------------------------------------------------------------


async def _ensure_place(
    conn: psycopg.AsyncConnection,
    places: PlaceRepository,
    *,
    profile_id: str,
    external_id: str,
    canonical_name: str,
    kind: str | None,
    parent_place_id: int | None,
    latitude: float | None = None,
    longitude: float | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[Place, bool]:
    existing = await places.find_by_external_key(
        conn, profile_id=profile_id, external_id=external_id
    )
    if existing is not None:
        return existing, False
    metadata = {
        "profile_id": profile_id,
        "external_id": external_id,
        **(extra_metadata or {}),
    }
    return (
        await places.insert_place(
            conn,
            canonical_name=canonical_name,
            kind=kind,
            parent_place_id=parent_place_id,
            latitude=latitude,
            longitude=longitude,
            metadata=metadata,
        ),
        True,
    )


async def _seed_aliases(
    conn: psycopg.AsyncConnection, places: PlaceRepository, place_id: int, names: list[str]
) -> None:
    seen: set[str] = set()
    for name in names:
        cleaned = name.strip()
        if not cleaned:
            continue
        normalized = normalize_place_text(cleaned)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await places.insert_alias(
            conn, place_id=place_id, alias=cleaned, normalized_alias=normalized
        )


async def import_places_from_city_profile(
    conn: psycopg.AsyncConnection, profile_path: str | Path
) -> int:
    """Seed `places` + `place_aliases` from one city-profile YAML file.

    Seeds the city itself, community settlements, every area-set's areas,
    and landmarks; the street gazetteer is deliberately NOT imported (its
    per-segment resolver contract needs object-type-aware matching beyond
    this task). Idempotent via (profile_id, external_id) metadata keys and
    per-place alias dedup; returns the number of NEW places created.
    """
    import yaml

    path = Path(profile_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"city profile {path} is not a mapping")
    profile_id = str(payload.get("profile_id") or path.stem)
    places = PlaceRepository()
    created_count = 0

    city_section = payload.get("city") or {}
    city_names = [str(v) for v in (city_section.get("names") or {}).values() if v]
    city_aliases = [str(a) for a in city_section.get("aliases") or []]
    city_place, created = await _ensure_place(
        conn,
        places,
        profile_id=profile_id,
        external_id="city",
        canonical_name=str(city_names[0]) if city_names else profile_id,
        kind="city",
        parent_place_id=None,
    )
    created_count += int(created)
    await _seed_aliases(conn, places, city_place.id, city_names + city_aliases)

    for settlement in city_section.get("community_settlements") or []:
        names = [
            str(name) for name in (settlement.get("name_ru"), settlement.get("name_uk")) if name
        ]
        if not names:
            continue
        place, created = await _ensure_place(
            conn,
            places,
            profile_id=profile_id,
            external_id=f"settlement:{settlement.get('id', names[0])}",
            canonical_name=names[0],
            kind=settlement.get("type"),
            parent_place_id=city_place.id,
        )
        created_count += int(created)
        await _seed_aliases(conn, places, place.id, names)

    geography = (payload.get("stable_context") or {}).get("geography") or {}
    for area_set in geography.get("area_sets") or []:
        for area in area_set.get("areas") or []:
            area_name = area.get("name")
            if not area_name:
                continue
            aliases = [str(area_name)] + [str(a) for a in area.get("aliases") or []]
            place, created = await _ensure_place(
                conn,
                places,
                profile_id=profile_id,
                external_id=f"area:{area_set.get('id')}:{area.get('id', area_name)}",
                canonical_name=str(area_name),
                kind="area",
                parent_place_id=city_place.id,
                extra_metadata={"area_set": str(area_set.get("id") or "")},
            )
            created_count += int(created)
            await _seed_aliases(conn, places, place.id, aliases)

    for landmark in (payload.get("stable_context") or {}).get("landmarks") or []:
        landmark_name = landmark.get("name")
        if not landmark_name:
            continue
        aliases = [str(landmark_name)] + [str(a) for a in landmark.get("aliases") or []]
        place, created = await _ensure_place(
            conn,
            places,
            profile_id=profile_id,
            external_id=f"landmark:{landmark.get('id', landmark_name)}",
            canonical_name=str(landmark_name),
            kind=landmark.get("type"),
            parent_place_id=city_place.id,
        )
        created_count += int(created)
        await _seed_aliases(conn, places, place.id, aliases)

    logger.info(
        "imported city profile %s: %d new places (%s)",
        profile_id,
        created_count,
        path.name,
    )
    return created_count
