"""Bounded Vision analysis for media-dependent relevance (Plan 3 Task 3).

Vision never re-decides text relevance from scratch and never replaces source
text: it derives small provenance observations (visible text, scene notes,
location/date clues, uncertainty language) that a follow-up AI verdict may
consume together with the original revision. Face identification is out of
scope forever.

Mode contract (``vision_policy_versions.mode``):

* ``off``            — no media work at all; ``needs_media`` items stay
  unresolved rather than being marked irrelevant.
* ``relevance_only`` — only explicit ``needs_media`` decisions spend vision.
* ``full``           — additionally runs bounded enrichment for already
  relevant revisions with eligible media, ahead of Claim extraction.

Provider boundary: :class:`VisionProvider` is an abstract ``analyze`` surface.
Concrete adapters may return per-asset ``unavailable`` outcomes when no pixel
data is obtainable; :class:`MetadataVisionProvider` is the shipped offline
fallback that classifies asset descriptors without any network or pixels.

Fail-open boundary: every terminal path funnels through
:meth:`VisionService.finish_vision_processing`, so a timeout/error still yields
a handoff whose ``ready_for_claims`` reflects TEXT support only — a relevant
decision stays claimable, while ``needs_media`` without observations stays
``needs_media`` (never a false irrelevant, never claims without evidence).

Limits (max assets per run, max provider calls, max size descriptor) stop work
early with an explicit partial outcome recorded in ``run.metadata["partial"]``.

Repositories never commit; the caller owns transaction boundaries. Provider
calls run OUTSIDE transactions except the bounded post-vision follow-up
verdict inside :meth:`VisionService.finish_vision_processing`, which must land
atomically with the run completion it belongs to.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import EditionRelevanceDecision, VisionAnalysisRun, VisionPolicyVersion
from src.ingestion.repository import IngestionRepository
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    VisionAnalysisRunRepository,
    VisionPolicyRepository,
)

logger = logging.getLogger(__name__)

VISION_PROMPT_VERSION = "vision-2026-08-v1"
DEFAULT_VISION_MODE = "relevance_only"

VISION_MODES = ("off", "relevance_only", "full")
VisionMode = Literal["off", "relevance_only", "full"]

MAX_ASSETS_PER_RUN = 4
MAX_PROVIDER_CALLS_PER_RUN = 4
MAX_SIZE_DESCRIPTOR_PIXELS = 8000

_ENSURE_CURRENT_RACE_ATTEMPTS = 3


class VisionProviderUnavailable(RuntimeError):
    """A vision provider attempt failed transiently (timeout/outage).

    The only exception ``analyze_vision`` retries; on the final attempt the run
    completes as ``unavailable`` and the fail-open handoff still applies.
    """


def _default_claim_policy_service() -> Any:
    """Lazy default for the claim handoff (avoids a circular module import)."""
    from src.processing.claims import ClaimExtractionPolicyService

    return ClaimExtractionPolicyService()


def vision_config_hash(*, mode: str) -> str:
    """Stable identity of the behaviour-affecting vision configuration.

    Mode is the only verdict-affecting input today; when a concrete
    pixel-capable adapter lands, its model/provider identity joins this hash so
    historical runs stay attributable to the exact configuration.
    """
    canonical = json.dumps({"mode": mode}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def should_run_vision(decision, revision, assets, *, mode: str) -> bool:
    """Pure scheduling policy for one completed decision + its revision media.

    Cases (spec Step 1):

    * ``off`` — never: items stay exactly as the text verdict left them, so a
      ``needs_media`` item remains unresolved instead of turning irrelevant.
    * ``relevance_only`` — only ``needs_media`` decisions spend vision; strong
      text verdicts never do.
    * ``full`` — additionally already-``relevant`` revisions with eligible
      media get bounded enrichment ahead of Claim extraction.
    * Any trigger requires at least one asset: nothing observable means nothing
      to analyze, and the item keeps its status.

    The spec's "absent/weak/uncertain text" case is implemented as the
    AI-emitted ``needs_media`` status: the model itself decides text was too
    thin and points at unseen media, so this function never re-judges text
    strength deterministically.
    """
    if mode not in VISION_MODES[1:]:
        return False
    has_media = bool(assets)
    if decision.status == "needs_media":
        return has_media
    if mode == "full":
        return decision.status == "relevant" and has_media
    return False


@dataclass(frozen=True)
class AssetDescriptor:
    """Bounded descriptor of one source asset handed to a VisionProvider."""

    asset_id: int | None
    kind: str
    mime_type: str | None
    external_url: str | None
    width: int | None
    height: int | None
    duration: int | None

    @property
    def size_pixels(self) -> int | None:
        values = [value for value in (self.width, self.height) if value is not None]
        return max(values) if values else None


@dataclass(frozen=True)
class VisionObservationDraft:
    """One derived observation before persistence."""

    source_asset_id: int | None
    kind: str
    text: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class VisionOutcome:
    """Per-asset provider result: observations or an explicit unavailability."""

    status: str
    observations: tuple[VisionObservationDraft, ...]
    error_kind: str | None = None


class VisionProvider(Protocol):
    """Abstract vision analysis boundary; concrete adapters implement this."""

    async def analyze(
        self, asset: AssetDescriptor, *, context: dict[str, Any]
    ) -> VisionOutcome: ...


class MetadataVisionProvider:
    """Offline descriptor classification fallback (no pixels, no network).

    Produces honest provenance observations about what the pipeline knows from
    ingestion metadata alone. It never invents scene content and never performs
    face identification; real pixel analysis arrives with a dedicated adapter.
    """

    async def analyze(self, asset: AssetDescriptor, *, context: dict[str, Any]) -> VisionOutcome:
        dimensions = (
            f"{asset.width}x{asset.height}"
            if asset.width and asset.height
            else "dimensions unknown"
        )
        duration = f", duration {asset.duration}s" if asset.duration else ""
        url_state = bool(asset.external_url)
        summary = (
            f"{asset.kind} ({asset.mime_type or 'unknown type'}), {dimensions}{duration}; "
            + ("source URL available" if url_state else "no source URL available")
        )
        return VisionOutcome(
            status="succeeded",
            observations=(
                VisionObservationDraft(
                    source_asset_id=asset.asset_id,
                    kind="media_descriptor",
                    text=summary,
                    metadata={
                        "basis": "metadata_only",
                        "pixel_data": False,
                        "external_url": url_state,
                        "asset_kind": asset.kind,
                    },
                ),
            ),
        )


class VisionPolicyService:
    """Resolve-or-create the edition's current vision policy version.

    Identity is (edition_id, config_hash(mode), prompt_version); a matching row
    is returned as-is, otherwise version MAX+1 is created. Unlike relevance
    policies there is no editions pointer column — callers keep the resolved id
    on the artifacts they create (runs reference their exact policy). Mode is
    read from ``settings.vision_mode`` in config.yaml by the jobs wiring;
    ``DEFAULT_VISION_MODE`` ("relevance_only") applies whenever operators have
    not pinned a mode explicitly.
    """

    def __init__(self, repo: VisionPolicyRepository | None = None) -> None:
        self._repo = repo or VisionPolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        mode: str = DEFAULT_VISION_MODE,
        config_hash: str | None = None,
        prompt_version: str = VISION_PROMPT_VERSION,
    ) -> VisionPolicyVersion:
        if mode not in VISION_MODES:
            raise ValueError(f"vision mode must be one of {', '.join(VISION_MODES)}, got {mode!r}")
        resolved_hash = config_hash or vision_config_hash(mode=mode)

        for _ in range(_ENSURE_CURRENT_RACE_ATTEMPTS):
            policies = await self._repo.list_for_edition(conn, edition_id)
            for policy in policies:
                if policy.config_hash == resolved_hash and policy.prompt_version == prompt_version:
                    return policy

            next_version = max((policy.version for policy in policies), default=0) + 1
            try:
                async with conn.transaction():
                    return await self._repo.insert(
                        conn,
                        edition_id=edition_id,
                        version=next_version,
                        mode=mode,
                        config_hash=resolved_hash,
                        prompt_version=prompt_version,
                    )
            except psycopg.errors.UniqueViolation:
                # A concurrent creator inserted version MAX+1 first; re-read
                # and converge on its row (duplicate policies are harmless).
                continue
        raise RuntimeError(
            f"could not ensure current vision policy for edition {edition_id} "
            f"after {_ENSURE_CURRENT_RACE_ATTEMPTS} attempts"
        )


@dataclass(frozen=True)
class VisionHandoff:
    """What Claim extraction (Task 4) consumes after one vision run."""

    relevance_decision_id: int
    vision_run_id: int | None
    ready_for_claims: bool


@dataclass(frozen=True)
class _VisionInputs:
    decision: EditionRelevanceDecision
    policy: VisionPolicyVersion
    assets: tuple[AssetDescriptor, ...]


class VisionService:
    """Orchestrate one bounded vision run over one decision's media."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        provider: VisionProvider,
        relevance_service: Any = None,
        ingestion_repo: IngestionRepository | None = None,
        run_repo: VisionAnalysisRunRepository | None = None,
        decision_repo: EditionRelevanceDecisionRepository | None = None,
        max_assets_per_run: int = MAX_ASSETS_PER_RUN,
        max_provider_calls_per_run: int = MAX_PROVIDER_CALLS_PER_RUN,
        max_size_pixels: int = MAX_SIZE_DESCRIPTOR_PIXELS,
        analyze_timeout: float = 30.0,
        claims_enabled: bool = False,
        claim_policy_service: Any = None,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self._relevance_service = relevance_service
        self._ingestion_repo = ingestion_repo or IngestionRepository()
        self._run_repo = run_repo or VisionAnalysisRunRepository()
        self._decision_repo = decision_repo or EditionRelevanceDecisionRepository()
        # Claim-extraction handoff (Task 4): opt-in at the wiring layer. When
        # enabled, a ready_for_claims handoff defers extract_claims INSIDE the
        # same transaction that completes the vision run / child decision.
        self._claims_enabled = claims_enabled
        self._claim_policy_service = claim_policy_service
        self.max_assets_per_run = max_assets_per_run
        self.max_provider_calls_per_run = max_provider_calls_per_run
        self.max_size_pixels = max_size_pixels
        self.analyze_timeout = analyze_timeout

    async def run(
        self, source_item_revision_id: int, relevance_decision_id: int, policy_id: int
    ) -> VisionHandoff:
        """Execute one bounded run and land its fail-open completion."""
        async with self.uow.transaction() as conn:
            inputs = await self._load_inputs(
                conn,
                source_item_revision_id=source_item_revision_id,
                relevance_decision_id=relevance_decision_id,
                policy_id=policy_id,
            )
            existing = await self._run_repo.latest_for_decision_policy(
                conn, relevance_decision_id=relevance_decision_id, policy_id=policy_id
            )
            if existing is not None and existing.status == "succeeded":
                # Redelivered job after success: converge on stored artifacts
                # instead of spending provider calls again.
                stored = await self._run_repo.list_observations(conn, existing.id)
                return await self._handoff_for(conn, inputs.decision, existing, tuple(stored))

            planned = inputs.assets[: self.max_assets_per_run]
            beyond_cap_ids = [asset.asset_id for asset in inputs.assets[self.max_assets_per_run :]]
            run = await self._run_repo.insert(
                conn,
                source_item_revision_id=source_item_revision_id,
                edition_id=inputs.decision.edition_id,
                relevance_decision_id=relevance_decision_id,
                policy_id=policy_id,
                metadata={
                    "mode": inputs.policy.mode,
                    "planned_asset_ids": [a.asset_id for a in planned],
                    "limits": {
                        "max_assets_per_run": self.max_assets_per_run,
                        "max_provider_calls_per_run": self.max_provider_calls_per_run,
                        "max_size_pixels": self.max_size_pixels,
                    },
                },
            )

        observations, unavailable_count, partial = await self._analyze_assets(planned)

        error_kind = None
        if not observations:
            if unavailable_count:
                error_kind = "no_pixel_data"
            elif partial or not planned:
                error_kind = "no_eligible_assets"

        partial_outcome = self._partial_outcome_metadata(beyond_cap_ids, partial)
        run_metadata = {"partial": partial_outcome} if partial_outcome else None

        async with self.uow.transaction() as conn:
            return await self.finish_vision_processing(
                conn,
                run,
                decision=inputs.decision,
                observations=tuple(observations),
                error=error_kind,
                run_metadata=run_metadata or None,
            )

    async def finalize_provider_failure(
        self,
        source_item_revision_id: int,
        relevance_decision_id: int,
        policy_id: int,
        *,
        error_kind: str = "provider_unavailable",
    ) -> VisionHandoff:
        """Terminal fail-open write after retries are exhausted."""
        async with self.uow.transaction() as conn:
            decision = await self._decision_repo.get(conn, relevance_decision_id)
            if decision is None or decision.source_item_revision_id != source_item_revision_id:
                raise ValueError(
                    f"relevance decision {relevance_decision_id} does not match "
                    f"revision {source_item_revision_id}"
                )
            policy = await VisionPolicyRepository().get(conn, policy_id)
            if policy is None or policy.edition_id != decision.edition_id:
                raise ValueError(
                    f"vision policy {policy_id} does not belong to edition {decision.edition_id}"
                )
            run = await self._run_repo.insert(
                conn,
                source_item_revision_id=source_item_revision_id,
                edition_id=decision.edition_id,
                relevance_decision_id=relevance_decision_id,
                policy_id=policy_id,
                metadata={"terminal_failure": True},
            )
            return await self.finish_vision_processing(
                conn, run, decision=decision, observations=(), error=error_kind
            )

    async def finish_vision_processing(
        self,
        conn: psycopg.AsyncConnection,
        run: VisionAnalysisRun,
        *,
        decision: EditionRelevanceDecision,
        observations: tuple[VisionObservationDraft | Any, ...],
        error: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> VisionHandoff:
        """Single fail-open completion boundary shared by all vision paths.

        Completes the immutable run (+ observation rows), then either produces
        the post-vision child decision (needs_media + observations) or hands the
        original decision onward. A text-supported relevant decision always
        stays ready for claims; needs_media without usable observations never
        resolves negatively.
        """
        await self._run_repo.complete(
            conn,
            run,
            observations=tuple(observations),
            error=error,
            additional_metadata=run_metadata,
        )
        summaries = [
            {"kind": obs.kind, "text": obs.text, "metadata": obs.metadata} for obs in observations
        ]
        logger.info(
            "vision run finished run=%s decision=%s status=%s error=%s observations=%d",
            run.id,
            decision.id,
            "unavailable" if error else "succeeded",
            error,
            len(summaries),
        )
        if decision.status == "needs_media" and observations:
            child = await self._relevance_service.decide_with_vision(conn, decision, observations)
            handoff = VisionHandoff(
                relevance_decision_id=child.id,
                vision_run_id=run.id,
                ready_for_claims=child.status == "relevant",
            )
        else:
            handoff = VisionHandoff(
                relevance_decision_id=decision.id,
                vision_run_id=run.id if observations else None,
                ready_for_claims=decision.status == "relevant",
            )
        if handoff.ready_for_claims and self._claims_enabled:
            # Atomic with run completion / child decision above: a crash can
            # never complete vision without its claims follow-up. Redelivery
            # replay (_handoff_for) deliberately does NOT re-defer — the first
            # execution already committed this defer with the same txn.
            await self._defer_extract_claims(conn, handoff)
        return handoff

    async def _defer_extract_claims(
        self, conn: psycopg.AsyncConnection, handoff: VisionHandoff
    ) -> None:
        policy_service = self._claim_policy_service or _default_claim_policy_service()
        decision = await self._decision_repo.get(conn, handoff.relevance_decision_id)
        if decision is None:
            raise ValueError(
                f"relevance decision {handoff.relevance_decision_id} vanished "
                "before the claim handoff"
            )
        policy = await policy_service.ensure_current(conn, edition_id=decision.edition_id)
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import extract_claims

        await extract_claims.configure(connection=conn).defer_async(
            source_item_revision_id=decision.source_item_revision_id,
            edition_id=decision.edition_id,
            relevance_decision_id=handoff.relevance_decision_id,
            policy_id=policy.id,
            vision_run_id=handoff.vision_run_id,
        )
        logger.info(
            "deferred extract_claims revision=%s decision=%s policy=%s vision_run=%s",
            decision.source_item_revision_id,
            handoff.relevance_decision_id,
            policy.id,
            handoff.vision_run_id,
        )

    @staticmethod
    def _partial_outcome_metadata(
        beyond_cap_ids: list[int | None],
        partial: dict[str, list[int | None]],
    ) -> dict[str, Any] | None:
        """Aggregate limit stops into one explicit partial outcome record.

        ``reason`` names the highest-priority stop that applied; ``asset_ids``
        lists everything that did NOT get analyzed, whatever the cause.
        """
        entries: list[tuple[str, list[int | None]]] = []
        if beyond_cap_ids:
            entries.append(("asset_limit_exceeded", beyond_cap_ids))
        for reason in ("call_budget_exhausted", "size_descriptor_exceeded", "provider_unavailable"):
            if partial.get(reason):
                entries.append((reason, partial[reason]))
        if not entries:
            return None
        return {
            "reason": entries[0][0],
            "asset_ids": [asset_id for _, ids in entries for asset_id in ids],
        }

    async def _analyze_assets(
        self, planned: tuple[AssetDescriptor, ...]
    ) -> tuple[list[VisionObservationDraft], int, dict[str, list[int | None]]]:
        """Call the provider within limits; exceptions map to the transient marker."""
        observations: list[VisionObservationDraft] = []
        unavailable_count = 0
        partial: dict[str, list[int | None]] = {}
        calls_used = 0
        for asset in planned:
            if calls_used >= self.max_provider_calls_per_run:
                partial.setdefault("call_budget_exhausted", []).append(asset.asset_id)
                continue
            size = asset.size_pixels
            if size is not None and size > self.max_size_pixels:
                partial.setdefault("size_descriptor_exceeded", []).append(asset.asset_id)
                continue
            try:
                outcome = await asyncio.wait_for(
                    self.provider.analyze(asset, context={}), timeout=self.analyze_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise VisionProviderUnavailable(
                    f"vision provider failed for asset {asset.asset_id}: {type(exc).__name__}"
                ) from exc
            calls_used += 1
            if outcome.status != "succeeded":
                unavailable_count += 1
                partial.setdefault("provider_unavailable", []).append(asset.asset_id)
                continue
            observations.extend(outcome.observations)
        return observations, unavailable_count, partial

    async def _load_inputs(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        relevance_decision_id: int,
        policy_id: int,
    ) -> _VisionInputs:
        decision = await self._decision_repo.get(conn, relevance_decision_id)
        if decision is None or decision.source_item_revision_id != source_item_revision_id:
            raise ValueError(
                f"relevance decision {relevance_decision_id} does not match "
                f"revision {source_item_revision_id}"
            )
        policy = await VisionPolicyRepository().get(conn, policy_id)
        if policy is None or policy.edition_id != decision.edition_id:
            raise ValueError(
                f"vision policy {policy_id} does not belong to edition {decision.edition_id}"
            )
        descriptors = await self._ingestion_repo.list_asset_descriptors(
            conn, source_item_revision_id
        )
        return _VisionInputs(
            decision=decision,
            policy=policy,
            assets=tuple(descriptors),
        )

    async def _handoff_for(
        self,
        conn: psycopg.AsyncConnection,
        decision: EditionRelevanceDecision,
        run: VisionAnalysisRun,
        observations: tuple[Any, ...],
    ) -> VisionHandoff:
        """Recompute a handoff from persisted artifacts (redelivery replay)."""
        if decision.status == "needs_media" and observations:
            child = await self._existing_child(conn, decision)
            return VisionHandoff(
                relevance_decision_id=child.id,
                vision_run_id=run.id,
                ready_for_claims=child.status == "relevant",
            )
        return VisionHandoff(
            relevance_decision_id=decision.id,
            vision_run_id=run.id if observations else None,
            ready_for_claims=decision.status == "relevant",
        )

    async def _existing_child(
        self, conn: psycopg.AsyncConnection, decision: EditionRelevanceDecision
    ):
        latest = await self._decision_repo.latest_for_revision_edition(
            conn,
            source_item_revision_id=decision.source_item_revision_id,
            edition_id=decision.edition_id,
        )
        if latest is None or latest.parent_decision_id != decision.id:
            raise ValueError(
                f"succeeded vision run for needs_media decision {decision.id} "
                "is missing its follow-up child decision"
            )
        return latest
