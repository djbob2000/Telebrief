"""Story matching processing (Plan 3 Task 7): policy identity, the LLM
matcher contract, and the three-boundary orchestrator that turns one claim
embedding into a durable story assignment.

Boundary layout (ruling: the AI call NEVER runs inside a transaction):

1. ``_create_run_and_candidates`` — one short transaction persists the
   ``running`` run row and freezes the retriever's union output: every
   candidate keeps the exact ``story_revision_id`` the matcher will read,
   its nullable ``story_revision_embedding_id``, provenance flags/scores,
   and a deterministic rank. Retrieval scores are metadata only — no
   threshold ever decides assignment, and an empty candidate set is a valid
   input that leads to ``NEW_STORY``.
2. ``StoryMatcher.choose`` — one AI consultation OUTSIDE any transaction;
   the prompt carries the complete ``Claim.normalized_assertion`` plus each
   candidate's exact title/summary/current_state/semantic_text.
3. The apply transaction locks the run row (SELECT ... FOR UPDATE),
   rejects a SAME_STORY whose target revision moved (mark 'stale' +
   re-defer a fresh ``match_claim`` on the SAME connection), then applies
   the verdict, persists the immutable decision plus accepted relation
   proposals, defers ``embed_story_revision`` for any created revision, and
   finally flips the run succeeded. ``uq_story_match_success`` ON
   (claim_id, policy_id) WHERE status='succeeded' is THE canonical guard:
   duplicate executions converge on the winner instead of writing twice.

Execution locking: matching is sequential per edition via procrastinate's
EXECUTION lock (``lock=story-matching-edition:<edition_id>`` set at defer
time — verified against installed procrastinate 3.9, where ``lock`` means
"no two jobs with this string run simultaneously"). ``queueing_lock`` is
deliberately NOT used: it raises AlreadyEnqueued AT DEFER TIME, which would
abort the caller's atomic handoff transaction (embed_claim success, stale
rerun) and silently drop claims; the execution lock only serializes runs,
never drops them.

Deterministic failures (unparseable model output, non-enum assignment,
targetless SAME_STORY, a ``semantic_changed`` proposal without semantic
text) never retry: both the consultation and the apply share one handler
that finalizes them as ``failed(invalid_match_response)`` through the same
guarded write, so failed runs stay visible debt for bounded backfill while
a concurrently succeeded winner is never demoted.

Repositories never commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg

from src.ai_providers import AIProvider, ProviderCascadeError, ensure_provider_cascade
from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import Claim
from src.domain.stories import (
    NewStoryRevision,
    StoryMatchingPolicyVersion,
    StoryMatchingRun,
    StoryRevision,
)
from src.ingestion.repository import IngestionRepository
from src.processing.places import PlaceResolutionPolicyService
from src.processing.relevance import (
    ProviderUnavailableError,
    _parse_json_object,
)
from src.repositories.claims import ClaimRepository
from src.repositories.embeddings import PURPOSE_CLAIM_QUERY, EmbeddingRepository
from src.repositories.places import (
    PlaceRepository,
    PlaceResolutionPolicyRepository,
)
from src.repositories.stories import StoryRepository
from src.repositories.story_candidates import (
    LockedMatchingRun,
    StoryCandidateRetriever,
    StoryMatchingPolicyVersionRepository,
    StoryMatchingRunRepository,
)

logger = logging.getLogger(__name__)

STORY_MATCHING_PROMPT_VERSION = "v1"

DEFAULT_VECTOR_LIMIT = 20
DEFAULT_LEXICAL_LIMIT = 10
DEFAULT_STATE_FALLBACK_LIMIT = 20
DEFAULT_TOTAL_CANDIDATE_LIMIT = 40
DEFAULT_RESOLVED_LOOKBACK_DAYS = 30

DEFAULT_NEW_STORY_STATE = "open"
_REVISION_REASON = "story_match"

Assignment = Literal["SAME_STORY", "NEW_STORY"]
_VALID_ASSIGNMENTS: frozenset[str] = frozenset({"SAME_STORY", "NEW_STORY"})

_MAX_OUTPUT_TOKENS = 2048

_ENSURE_CURRENT_RACE_ATTEMPTS = 3


class InvalidMatchResponse(RuntimeError):
    """Deterministic matcher-contract violation; never retried."""


def story_matching_execution_lock(edition_id: int) -> str:
    """The per-edition execution lock string deferred onto every job."""
    return f"story-matching-edition:{edition_id}"


def story_matching_config_hash(
    *,
    embedding_model: str,
    embedding_dimensions: int,
    vector_limit: int,
    lexical_limit: int,
    state_fallback_limit: int,
    total_candidate_limit: int,
    resolved_lookback_days: int,
) -> str:
    """Deterministic digest of the complete matching retrieval config."""
    payload = json.dumps(
        {
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "vector_limit": vector_limit,
            "lexical_limit": lexical_limit,
            "state_fallback_limit": state_fallback_limit,
            "total_candidate_limit": total_candidate_limit,
            "resolved_lookback_days": resolved_lookback_days,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class StoryMatchingPolicyService:
    """Resolve-or-create the edition's current story matching policy version.

    Identity is the FULL retrieval config tuple (embedding model/dimensions
    plus per-signal limits, candidate cap and resolved-story lookback)
    digested into ``config_hash``; a changed config creates a NEW version
    instead of silently re-matching under different semantics. ``editions``
    carries no current-policy pointer column, so "current" is purely
    identity-resolved with latest-version-wins semantics; concurrent
    creators are arbitrated by uq_story_matching_policy_edition_version via
    a savepoint re-read loop.
    """

    def __init__(self, repo: StoryMatchingPolicyVersionRepository | None = None) -> None:
        self._repo = repo or StoryMatchingPolicyVersionRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        embedding_model: str,
        embedding_dimensions: int,
        vector_limit: int = DEFAULT_VECTOR_LIMIT,
        lexical_limit: int = DEFAULT_LEXICAL_LIMIT,
        state_fallback_limit: int = DEFAULT_STATE_FALLBACK_LIMIT,
        total_candidate_limit: int = DEFAULT_TOTAL_CANDIDATE_LIMIT,
        resolved_lookback_days: int = DEFAULT_RESOLVED_LOOKBACK_DAYS,
        prompt_version: str = STORY_MATCHING_PROMPT_VERSION,
    ) -> StoryMatchingPolicyVersion:
        config_hash = story_matching_config_hash(
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            vector_limit=vector_limit,
            lexical_limit=lexical_limit,
            state_fallback_limit=state_fallback_limit,
            total_candidate_limit=total_candidate_limit,
            resolved_lookback_days=resolved_lookback_days,
        )
        for _ in range(_ENSURE_CURRENT_RACE_ATTEMPTS):
            policies = await self._repo.list_for_edition(conn, edition_id)
            for policy in policies:
                if policy.config_hash == config_hash and policy.prompt_version == prompt_version:
                    return policy

            next_version = max((policy.version for policy in policies), default=0) + 1
            try:
                async with conn.transaction():
                    return await self._repo.insert(
                        conn,
                        edition_id=edition_id,
                        version=next_version,
                        config_hash=config_hash,
                        prompt_version=prompt_version,
                        embedding_model=embedding_model,
                        embedding_dimensions=embedding_dimensions,
                        vector_limit=vector_limit,
                        lexical_limit=lexical_limit,
                        state_fallback_limit=state_fallback_limit,
                        total_candidate_limit=total_candidate_limit,
                        resolved_lookback_days=resolved_lookback_days,
                    )
            except psycopg.errors.UniqueViolation:
                # A concurrent creator inserted version MAX+1 first; re-read
                # and converge on its row (duplicate policies are harmless).
                continue
        raise RuntimeError(
            f"could not ensure current story matching policy for edition {edition_id} "
            f"after {_ENSURE_CURRENT_RACE_ATTEMPTS} attempts"
        )


@dataclass(frozen=True)
class StoryUpdateProposal:
    """Optional SAME_STORY/NEW_STORY story-state payload from the matcher."""

    semantic_changed: bool
    title: str | None = None
    summary: str | None = None
    current_state: str | None = None
    semantic_text: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "semantic_changed": self.semantic_changed,
            "title": self.title,
            "summary": self.summary,
            "current_state": self.current_state,
            "semantic_text": self.semantic_text,
        }


@dataclass(frozen=True)
class RelationProposal:
    """One proposed cross-story relation awaiting confirmation."""

    to_story_id: int
    relation_type: str


@dataclass(frozen=True)
class MatchProposal:
    """Strict validated matcher verdict; the only shape the pipeline persists."""

    assignment: Assignment
    target_story_id: int | None = None
    story_update: StoryUpdateProposal | None = None
    relation_proposals: tuple[RelationProposal, ...] = ()
    confidence: float | None = None
    reason: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> MatchProposal:
        """Validate a model payload; anything off-contract is a typed error.

        Enum-only assignment ruling: ``assignment`` MUST be exactly
        ``SAME_STORY`` or ``NEW_STORY`` — there is no third verdict and no
        score-based shortcut to one. Malformed relation entries are dropped
        (a broken hint must not destroy an otherwise valid verdict); a
        targetless SAME_STORY invalidates the whole response.
        """
        if not isinstance(payload, dict):
            raise InvalidMatchResponse("match payload is not a JSON object")
        assignment = payload.get("assignment")
        if assignment not in _VALID_ASSIGNMENTS:
            raise InvalidMatchResponse(f"assignment must be one of {sorted(_VALID_ASSIGNMENTS)}")
        target_raw = payload.get("target_story_id")
        if assignment == "SAME_STORY":
            if isinstance(target_raw, bool) or not isinstance(target_raw, int) or target_raw <= 0:
                raise InvalidMatchResponse("SAME_STORY requires a positive target_story_id")
            target_story_id: int | None = target_raw
        else:
            # NEW_STORY ignores any echoed target: revision #1 is created.
            target_story_id = None
        story_update = cls._parse_update(payload.get("story_update"))
        proposals = cls._parse_proposals(payload.get("relation_proposals"))
        confidence = cls._parse_confidence(payload.get("confidence"))
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            reason = None
        return cls(
            assignment=assignment,
            target_story_id=target_story_id,
            story_update=story_update,
            relation_proposals=proposals,
            confidence=confidence,
            reason=(reason.strip() or None) if isinstance(reason, str) else None,
        )

    @staticmethod
    def _text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    @classmethod
    def _parse_update(cls, raw: Any) -> StoryUpdateProposal | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise InvalidMatchResponse("story_update must be a JSON object")
        changed = raw.get("semantic_changed")
        if not isinstance(changed, bool):
            raise InvalidMatchResponse("story_update.semantic_changed must be boolean")
        return StoryUpdateProposal(
            semantic_changed=changed,
            title=cls._text(raw.get("title")),
            summary=cls._text(raw.get("summary")),
            current_state=cls._text(raw.get("current_state")),
            semantic_text=cls._text(raw.get("semantic_text")),
        )

    @classmethod
    def _parse_proposals(cls, raw: Any) -> tuple[RelationProposal, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise InvalidMatchResponse("relation_proposals must be a list")
        parsed: list[RelationProposal] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            to_story_id = entry.get("to_story_id")
            relation_type = cls._text(entry.get("relation_type"))
            if isinstance(to_story_id, bool) or not isinstance(to_story_id, int):
                continue
            if to_story_id <= 0 or relation_type is None:
                continue
            parsed.append(RelationProposal(to_story_id=to_story_id, relation_type=relation_type))
        return tuple(parsed)

    @staticmethod
    def _parse_confidence(raw: Any) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidMatchResponse("confidence must be a number 0.0-1.0 or null")
        value = float(raw)
        if not 0.0 <= value <= 1.0:
            raise InvalidMatchResponse("confidence must be a number 0.0-1.0 or null")
        return value


@dataclass(frozen=True)
class MatcherCandidateView:
    """What the matcher sees for one frozen candidate: full revision texts
    plus the retrieval provenance as hints."""

    candidate: Any  # StoryCandidate-shaped (or the frozen DB projection)
    revision: StoryRevision

    @property
    def story_id(self) -> int:
        return int(self.candidate.story_id)

    @property
    def hints(self) -> dict[str, Any]:
        reasons = sorted(getattr(self.candidate, "retrieval_reasons", frozenset()) or ())
        return {
            "reasons": reasons,
            "vector_distance": getattr(self.candidate, "vector_distance", None),
            "lexical_score": getattr(self.candidate, "lexical_score", None),
            "location_overlap": getattr(self.candidate, "location_overlap", None),
            "entity_overlap": getattr(self.candidate, "entity_overlap", None),
        }


class StoryMatcher:
    """One AI consultation mapping a claim plus candidate stories to a
    validated :class:`MatchProposal`.

    Input contract (ruling): the model receives the COMPLETE
    ``Claim.normalized_assertion`` and each candidate's exact
    title/summary/current_state/semantic_text — never an isolated matched
    word or a score summary alone. Retrieval signals ride along explicitly
    labelled as hints.
    """

    def __init__(
        self,
        *,
        provider: AIProvider,
        model: str,
        provider_name: str = "primary",
        reasoning_effort: str | None = None,
        max_output_tokens: int = _MAX_OUTPUT_TOKENS,
    ) -> None:
        # Uniform cascade semantics even for single-slot providers.
        self.provider = ensure_provider_cascade(provider, logger=logger, slot_name=provider_name)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens

    async def choose(
        self,
        claim: Claim,
        views: Sequence[MatcherCandidateView],
        *,
        edition_name: str | None = None,
    ) -> MatchProposal:
        try:
            response = await self.provider.chat_completion(
                messages=[
                    {"role": "system", "content": self._system_prompt(edition_name)},
                    {"role": "user", "content": self._user_payload(claim, views)},
                ],
                model=self.model,
                max_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={"type": "json_object"},
            )
        except ProviderCascadeError as exc:
            # Same mapping rule as relevance/claims: every exhausted cascade
            # is provider unavailability for this attempt.
            raise ProviderUnavailableError(
                f"story matching provider unavailable ({exc.diagnostic_summary()})"
            ) from exc
        return MatchProposal.from_dict(_parse_json_object(response))

    def _system_prompt(self, edition_name: str | None) -> str:
        edition = f' for the "{edition_name}" edition' if edition_name else ""
        return (
            f"You are the local-news story editor{edition}. You decide whether "
            "one factual CLAIM continues an already-tracked story or starts a "
            "new one.\n\n"
            "Rules:\n"
            "- Judge ONLY the complete texts given below. Retrieval hints "
            "(vector distance, lexical score, reasons) are provenance "
            "metadata, never evidence; no score may decide the assignment.\n"
            '- "assignment" MUST be exactly "SAME_STORY" or "NEW_STORY" — '
            "there is no other verdict.\n"
            "- With no candidates listed, answer NEW_STORY.\n"
            '- For SAME_STORY set "target_story_id" to the chosen candidate\'s '
            "story_id.\n"
            '- Use "story_update" when the claim materially changes the '
            'story\'s meaning: "semantic_changed": true plus the FULL '
            "replacement fields (title/summary/current_state/semantic_text). "
            'With "semantic_changed": false omit replacement text entirely.\n'
            "- For NEW_STORY propose the first revision's fields inside "
            '"story_update"; the claim itself is the minimal meaning when you '
            "propose none.\n"
            '- "relation_proposals" optionally links the chosen/new story to '
            "OTHER listed stories; every to_story_id must be a listed story.\n\n"
            "Return EXACTLY one JSON object and nothing else:\n"
            '{"assignment": "<SAME_STORY|NEW_STORY>", "target_story_id": <int '
            'or null>, "story_update": {"semantic_changed": <bool>, '
            '"title": "<string or null>", "summary": "<string or null>", '
            '"current_state": "<string or null>", "semantic_text": "<string '
            'or null>"}, "relation_proposals": [{"to_story_id": <int>, '
            '"relation_type": "<string>"}], "confidence": <number 0.0-1.0 or '
            'null>, "reason": "<short explanation>"}'
        )

    def _user_payload(self, claim: Claim, views: Sequence[MatcherCandidateView]) -> str:
        return json.dumps(
            {
                "claim": {"normalized_assertion": claim.normalized_assertion},
                "candidates": [
                    {
                        "story_id": view.story_id,
                        "title": view.revision.title,
                        "summary": view.revision.summary,
                        "current_state": view.revision.current_state,
                        "semantic_text": view.revision.semantic_text,
                        "retrieval_hints": view.hints,
                    }
                    for view in views
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )


@dataclass(frozen=True)
class _MatchingContext:
    """Everything frozen in boundary one and consumed by boundaries two/three."""

    run: StoryMatchingRun
    claim: Claim
    policy: StoryMatchingPolicyVersion
    views: list[MatcherCandidateView]
    edition_name: str | None


@dataclass(frozen=True)
class _ApplyResult:
    story_id: int | None
    created_revision: StoryRevision | None
    proposal_entries: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class StoryMatchingOutcome:
    """Result of one orchestrated matching execution."""

    run: StoryMatchingRun | None
    decision: MatchProposal | None = None
    story_id: int | None = None
    revision: StoryRevision | None = None
    replayed: bool = False
    stale_rerun_deferred: bool = False
    degraded: str | None = None


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class StoryMatchingService:
    """Orchestrate one claim embedding through retrieve -> match -> apply.

    The policy id is always the caller-supplied one: a retried job keeps the
    originally queued policy even when a newer version became current, so
    verdicts stay attributable to their exact prompt/config identity.
    """

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        matcher: Any,
        runs: StoryMatchingRunRepository | None = None,
        policies: StoryMatchingPolicyVersionRepository | None = None,
        stories: StoryRepository | None = None,
        claims: ClaimRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        retriever: Any = None,
        ingestion: IngestionRepository | None = None,
    ) -> None:
        self.uow = uow
        self.matcher = matcher
        self._runs = runs or StoryMatchingRunRepository()
        self._policies = policies or StoryMatchingPolicyVersionRepository()
        self._stories = stories or StoryRepository()
        self._claims = claims or ClaimRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._retriever = retriever if retriever is not None else StoryCandidateRetriever()
        self._ingestion = ingestion or IngestionRepository()

    async def run(
        self, claim_id: int, policy_id: int, claim_embedding_id: int
    ) -> StoryMatchingOutcome:
        """Execute the three-boundary flow for one claim/policy/embedding."""
        context = await self._create_run_and_candidates(claim_id, policy_id, claim_embedding_id)
        if isinstance(context, StoryMatchingRun):  # canonical winner replay
            return StoryMatchingOutcome(run=context, replayed=True)

        # The consultation AND its output validation share one handler:
        # _parse_json_object -> MatchProposal.from_dict failures are
        # deterministic contract violations that must reach the guarded
        # terminal path instead of killing the job with a stuck 'running'
        # run (which backfill would count as coverage, silently dropping
        # the claim forever).
        try:
            decision = await self.matcher.choose(
                context.claim, context.views, edition_name=context.edition_name
            )
            logger.info(
                "story matcher verdict claim=%s policy=%s run=%s assignment=%s target=%s",
                claim_id,
                policy_id,
                context.run.id,
                decision.assignment,
                decision.target_story_id,
            )
            async with self.uow.transaction() as conn:
                outcome = await self._apply(conn, context, decision)
        except psycopg.errors.UniqueViolation as exc:
            if (
                getattr(exc, "diag", None) is None
                or exc.diag.constraint_name != "uq_story_match_success"
            ):
                raise
            return await self._converge_existing_success(claim_id, policy_id)
        except InvalidMatchResponse as exc:
            return await self._finalize_invalid_response(context.run, exc)
        return outcome

    # ------------------------------------------------------------------
    # Boundary 1: run row + frozen candidates (one short transaction)
    # ------------------------------------------------------------------

    async def _create_run_and_candidates(
        self, claim_id: int, policy_id: int, claim_embedding_id: int
    ) -> _MatchingContext | StoryMatchingRun:
        """Freeze everything the matcher will see, then hand the frozen
        snapshot out of the transaction.

        Single short transaction (documented choice): replay check, run
        insert/reuse, retrieval and candidate freezing share one consistent
        snapshot; nothing slow happens inside — no AI call, no pool hold
        beyond local queries."""
        async with self.uow.transaction() as conn:
            succeeded = await self._runs.find_succeeded(
                conn, claim_id=claim_id, policy_id=policy_id
            )
            if succeeded is not None:
                return succeeded

            claims = await self._claims.get_many(conn, [claim_id])
            if not claims:
                raise ValueError(f"claim {claim_id} does not exist")
            claim = claims[0]
            policy = await self._load_policy(conn, policy_id, claim.edition_id)
            embedded = await self._load_embedding(conn, claim_id, claim_embedding_id, policy)
            edition_name = await self._ingestion.get_edition_name(conn, claim.edition_id)

            run = await self._runs.latest_running(conn, claim_id=claim_id, policy_id=policy_id)
            if run is None:
                run = await self._runs.insert_running(
                    conn,
                    claim_id=claim_id,
                    edition_id=claim.edition_id,
                    policy_id=policy_id,
                    claim_embedding_id=claim_embedding_id,
                )
            frozen = await self._runs.frozen_candidates(conn, run.id)
            if not frozen:
                retrieved = await self._retriever.retrieve(
                    conn, claim=claim, claim_embedding=embedded.vector, policy=policy
                )
                await self._runs.save_candidates(conn, run_id=run.id, candidates=retrieved)
                frozen = await self._runs.frozen_candidates(conn, run.id)
            revisions = {
                revision.id: revision
                for revision in await self._stories.get_revisions(
                    conn, [candidate.story_revision_id for candidate in frozen]
                )
            }
            views = [
                MatcherCandidateView(
                    candidate=candidate, revision=revisions[candidate.story_revision_id]
                )
                for candidate in frozen
            ]
        return _MatchingContext(
            run=run, claim=claim, policy=policy, views=views, edition_name=edition_name
        )

    async def _load_policy(
        self, conn: psycopg.AsyncConnection, policy_id: int, edition_id: int
    ) -> StoryMatchingPolicyVersion:
        policy = await self._policies.get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"story matching policy {policy_id} does not belong to edition {edition_id}"
            )
        return policy

    async def _load_embedding(
        self,
        conn: psycopg.AsyncConnection,
        claim_id: int,
        claim_embedding_id: int,
        policy: StoryMatchingPolicyVersion,
    ):
        embedded = await self._embeddings.get_claim_embedding_row(
            conn, embedding_id=claim_embedding_id
        )
        if embedded is None:
            raise ValueError(f"claim embedding {claim_embedding_id} does not exist")
        if embedded.claim_id != claim_id:
            raise ValueError(
                f"claim embedding {claim_embedding_id} belongs to claim "
                f"{embedded.claim_id}, not {claim_id}"
            )
        # A stale rerun keeps the same immutable ClaimEmbedding unless a new
        # policy explicitly selects another space: incompatible spaces fail
        # loudly instead of silently retrieving across vector worlds.
        if (
            embedded.model != policy.embedding_model
            or embedded.dimensions != policy.embedding_dimensions
            or embedded.purpose != PURPOSE_CLAIM_QUERY
        ):
            raise ValueError(
                f"claim embedding {claim_embedding_id} "
                f"({embedded.model}/{embedded.dimensions}/{embedded.purpose}) "
                f"is incompatible with policy {policy.id} "
                f"({policy.embedding_model}/{policy.embedding_dimensions})"
            )
        return embedded

    # ------------------------------------------------------------------
    # Boundary 3: locked stale check + atomic apply
    # ------------------------------------------------------------------

    async def _apply(
        self,
        conn: psycopg.AsyncConnection,
        context: _MatchingContext,
        decision: MatchProposal,
    ) -> StoryMatchingOutcome:
        locked = await self._runs.lock_with_candidates(conn, context.run.id)
        if locked is None:
            raise RuntimeError(f"matching run {context.run.id} vanished before apply")

        known_stories = {candidate.story_id for candidate in locked.candidates}
        if decision.assignment == "SAME_STORY" and decision.target_story_id not in known_stories:
            raise InvalidMatchResponse(
                f"SAME_STORY target {decision.target_story_id} is not among the "
                f"frozen candidates {sorted(known_stories)}"
            )
        target_story_id = decision.target_story_id

        if target_story_id is not None:
            candidate = locked.candidate_for(target_story_id)
            current_revision_id = await self._stories.current_revision_id(conn, target_story_id)
            if candidate.story_revision_id != current_revision_id:
                # The world moved between freeze and apply: never apply over
                # a stale read — mark stale and re-defer on the SAME conn.
                await self._runs.mark_stale(conn, locked.run.id, completed_at=_now())
                await self._defer_rerun(conn, context, locked)
                logger.warning(
                    "stale matching target story=%s run=%s; rerun deferred",
                    target_story_id,
                    locked.run.id,
                )
                return StoryMatchingOutcome(
                    run=locked.run, decision=decision, stale_rerun_deferred=True
                )

        applied = await self._apply_decision(conn, context, locked, decision, known_stories)
        await self._runs.insert_decision(
            conn,
            run_id=locked.run.id,
            assignment=decision.assignment,
            target_story_id=applied.story_id if decision.assignment == "SAME_STORY" else None,
            story_update=decision.story_update.payload() if decision.story_update else None,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        await self._runs.insert_relation_proposals(
            conn, run_id=locked.run.id, entries=applied.proposal_entries
        )
        if applied.created_revision is not None:
            await self._defer_revision_embedding(conn, context.policy, applied.created_revision)
        if applied.story_id is not None:
            await self._defer_evidence_assessment(conn, context.claim.edition_id, applied.story_id)
        await self._runs.mark_succeeded(conn, locked.run.id, completed_at=_now())
        return StoryMatchingOutcome(
            run=locked.run,
            decision=decision,
            story_id=applied.story_id,
            revision=applied.created_revision,
        )

    async def _apply_decision(
        self,
        conn: psycopg.AsyncConnection,
        context: _MatchingContext,
        locked: LockedMatchingRun,
        decision: MatchProposal,
        known_stories: set[int],
    ) -> _ApplyResult:
        """Attach/create according to the verdict; all payloads are validated
        BEFORE any write so a malformed proposal cannot half-apply."""
        claim = context.claim

        if decision.assignment == "NEW_STORY":
            revision_payload = self._new_story_revision(claim, decision.story_update)
            created = await self._stories.create_story_with_revision(
                conn,
                edition_id=claim.edition_id,
                claim_id=claim.id,
                revision=revision_payload,
            )
            return _ApplyResult(
                story_id=created.story_id,
                created_revision=created.revision,
                proposal_entries=self._validated_proposals(
                    created.story_id, decision, known_stories
                ),
            )

        if decision.target_story_id is None:  # unreachable for validated SAME_STORY
            raise InvalidMatchResponse("SAME_STORY requires a target story")
        target_story_id = decision.target_story_id
        await self._stories.attach_claim(
            conn, story_id=target_story_id, claim_id=claim.id, attached_at=_now()
        )
        created_revision: StoryRevision | None = None
        if decision.story_update is not None and decision.story_update.semantic_changed:
            current_revision_id = await self._stories.current_revision_id(conn, target_story_id)
            if current_revision_id is None:
                raise RuntimeError(f"story {target_story_id} has no current revision before apply")
            current = await self._stories.get_revision(conn, current_revision_id)
            if current is None:
                raise RuntimeError(
                    f"story {target_story_id} lost its current revision before apply"
                )
            revision_payload = self._proposed_revision(current, decision.story_update)
            created_revision = await self._stories.create_revision_if_semantic_change(
                conn,
                story_id=target_story_id,
                semantic_changed=True,
                revision=revision_payload,
            )
        return _ApplyResult(
            story_id=target_story_id,
            created_revision=created_revision,
            proposal_entries=self._validated_proposals(target_story_id, decision, known_stories),
        )

    def _new_story_revision(
        self, claim: Claim, update: StoryUpdateProposal | None
    ) -> NewStoryRevision:
        """Revision #1 for a brand-new story: proposed fields when valid, the
        whole normalized assertion as the minimal derived meaning otherwise."""
        semantic_text = _strip_or_none(update.semantic_text if update else None)
        if semantic_text is None:
            semantic_text = claim.normalized_assertion.strip()
        if not semantic_text:
            raise InvalidMatchResponse(
                "NEW_STORY requires a non-empty semantic_text or normalized_assertion"
            )
        return NewStoryRevision(
            current_state=(
                _strip_or_none(update.current_state if update else None) or DEFAULT_NEW_STORY_STATE
            ),
            semantic_text=semantic_text,
            content_hash=_content_hash(semantic_text),
            created_at=_now(),
            title=_strip_or_none(update.title if update else None),
            summary=_strip_or_none(update.summary if update else None),
            reason=_REVISION_REASON,
        )

    def _proposed_revision(
        self, current: StoryRevision, update: StoryUpdateProposal
    ) -> NewStoryRevision:
        """A SAME_STORY material change becomes a full replacement payload
        merged over the current revision; an empty semantic_text is a
        deterministic contract violation (raised before any write)."""
        semantic_text = _strip_or_none(update.semantic_text)
        if semantic_text is None:
            raise InvalidMatchResponse(
                "story_update.semantic_changed=true requires non-empty semantic_text"
            )
        return NewStoryRevision(
            current_state=_strip_or_none(update.current_state) or current.current_state,
            semantic_text=semantic_text,
            content_hash=_content_hash(semantic_text),
            created_at=_now(),
            title=_strip_or_none(update.title) or current.title,
            summary=_strip_or_none(update.summary) or current.summary,
            reason=_REVISION_REASON,
        )

    @staticmethod
    def _validated_proposals(
        from_story_id: int, decision: MatchProposal, known_stories: set[int]
    ) -> list[tuple[int, int, str]]:
        """Accept only proposals pointing at stories the matcher actually saw."""
        entries: list[tuple[int, int, str]] = []
        for proposal in decision.relation_proposals:
            if proposal.to_story_id == from_story_id or proposal.to_story_id not in known_stories:
                logger.warning(
                    "dropped relation proposal to unlisted story %s", proposal.to_story_id
                )
                continue
            entries.append((from_story_id, proposal.to_story_id, proposal.relation_type))
        return entries

    async def _defer_rerun(
        self, conn: psycopg.AsyncConnection, context: _MatchingContext, locked: LockedMatchingRun
    ) -> None:
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import match_claim

        claim_embedding_id = locked.run.claim_embedding_id
        if claim_embedding_id is None:
            raise RuntimeError(f"matching run {locked.run.id} lost its claim embedding id")
        await match_claim.configure(
            connection=conn,
            lock=story_matching_execution_lock(context.claim.edition_id),
        ).defer_async(
            claim_id=context.claim.id,
            policy_id=context.policy.id,
            claim_embedding_id=claim_embedding_id,
        )

    async def _defer_revision_embedding(
        self,
        conn: psycopg.AsyncConnection,
        policy: StoryMatchingPolicyVersion,
        revision: StoryRevision,
    ) -> None:
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import embed_story_revision

        await embed_story_revision.configure(connection=conn).defer_async(
            story_revision_id=revision.id,
            model=policy.embedding_model,
            dimensions=policy.embedding_dimensions,
        )

    async def _defer_evidence_assessment(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int,
        story_id: int,
    ) -> None:
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import assess_evidence
        from src.processing.evidence import EvidencePolicyService

        current_revision_id = await self._stories.current_revision_id(conn, story_id)
        if current_revision_id is None:
            return
        policy = await EvidencePolicyService().ensure_current(conn, edition_id=edition_id)
        await assess_evidence.configure(connection=conn).defer_async(
            story_id=story_id,
            story_revision_id=current_revision_id,
            policy_id=policy.id,
        )

    # ------------------------------------------------------------------
    # Convergence helpers (canonical guard + deterministic degradation)
    # ------------------------------------------------------------------

    async def _converge_existing_success(
        self, claim_id: int, policy_id: int
    ) -> StoryMatchingOutcome:
        """Duplicate execution lost the canonical index race: converge on the
        winner's successful run instead of writing a second success."""
        async with self.uow.transaction() as conn:
            winner = await self._runs.find_succeeded(conn, claim_id=claim_id, policy_id=policy_id)
        if winner is None:
            raise RuntimeError(
                f"uq_story_match_success fired but no succeeded run exists for "
                f"claim={claim_id} policy={policy_id}"
            )
        logger.warning(
            "duplicate match_claim converged on succeeded run=%s claim=%s policy=%s",
            winner.id,
            claim_id,
            policy_id,
        )
        return StoryMatchingOutcome(run=winner, replayed=True)

    async def _finalize_invalid_response(
        self, run: StoryMatchingRun, exc: InvalidMatchResponse
    ) -> StoryMatchingOutcome:
        """Deterministic contract violations never retry: the guarded write
        marks the run failed(invalid_match_response) unless a concurrent winner
        already holds the canonical slot."""
        async with self.uow.transaction() as conn:
            demoted = await self._runs.mark_failed(
                conn, run.id, error_kind="invalid_match_response", completed_at=_now()
            )
            final = await self._runs.get(conn, run.id)
        if not demoted:
            logger.warning(
                "invalid match response for run=%s but the slot moved on; converging (%s)",
                run.id,
                exc,
            )
        else:
            logger.warning("invalid match response run=%s: %s", run.id, exc)
        return StoryMatchingOutcome(run=final, degraded="invalid_match_response")

    async def finalize_provider_failure(
        self, claim_id: int, policy_id: int
    ) -> StoryMatchingOutcome:
        """Terminal fail-open write after retries are exhausted: the open run
        completes as failed(provider_unavailable); a concurrently succeeded
        canonical winner is NEVER demoted (guarded mark_failed refuses)."""
        async with self.uow.transaction() as conn:
            run = await self._runs.latest_running(conn, claim_id=claim_id, policy_id=policy_id)
            if run is None:
                run = await self._runs.find_succeeded(conn, claim_id=claim_id, policy_id=policy_id)
                if run is not None:
                    return StoryMatchingOutcome(run=run, replayed=True)
                return StoryMatchingOutcome(run=None, degraded="provider_unavailable")
            demoted = await self._runs.mark_failed(
                conn, run.id, error_kind="provider_unavailable", completed_at=_now()
            )
            final = await self._runs.get(conn, run.id)
        if demoted:
            logger.warning(
                "story matching degraded provider_unavailable run=%s claim=%s", run.id, claim_id
            )
        else:
            logger.warning(
                "provider failure for run=%s lost the canonical race; converging", run.id
            )
        return StoryMatchingOutcome(run=final, degraded="provider_unavailable")


class StoryMatchingPrerequisiteService:
    """The Task 8 barrier between claim evidence and story matching.

    ``maybe_schedule`` is THE single gate replacing the direct match_claim
    defer in the embed_claim success transaction. Matching queues ONLY when:

    1. a compatible ClaimEmbedding exists — deterministically the LATEST
       claim_query vector of the claim (the frozen space every downstream
       check validates against), and
    2. EVERY ``claim_place_mentions`` row of the claim holds a completed
       result (resolved OR explicit unresolved) under the edition's CURRENT
       place-resolution policy. Claims with zero mentions satisfy the
       barrier vacuously and queue immediately; unresolved geography never
       blocks anything once its explicit outcome exists.

    When both hold it freezes the current place-policy id, resolves/creates
    the exact StoryMatchingPolicyVersion for the embedding's space, and
    defers ``match_claim(claim_id, policy_id, claim_embedding_id)`` ONCE on
    the caller's connection — atomic with whatever made the claim ready.
    Duplicate defers (at-least-once executions) converge downstream on the
    canonical succeeded run keyed by (claim, policy).

    Returns True only when matching was actually scheduled.
    """

    def __init__(
        self,
        *,
        claims: ClaimRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        places: PlaceRepository | None = None,
        place_policies: PlaceResolutionPolicyRepository | None = None,
        place_policy_service: PlaceResolutionPolicyService | None = None,
        matching_policy_service: StoryMatchingPolicyService | None = None,
    ) -> None:
        self._claims = claims or ClaimRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._places = places or PlaceRepository()
        self._place_policies = place_policies or PlaceResolutionPolicyRepository()
        self._place_policy_service = place_policy_service or PlaceResolutionPolicyService(
            self._place_policies
        )
        self._matching_policy_service = matching_policy_service or StoryMatchingPolicyService()

    async def maybe_schedule(self, conn: psycopg.AsyncConnection, *, claim_id: int) -> bool:
        claims = await self._claims.get_many(conn, [claim_id])
        if not claims:
            raise ValueError(f"claim {claim_id} does not exist")
        claim = claims[0]

        embedding = await self._embeddings.latest_claim_embedding_identity(conn, claim_id=claim.id)
        if embedding is None:
            return False

        place_policy = await self._place_policy_service.ensure_current(
            conn, edition_id=claim.edition_id
        )
        satisfied = await self._places.barrier_satisfied(
            conn, claim_id=claim.id, policy_id=place_policy.id
        )
        if not satisfied:
            return False

        policy = await self._matching_policy_service.ensure_current(
            conn,
            edition_id=claim.edition_id,
            embedding_model=embedding.model,
            embedding_dimensions=embedding.dimensions,
        )
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import match_claim

        await match_claim.configure(
            connection=conn,
            lock=story_matching_execution_lock(claim.edition_id),
        ).defer_async(
            claim_id=claim.id,
            policy_id=policy.id,
            claim_embedding_id=embedding.id,
        )
        logger.info(
            "prerequisites satisfied; deferred match_claim claim=%s policy=%s embedding=%s "
            "place_policy=%s",
            claim.id,
            policy.id,
            embedding.id,
            place_policy.id,
        )
        return True
