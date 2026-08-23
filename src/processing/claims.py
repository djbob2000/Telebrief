"""Claim extraction processing (Plan 3 Task 4): policy identity, bounded
conversation context, AI extraction with atomic canonical visibility,
immutable corrections, and fail-open terminal paths.

Controller rulings implemented here:

* ``editions`` gains NO current-claim-policy pointer column (unlike
  relevance policies). :meth:`ClaimExtractionPolicyService.ensure_current`
  therefore resolves "current" purely by identity (config_hash,
  prompt_version) per edition with latest-version-wins semantics; every
  extraction run pins the exact resolved policy id.
* Place/entity mentions are stored INSIDE ``claims.metadata`` as
  ``{"place_mentions": [...], "entities": [...]}`` (T4 staging) AND, since
  Plan 3 Task 8, materialized into the dedicated ``claim_place_mentions`` /
  ``claim_entities`` rows. With ``place_resolution_handoff`` enabled (the
  production wiring), the SAME success transaction resolves-or-creates the
  edition's current place-resolution policy and defers one
  ``resolve_place_mention(mention_id, policy_id)`` per newly-created
  mention; claims with zero mentions satisfy the matching barrier
  vacuously at embedding time. Legacy metadata rows migrate via the
  bounded idempotent backfill; metadata itself is never rewritten.

Pipeline shape (spec §15): at most one run per
(source_item_revision_id, edition_id, extraction_policy_id) may ever reach
status 'succeeded'; the partial unique index uq_claim_extraction_success is
the authoritative backstop and the guarded ``mark_succeeded`` keeps the
common path friendly. The provider call runs OUTSIDE any transaction so a
slow model never holds a pooled connection, and provider retries keep the
same semantic run id — only ``processing_attempts.attempt_no`` increments
(that table is audit history, never a queue). Terminal provider exhaustion
marks the run ``failed(error_kind='provider_unavailable')``, closes its
attempt as ``unavailable``, and reports OPERATIONAL success: an item stays
relevant-but-claimless, bounded backfill retries it later, and the pipeline
never blocks on the AI. A structurally invalid payload is a deterministic
failure (never retried): the attempt closes ``failed``, the run fails with
``invalid_ai_response`` and the pipeline degrades identically.

Repositories never commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from src.ai_providers import AIProvider, ProviderCascadeError, ensure_provider_cascade
from src.config_loader import EmbeddingConfig
from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import (
    Claim,
    ClaimExtractionPolicyVersion,
    ClaimExtractionRun,
    NewClaim,
)
from src.ingestion.repository import IngestionRepository
from src.processing.places import (
    PlaceResolutionPolicyService,
    normalize_place_text,
    staging_strings,
)
from src.processing.relevance import ProviderUnavailableError, _parse_json_object
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.places import PlaceRepository

logger = logging.getLogger(__name__)

CLAIM_EXTRACTION_PROMPT_VERSION = "claims-2026-08-v1"

# Total parent+root conversation context handed to the model. The target
# revision (THE provenance) is included in full; context exists only to
# resolve ellipsis/deixis and is capped so replies to huge threads stay
# bounded. Root takes half the budget, parent gets the remainder.
CONTEXT_CHAR_BUDGET = 2000

_ATTEMPT_STAGE = "claim_extraction"
_MAX_OUTPUT_TOKENS = 2048
_ENSURE_CURRENT_RACE_ATTEMPTS = 3

_VALID_RELATION_TYPES = frozenset({"CORRECTS", "SUPERSEDES", "RETRACTS"})


class InvalidClaimResponse(RuntimeError):
    """Structurally unusable model payload; deterministic, never retried."""


class CanonicalSlotLost(RuntimeError):
    """A concurrent execution already claimed the canonical success slot."""


def claim_config_hash() -> str:
    """Stable identity of extraction-affecting configuration.

    No verdict-affecting runtime setting exists yet (the prompt version is
    tracked separately in its own column); when a concrete model/provider
    identity starts shaping extraction output it joins this hash so runs
    stay attributable to their exact configuration.
    """
    canonical = json.dumps(
        {"schema": 1, "stage": "claim_extraction"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _truncate(text: str | None, limit: int) -> str | None:
    """Strip and hard-truncate one context piece to ``limit`` characters."""
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _parse_event_moment(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed


@dataclass(frozen=True)
class ExtractedClaimDraft:
    """One validated model-proposed claim before persistence.

    Mentions ride along as raw unresolved texts and land in
    ``claims.metadata`` (see module docstring ruling) via :meth:`to_new_claim`.
    """

    assertion_text: str
    normalized_assertion: str
    event_time_start: dt.datetime | None = None
    event_time_end: dt.datetime | None = None
    event_time_precision: str | None = None
    event_time_confidence: float | None = None
    event_time_original_text: str | None = None
    place_mentions: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    @classmethod
    def from_entry(cls, entry: Any) -> ExtractedClaimDraft | None:
        """Validate one model entry; ``None`` drops it (per-entry lenience).

        Only structural garbage ACROSS the payload fails the whole call
        (:class:`InvalidClaimResponse`); an individually malformed claim is
        skipped so one bad entry cannot discard good ones.
        """
        if not isinstance(entry, dict):
            return None
        assertion = entry.get("assertion_text")
        normalized = entry.get("normalized_assertion")
        if not isinstance(assertion, str) or not assertion.strip():
            return None
        if not isinstance(normalized, str) or not normalized.strip():
            return None
        event_time = entry.get("event_time")
        if not isinstance(event_time, dict):
            event_time = {}
        precision_raw = event_time.get("precision")
        original_raw = event_time.get("original_text")
        return cls(
            assertion_text=assertion.strip(),
            normalized_assertion=normalized.strip(),
            event_time_start=_parse_event_moment(event_time.get("start")),
            event_time_end=_parse_event_moment(event_time.get("end")),
            event_time_precision=(
                precision_raw.strip()
                if isinstance(precision_raw, str) and precision_raw.strip()
                else None
            ),
            event_time_confidence=_optional_confidence(event_time.get("confidence")),
            event_time_original_text=_optional_str(original_raw),
            place_mentions=_string_list(entry.get("place_mentions")),
            entities=_string_list(entry.get("entities")),
        )

    def to_new_claim(self) -> NewClaim:
        return NewClaim(
            assertion_text=self.assertion_text,
            normalized_assertion=self.normalized_assertion,
            event_time_start=self.event_time_start,
            event_time_end=self.event_time_end,
            event_time_precision=self.event_time_precision,
            event_time_confidence=self.event_time_confidence,
            event_time_original_text=self.event_time_original_text,
            # T8 ruling: dedicated mention tables arrive later; migrate then.
            metadata={
                "place_mentions": list(self.place_mentions),
                "entities": list(self.entities),
            },
        )


@dataclass(frozen=True)
class ClaimExtractionContext:
    """What one extraction consultation sees.

    ``assertion_text`` is THE provenance text of the target revision;
    ``parent_text``/``root_text`` are bounded clarifying context for
    replies/comments and never become provenance themselves.
    """

    revision_id: int
    assertion_text: str | None
    parent_text: str | None = None
    root_text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class ClaimExtractionPolicyService:
    """Resolve-or-create the edition's current claim-extraction policy version.

    Identity is (edition_id, config_hash, prompt_version); a matching row is
    returned as-is, otherwise version MAX+1 is created. RULING: there is no
    editions pointer column for claim policies — "current" is purely the
    identity-resolved row with latest-version-wins semantics, and callers pin
    the resolved exact id onto the runs they create. Concurrent creators are
    arbitrated by uq_claim_extraction_policy_edition_version via a savepoint
    re-read loop.
    """

    def __init__(self, repo: ClaimExtractionPolicyRepository | None = None) -> None:
        self._repo = repo or ClaimExtractionPolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str | None = None,
        prompt_version: str = CLAIM_EXTRACTION_PROMPT_VERSION,
    ) -> ClaimExtractionPolicyVersion:
        resolved_hash = config_hash or claim_config_hash()
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
                        config_hash=resolved_hash,
                        prompt_version=prompt_version,
                    )
            except psycopg.errors.UniqueViolation:
                # A concurrent creator inserted version MAX+1 first; re-read
                # and converge on its row (duplicate policies are harmless).
                continue
        raise RuntimeError(
            f"could not ensure current claim extraction policy for edition {edition_id} "
            f"after {_ENSURE_CURRENT_RACE_ATTEMPTS} attempts"
        )


class ClaimExtractionContextBuilder:
    """Build the bounded consultation context around ONE target revision.

    For replies/comments the latest revisions of the linked parent and root
    items join the payload so deictic language («тоже», «ещё нет», «там») can
    be resolved. Combined context never exceeds CONTEXT_CHAR_BUDGET
    characters (root first, parent fills the remainder); the target revision
    itself is never truncated here and remains the sole provenance source.
    """

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        ingestion_repo: IngestionRepository | None = None,
    ) -> None:
        self._uow = uow
        self._ingestion_repo = ingestion_repo or IngestionRepository()

    async def build(self, revision_id: int) -> ClaimExtractionContext:
        async with self._uow.transaction() as conn:
            return await self.build_with_conn(conn, revision_id)

    async def build_with_conn(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> ClaimExtractionContext:
        revision = await self._ingestion_repo.get_revision(conn, revision_id)
        if revision is None:
            raise ValueError(f"source item revision {revision_id} does not exist")
        raw_parent, raw_root = await self._ingestion_repo.get_reply_context(conn, revision_id)
        root_text = _truncate(raw_root, CONTEXT_CHAR_BUDGET // 2)
        parent_budget = max(0, CONTEXT_CHAR_BUDGET - len(root_text or ""))
        parent_text = _truncate(raw_parent, parent_budget)
        return ClaimExtractionContext(
            revision_id=revision.id,
            assertion_text=revision.text_content,
            parent_text=parent_text,
            root_text=root_text,
            payload=dict(revision.payload or {}),
        )


@dataclass(frozen=True)
class ClaimExtractionResult:
    """Terminal outcome of one ``extract`` invocation.

    ``degraded`` is set on fail-open terminal paths (``provider_unavailable``
    / ``invalid_ai_response``) where the run ends failed but the pipeline
    succeeds operationally; ``replayed`` marks convergence on an existing
    canonical success instead of fresh work.
    """

    run: ClaimExtractionRun
    claims: tuple[Claim, ...]
    replayed: bool = False
    degraded: str | None = None


class ClaimExtractionService:
    """Extract immutable source-bound claims for one relevant decision.

    The policy id is always the caller-supplied one: a retried job keeps the
    originally queued policy even if a newer version became current in the
    meantime, so claims stay attributable to their exact prompt/config.
    """

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        provider: AIProvider,
        model: str,
        provider_name: str,
        reasoning_effort: str | None = None,
        max_output_tokens: int = _MAX_OUTPUT_TOKENS,
        ingestion_repo: IngestionRepository | None = None,
        policy_repo: ClaimExtractionPolicyRepository | None = None,
        run_repo: ClaimExtractionRunRepository | None = None,
        claim_repo: ClaimRepository | None = None,
        context_builder: ClaimExtractionContextBuilder | None = None,
        processing_mode: str = "knowledge_full",
        embedding_config: EmbeddingConfig | None = None,
        place_resolution_handoff: bool = False,
        places_repo: PlaceRepository | None = None,
        place_policy_service: PlaceResolutionPolicyService | None = None,
    ) -> None:
        self.uow = uow
        # Uniform cascade semantics even for single-slot providers.
        self.provider = ensure_provider_cascade(provider, logger=logger, slot_name=provider_name)
        self.model = model
        self.provider_name = provider_name
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self._processing_mode = processing_mode
        self._embedding_config = embedding_config
        # Place-resolution handoff is opt-in at the wiring layer (mirrors the
        # embedding service's matching_handoff): when enabled, newly created
        # claims materialize their mentions/entities and defer one
        # resolve_place_mention per new mention on the success transaction.
        self._place_handoff = place_resolution_handoff
        self._places = places_repo or PlaceRepository()
        self._place_policy_service = place_policy_service or PlaceResolutionPolicyService()
        self._ingestion_repo = ingestion_repo or IngestionRepository()
        self._policy_repo = policy_repo or ClaimExtractionPolicyRepository()
        self._run_repo = run_repo or ClaimExtractionRunRepository()
        self._claim_repo = claim_repo or ClaimRepository()
        self._context_builder = context_builder or ClaimExtractionContextBuilder(
            uow, self._ingestion_repo
        )

    async def extract(
        self,
        source_item_revision_id: int,
        edition_id: int,
        relevance_decision_id: int,
        policy_id: int,
        vision_run_id: int | None = None,
    ) -> ClaimExtractionResult:
        """One extraction pass over one revision under one exact policy."""
        del vision_run_id  # threaded for API stability; audit rides on attempts
        async with self.uow.transaction() as conn:
            policy = await self._policy_repo.get(conn, policy_id)
            if policy is None or policy.edition_id != edition_id:
                raise ValueError(
                    f"claim extraction policy {policy_id} does not belong to edition {edition_id}"
                )
            run = await self._resolve_run(
                conn,
                source_item_revision_id=source_item_revision_id,
                edition_id=edition_id,
                extraction_policy_id=policy_id,
                relevance_decision_id=relevance_decision_id,
            )
            if run.status == "succeeded":
                claims = await self._claim_repo.list_for_run(conn, run.id)
                logger.info("claim extraction replay run=%s claims=%d", run.id, len(claims))
                return ClaimExtractionResult(run=run, claims=tuple(claims), replayed=True)
            attempt = await self._run_repo.start_attempt(
                conn,
                stage=_ATTEMPT_STAGE,
                semantic_run_id=run.id,
                provider=self.provider_name,
                model=self.model,
            )
            context = await self._context_builder.build_with_conn(conn, source_item_revision_id)
            edition_name = await self._ingestion_repo.get_edition_name(conn, edition_id)
            if edition_name is None:
                raise ValueError(f"edition {edition_id} does not exist")

        try:
            drafts = await self._call_model(context, edition_name)
        except ProviderUnavailableError:
            # This technical attempt ended unavailable; the semantic run stays
            # open so the retried execution appends the next attempt row.
            async with self.uow.transaction() as conn:
                await self._run_repo.finish_attempt(
                    conn,
                    attempt,
                    status="unavailable",
                    completed_at=_now(),
                    error_kind="provider_unavailable",
                )
            raise
        except InvalidClaimResponse:
            return await self._finalize_invalid_response(run, attempt)

        async with self.uow.transaction() as conn:
            await self._run_repo.finish_attempt(
                conn, attempt, status="succeeded", completed_at=_now()
            )
        return await self._persist_success(run, drafts)

    async def record_correction(
        self,
        old_claim_id: int,
        corrected_assertion: str,
        relation: str = "CORRECTS",
    ) -> Claim:
        """Create a NEW immutable claim correcting ``old_claim_id`` plus the
        linking relation; the old claim is never modified.

        Provenance chains through the ORIGINAL extraction run and revision —
        a correction annotates an existing source-bound claim, it is not a
        new observation.
        """
        if relation not in _VALID_RELATION_TYPES:
            raise ValueError(
                f"relation must be one of {sorted(_VALID_RELATION_TYPES)}, got {relation!r}"
            )
        corrected = corrected_assertion.strip()
        if not corrected:
            raise ValueError("corrected_assertion must not be empty")
        async with self.uow.transaction() as conn:
            existing = await self._claim_repo.get_many(conn, [old_claim_id])
            if not existing:
                raise ValueError(f"claim {old_claim_id} does not exist")
            old_claim = existing[0]
            run = await self._run_repo.get(conn, old_claim.claim_extraction_run_id)
            if run is None:
                raise RuntimeError(f"extraction run {old_claim.claim_extraction_run_id} vanished")
            inserted = await self._claim_repo.insert_claims(
                conn,
                run=run,
                claims=[
                    NewClaim(
                        assertion_text=corrected,
                        normalized_assertion=corrected,
                        metadata={"correction_of": old_claim.id, "correction_relation": relation},
                    )
                ],
            )
            new_claim = inserted[0]
            await self._claim_repo.attach_relation(
                conn,
                from_claim_id=new_claim.id,
                to_claim_id=old_claim.id,
                relation_type=relation,
            )
        logger.info(
            "recorded claim correction old=%s new=%s relation=%s",
            old_claim.id,
            new_claim.id,
            relation,
        )
        return new_claim

    async def finalize_provider_failure(
        self,
        source_item_revision_id: int,
        edition_id: int,
        relevance_decision_id: int,
        policy_id: int,
        vision_run_id: int | None = None,
    ) -> ClaimExtractionResult:
        """Terminal fail-open write after retries are exhausted.

        Marks the RUNNING semantic run failed(provider_unavailable), closes
        any still-open audit attempt as unavailable, and RETURNS successfully
        — the item stays relevant-but-claimless and bounded backfill retries
        later (failed runs never occupy the canonical slot). A concurrently
        succeeded canonical winner is never demoted: the guarded
        ``mark_failed`` refuses, and this path converges on the winner's
        claims instead of reporting degradation.
        """
        del vision_run_id
        async with self.uow.transaction() as conn:
            policy = await self._policy_repo.get(conn, policy_id)
            if policy is None or policy.edition_id != edition_id:
                raise ValueError(
                    f"claim extraction policy {policy_id} does not belong to edition {edition_id}"
                )
            run = await self._resolve_run(
                conn,
                source_item_revision_id=source_item_revision_id,
                edition_id=edition_id,
                extraction_policy_id=policy_id,
                relevance_decision_id=relevance_decision_id,
            )
            if run.status == "succeeded":
                # Lost the race before even finalizing: converge on the
                # winner atomically visible artifacts, change nothing.
                claims = await self._claim_repo.list_for_run(conn, run.id)
                return ClaimExtractionResult(run=run, claims=tuple(claims), replayed=True)
            demoted = await self._run_repo.mark_failed(
                conn,
                run.id,
                error_kind="provider_unavailable",
                completed_at=_now(),
            )
            lost_slot = not demoted
            if not lost_slot:
                open_attempt = await self._run_repo.latest_open_attempt(
                    conn, stage=_ATTEMPT_STAGE, semantic_run_id=run.id
                )
                if open_attempt is not None:
                    await self._run_repo.finish_attempt(
                        conn,
                        open_attempt,
                        status="unavailable",
                        completed_at=_now(),
                        error_kind="provider_unavailable",
                    )
                final = await self._run_repo.get(conn, run.id)
        if lost_slot:
            # A concurrent execution flipped the run to succeeded between our
            # read and write: never report failure over a canonical winner.
            logger.warning(
                "finalize_provider_failure lost canonical race for run=%s; converging",
                run.id,
            )
            return await self._replay_after_lost_slot(run)
        logger.warning(
            "claim extraction degraded provider_unavailable run=%s revision=%s",
            run.id,
            source_item_revision_id,
        )
        if final is None:
            raise RuntimeError(f"claim extraction run {run.id} vanished at finalization")
        return ClaimExtractionResult(run=final, claims=(), degraded="provider_unavailable")

    async def _resolve_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        extraction_policy_id: int,
        relevance_decision_id: int,
    ) -> ClaimExtractionRun:
        """Reuse the open running run across technical retries; failed runs
        (and absent ones) start a fresh chain via the canonical get-or-create.
        """
        existing = await self._run_repo.latest_for_key(
            conn,
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            extraction_policy_id=extraction_policy_id,
        )
        if existing is not None and existing.status == "running":
            return existing
        run, _created = await self._run_repo.get_or_create_run(
            conn,
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            extraction_policy_id=extraction_policy_id,
            relevance_decision_id=relevance_decision_id,
        )
        return run

    async def _call_model(
        self, context: ClaimExtractionContext, edition: str
    ) -> tuple[ExtractedClaimDraft, ...]:
        """Strict-JSON consultation; runs OUTSIDE any transaction."""
        try:
            response = await self.provider.chat_completion(
                messages=[
                    {"role": "system", "content": self._system_prompt(edition)},
                    {"role": "user", "content": self._user_payload(context)},
                ],
                model=self.model,
                max_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={"type": "json_object"},
            )
        except ProviderCascadeError as exc:
            # Same mapping rule as relevance: every exhausted cascade is
            # provider unavailability for this attempt.
            raise ProviderUnavailableError(
                f"claim extraction provider unavailable ({exc.diagnostic_summary()})"
            ) from exc
        return self._parse_claims(_parse_json_object(response))

    def _parse_claims(self, payload: Any) -> tuple[ExtractedClaimDraft, ...]:
        if not isinstance(payload, dict):
            raise InvalidClaimResponse("claim payload is not a JSON object")
        entries = payload.get("claims")
        if not isinstance(entries, list):
            raise InvalidClaimResponse('claim payload lacks a "claims" list')
        drafts: list[ExtractedClaimDraft] = []
        for entry in entries:
            draft = ExtractedClaimDraft.from_entry(entry)
            if draft is None:
                logger.debug("dropped malformed claim entry from model payload")
                continue
            drafts.append(draft)
        return tuple(drafts)

    async def _finalize_invalid_response(
        self, run: ClaimExtractionRun, attempt: Any
    ) -> ClaimExtractionResult:
        """Deterministic parse failures never retry and never block.

        The guarded ``mark_failed`` keeps a concurrently succeeded canonical
        winner intact; when the slot is already won this converges on the
        winner's claims instead of reporting degradation.
        """
        async with self.uow.transaction() as conn:
            await self._run_repo.finish_attempt(
                conn,
                attempt,
                status="failed",
                completed_at=_now(),
                error_kind="invalid_ai_response",
            )
            demoted = await self._run_repo.mark_failed(
                conn,
                run.id,
                error_kind="invalid_ai_response",
                completed_at=_now(),
            )
        if not demoted:
            logger.warning(
                "invalid response for run=%s but canonical slot already won; converging",
                run.id,
            )
            return await self._replay_after_lost_slot(run)
        async with self.uow.transaction() as conn:
            final = await self._run_repo.get(conn, run.id)
        logger.warning("claim extraction invalid response run=%s", run.id)
        if final is None:
            raise RuntimeError(f"claim extraction run {run.id} vanished after failure")
        return ClaimExtractionResult(run=final, claims=(), degraded="invalid_ai_response")

    async def _persist_success(
        self, run: ClaimExtractionRun, drafts: Sequence[ExtractedClaimDraft]
    ) -> ClaimExtractionResult:
        """Insert immutable claims and flip the run succeeded ATOMICALLY.

        Claims become visible only together with canonical success. A lost
        race (guarded update says another run holds the slot, or the partial
        unique index rejects us at write time) rolls the transaction back and
        converges on the winner's artifacts instead.
        """
        new_claims = [draft.to_new_claim() for draft in drafts]
        completed_at = _now()
        try:
            async with self.uow.transaction() as conn:
                inserted = await self._claim_repo.insert_claims(conn, run=run, claims=new_claims)
                won = await self._run_repo.mark_succeeded(conn, run.id, completed_at=completed_at)
                if not won:
                    raise CanonicalSlotLost(f"canonical success held elsewhere for run {run.id}")
                if self._place_handoff:
                    await self._materialize_place_evidence(conn, inserted)
                await self._defer_embed_claims(conn, inserted)
                if (
                    self._processing_mode == "knowledge_no_embeddings"
                    or self._embedding_config is None
                ):
                    from src.processing.story_matching import StoryMatchingPrerequisiteService

                    prereq = StoryMatchingPrerequisiteService(processing_mode=self._processing_mode)
                    for claim in inserted:
                        if not staging_strings(claim.metadata, "place_mentions"):
                            await prereq.maybe_schedule(
                                conn, claim_id=claim.id, processing_mode=self._processing_mode
                            )
                final = await self._run_repo.get(conn, run.id)
        except psycopg.errors.UniqueViolation as exc:
            if getattr(exc, "diag", None) is None or exc.diag.constraint_name != (
                "uq_claim_extraction_success"
            ):
                raise
            return await self._replay_after_lost_slot(run)
        except CanonicalSlotLost:
            return await self._replay_after_lost_slot(run)
        if final is None:
            raise RuntimeError(f"claim extraction run {run.id} vanished after success")
        logger.info(
            "claim extraction succeeded run=%s revision=%s claims=%d",
            run.id,
            run.source_item_revision_id,
            len(inserted),
        )
        return ClaimExtractionResult(run=final, claims=tuple(inserted))

    async def _defer_embed_claims(
        self,
        conn: psycopg.AsyncConnection,
        inserted: Sequence[Claim],
    ) -> None:
        """Queue one embedding job per newly created claim INSIDE the success
        transaction: claims, canonical success, and their embed defers commit
        atomically — a crash can never leave a succeeded claim without its
        queued vector. Model/dimensions are copied from the config into the
        task arguments so retries keep the exact queued vector space.
        """
        if self._processing_mode == "knowledge_no_embeddings":
            return

        config = self._embedding_config
        if config is None or not inserted:
            return
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import embed_claim

        for claim in inserted:
            await embed_claim.configure(connection=conn).defer_async(
                claim_id=claim.id,
                model=config.model,
                dimensions=config.dimensions,
            )
        logger.info(
            "deferred %d embed_claim jobs (model=%s dimensions=%d)",
            len(inserted),
            config.model,
            config.dimensions,
        )

    async def _materialize_place_evidence(
        self,
        conn: psycopg.AsyncConnection,
        inserted: Sequence[Claim],
    ) -> None:
        """Materialize T8 evidence rows and defer place resolution ATOMICALLY.

        Runs INSIDE the success transaction: claim_place_mentions /
        claim_entities rows are created idempotently from the staging
        metadata (original mention text preserved verbatim), the edition's
        current place-resolution policy is resolved-or-created once, and one
        ``resolve_place_mention(mention_id, policy_id)`` is deferred per
        NEWLY-CREATED mention on THIS connection — a crash can never leave a
        visible mention without its queued resolution. Claims with zero
        mentions defer nothing; their barrier side is vacuously satisfied.
        """
        claims_with_evidence = [
            claim
            for claim in inserted
            if staging_strings(claim.metadata, "place_mentions")
            or staging_strings(claim.metadata, "entities")
        ]
        if not claims_with_evidence:
            return
        policy = await self._place_policy_service.ensure_current(
            conn, edition_id=claims_with_evidence[0].edition_id
        )
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import resolve_place_mention

        deferred = 0
        for claim in claims_with_evidence:
            for raw in staging_strings(claim.metadata, "place_mentions"):
                mention, created = await self._places.create_mention(
                    conn, claim_id=claim.id, original_text=raw
                )
                if created:
                    await resolve_place_mention.configure(connection=conn).defer_async(
                        mention_id=mention.id, policy_id=policy.id
                    )
                    deferred += 1
            for raw in staging_strings(claim.metadata, "entities"):
                await self._places.create_entity(
                    conn, claim_id=claim.id, normalized_text=normalize_place_text(raw)
                )
        logger.info(
            "materialized place evidence for %d claims (policy=%s, deferred=%d)",
            len(claims_with_evidence),
            policy.id,
            deferred,
        )

    async def _replay_after_lost_slot(self, run: ClaimExtractionRun) -> ClaimExtractionResult:
        """Converge on whichever concurrent execution won the canonical slot."""
        async with self.uow.transaction() as conn:
            winner, _created = await self._run_repo.get_or_create_run(
                conn,
                source_item_revision_id=run.source_item_revision_id,
                edition_id=run.edition_id,
                extraction_policy_id=run.extraction_policy_id,
                relevance_decision_id=run.relevance_decision_id,
            )
            claims = await self._claim_repo.list_for_run(conn, winner.id)
        return ClaimExtractionResult(run=winner, claims=tuple(claims), replayed=True)

    def _system_prompt(self, edition: str) -> str:
        return (
            f'You are the local-news analyst for the "{edition}" edition. '
            "Extract immutable factual claims from one source message.\n\n"
            "Rules:\n"
            "- Each claim is one independent factual proposition. Split several "
            "assertions into separate claims because they differ SEMANTICALLY, "
            "not because the text has several sentences; a single-sentence post "
            "may hold two propositions and a seven-sentence post may hold none.\n"
            "- Every normalized_assertion must stand on its own as a complete, "
            "self-contained statement that keeps its full meaning without the "
            "surrounding message or conversation. NEVER emit a bare fragment "
            "such as «Всё ещё нет» alone; resolve deixis («тоже», «ещё нет», "
            "«там», «с восьми») using the provided conversation context.\n"
            "- Short complete claims are valid as they are: «Пожар у вокзала.» "
            "needs no padding. There are no minimum-word or minimum-sentence "
            "rules.\n"
            "- The conversation context (parent/root messages) exists only to "
            "clarify what the target message means; provenance always stays "
            "with the target message and context text must never be quoted as "
            "its own source.\n"
            "- Report place names verbatim in place_mentions (raw unresolved "
            "texts); entities optionally; event_time fields (start, end, "
            "precision, confidence, original_text) only when the message "
            "supports them.\n"
            "- Judge only the given material; never invent facts.\n\n"
            "Return EXACTLY one JSON object and nothing else:\n"
            '{"claims": [{"assertion_text": "<verbatim fragment>", '
            '"normalized_assertion": "<self-contained proposition>", '
            '"event_time": {"start": "<ISO datetime>", "end": "<ISO datetime>", '
            '"precision": "<hour|day|...>", "confidence": <number 0.0-1.0>, '
            '"original_text": "<verbatim time phrase>"}, '
            '"place_mentions": ["<raw text>"], "entities": ["<...>"]}]}'
        )

    def _user_payload(self, context: ClaimExtractionContext) -> str:
        conversation_context: dict[str, Any] | None = None
        if context.parent_text or context.root_text:
            conversation_context = {
                "parent_message": context.parent_text,
                "root_message": context.root_text,
            }
        return json.dumps(
            {
                "target_message": {
                    "text": (context.assertion_text or "").strip()
                    or "(no textual content was captured for this item)",
                    "metadata": context.payload,
                },
                "conversation_context": conversation_context,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
