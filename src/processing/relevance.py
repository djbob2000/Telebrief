"""Edition relevance processing: policy resolution, AI verdicts, persistence.

Fail-open contract (spec §12-14): the AI provider is the only component
allowed to call a revision irrelevant. When the provider cannot produce a
usable verdict the pipeline degrades to ``uncertain`` — never to a negative
verdict — and there is no deterministic keyword gate in front of the model:
every revision reaches the AI as valid non-empty text.

Provider adapter mapping (thin, the cascade is not rewritten):

* ``ProviderCascade.chat_completion`` is the single AI entry point; the
  service wraps any raw provider with :func:`ensure_provider_cascade` so the
  failure surface is uniform.
* A fully exhausted cascade raises :class:`ProviderCascadeError`. EVERY such
  error is mapped to :class:`ProviderUnavailableError`, not just pure outages
  (``is_pure_outage``): a cascade that returns nothing yields no verdict this
  attempt, and token-budget / context-size storms must degrade to uncertain
  rather than invite a hallucinated replacement verdict. The structured
  ``failure_kinds`` are preserved in the exception text via
  ``diagnostic_summary()`` for safe logging (slot:kind:exception, no raw
  provider payloads).
* :class:`ProviderUnavailableError` drives the bounded retry gate in
  ``src/jobs/processing.py``: attempts 0 and 1 raise
  :class:`TransientProcessingError` (the only retryable exception), and the
  final attempt persists ``uncertain(provider_unavailable)`` and returns
  successfully.
* Malformed or non-JSON model output is validated into
  ``uncertain("invalid_ai_response")`` by :meth:`RelevanceResult.from_dict`
  and persisted immediately (retrying cannot fix a deterministic parse).

Repositories never commit; the caller owns transaction boundaries. The AI
call runs OUTSIDE any transaction so a slow provider never holds a pooled
connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
import yaml

from src.ai_providers import (
    AIProvider,
    ProviderCascadeError,
    ensure_provider_cascade,
)
from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import EditionRelevanceDecision, RelevancePolicyVersion
from src.ingestion.repository import IngestionRepository
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)

logger = logging.getLogger(__name__)

RELEVANCE_PROMPT_VERSION = "relevance-2026-08-v1"

RelevanceStatus = Literal["relevant", "irrelevant", "uncertain", "needs_media"]
_VALID_STATUSES: frozenset[str] = frozenset({"relevant", "irrelevant", "uncertain", "needs_media"})

_INVALID_RESPONSE_REASON = "invalid_ai_response"
_NO_TEXT_PLACEHOLDER = "(no textual content was captured for this item)"
_EMPTY_REASON_PLACEHOLDER = "(model returned no reason)"
_MAX_OUTPUT_TOKENS = 1024

_ENSURE_CURRENT_RACE_ATTEMPTS = 3


class TransientProcessingError(RuntimeError):
    """The only exception Procrastinate's relevance retry strategy retries."""


class ProviderUnavailableError(RuntimeError):
    """The AI cascade returned no usable verdict for this attempt."""


@dataclass(frozen=True)
class RelevanceResult:
    """Strict validated AI verdict; the only shape the pipeline persists."""

    status: RelevanceStatus
    confidence: float | None
    reason: str

    @classmethod
    def from_dict(cls, payload: Any) -> RelevanceResult:
        """Validate a model payload; anything malformed becomes uncertain.

        The mapping is deliberately total: an unparseable verdict must never
        crash the pipeline nor be interpreted as a negative decision.
        """
        if not isinstance(payload, dict):
            return cls(status="uncertain", confidence=None, reason=_INVALID_RESPONSE_REASON)
        status = payload.get("status")
        if status not in _VALID_STATUSES:
            return cls(status="uncertain", confidence=None, reason=_INVALID_RESPONSE_REASON)
        confidence_raw = payload.get("confidence")
        if confidence_raw is None:
            confidence = None
        elif isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
            return cls(status="uncertain", confidence=None, reason=_INVALID_RESPONSE_REASON)
        elif not 0.0 <= float(confidence_raw) <= 1.0:
            return cls(status="uncertain", confidence=None, reason=_INVALID_RESPONSE_REASON)
        else:
            confidence = float(confidence_raw)
        reason_raw = payload.get("reason")
        if not isinstance(reason_raw, str):
            return cls(status="uncertain", confidence=None, reason=_INVALID_RESPONSE_REASON)
        reason = reason_raw.strip() or _EMPTY_REASON_PLACEHOLDER
        return cls(status=status, confidence=confidence, reason=reason)


@dataclass(frozen=True)
class RevisionMaterial:
    """Everything the AI sees about one revision (text is never empty downstream)."""

    text_content: str | None
    payload: dict[str, Any]
    assets: tuple[dict[str, Any], ...]


def relevance_config_hash(*, ai_provider: str, ai_model: str) -> str:
    """Stable hash of the verdict-affecting AI identity (provider + model).

    A change in either value starts a new relevance policy version, keeping
    historical decisions attributable to the exact configuration that made
    them. The prompt version is tracked separately in its own column.
    """
    canonical = json.dumps(
        {"ai_model": ai_model, "ai_provider": ai_provider},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_relevance_config_hash(config_path: str | None = None) -> str:
    """Read the AI identity leniently from config.yaml for the ingestion wiring.

    Deliberately does NOT run full credential validation: ingestion must be
    able to compute the policy identity (and defer jobs) even on hosts whose
    AI credentials are only available to the worker process. Missing file or
    missing keys fall back to the documented defaults.
    """
    settings: dict[str, Any] = {}
    try:
        path = config_path or "config.yaml"
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if isinstance(raw, dict):
            candidate = raw.get("settings")
            if isinstance(candidate, dict):
                settings = candidate
    except (OSError, yaml.YAMLError):
        settings = {}

    provider = str(settings.get("ai_provider") or "openai").lower()
    if provider == "openai":
        model = settings.get("ai_model") or settings.get("openai_model") or "gpt-5-nano"
    else:
        model = settings.get("ai_model") or ""
    return relevance_config_hash(ai_provider=provider, ai_model=str(model))


@dataclass(frozen=True)
class IngestionRelevanceWiring:
    """Collaborators the ingestion service needs to defer relevance jobs."""

    policy_service: RelevancePolicyService
    config_hash: str
    prompt_version: str

    @classmethod
    def create(cls, *, config_path: str | None = None) -> IngestionRelevanceWiring:
        """Build wiring from the lenient on-disk config identity."""
        return cls(
            policy_service=RelevancePolicyService(),
            config_hash=resolve_relevance_config_hash(config_path),
            prompt_version=RELEVANCE_PROMPT_VERSION,
        )


class RelevancePolicyService:
    """Resolve-or-create the edition's current relevance policy version.

    Identity is (edition_id, config_hash, prompt_version). A matching policy
    is returned as-is; otherwise a new version = MAX+1 row is created and the
    edition's current pointer is moved onto it. All writes happen inside the
    caller's transaction. Concurrent creators are arbitrated by
    uq_relevance_policy_edition_version via a savepoint re-read loop.
    """

    def __init__(self, repo: RelevancePolicyVersionRepository | None = None) -> None:
        self._repo = repo or RelevancePolicyVersionRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
    ) -> RelevancePolicyVersion:
        for _ in range(_ENSURE_CURRENT_RACE_ATTEMPTS):
            policies = await self._repo.list_for_edition(conn, edition_id)
            for policy in policies:
                if policy.config_hash == config_hash and policy.prompt_version == prompt_version:
                    await self._repo.set_current(conn, edition_id=edition_id, policy_id=policy.id)
                    return policy

            next_version = max((policy.version for policy in policies), default=0) + 1
            try:
                async with conn.transaction():
                    created = await self._repo.insert(
                        conn,
                        edition_id=edition_id,
                        version=next_version,
                        config_hash=config_hash,
                        prompt_version=prompt_version,
                    )
                    await self._repo.set_current(conn, edition_id=edition_id, policy_id=created.id)
                    return created
            except psycopg.errors.UniqueViolation:
                # A concurrent creator inserted version MAX+1 first; re-read
                # and converge on its row (duplicate policies are harmless).
                continue
        raise RuntimeError(
            f"could not ensure current relevance policy for edition {edition_id} "
            f"after {_ENSURE_CURRENT_RACE_ATTEMPTS} attempts"
        )


class RelevanceService:
    """Evaluate one revision against one exact policy and persist the verdict.

    The policy id is always the caller-supplied one: a retried job keeps the
    originally queued policy even if a newer version became current in the
    meantime, so decisions stay attributable to their exact prompt/config.
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
        policy_repo: RelevancePolicyVersionRepository | None = None,
        decision_repo: EditionRelevanceDecisionRepository | None = None,
    ) -> None:
        self.uow = uow
        # Uniform cascade semantics even for single-slot providers.
        self.provider = ensure_provider_cascade(provider, logger=logger, slot_name=provider_name)
        self.model = model
        self.provider_name = provider_name
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self._ingestion_repo = ingestion_repo or IngestionRepository()
        self._policy_repo = policy_repo or RelevancePolicyVersionRepository()
        self._decision_repo = decision_repo or EditionRelevanceDecisionRepository()

    async def evaluate(
        self, source_item_revision_id: int, edition_id: int, policy_id: int
    ) -> EditionRelevanceDecision:
        """Consult the AI once and persist one immutable root decision."""
        async with self.uow.transaction() as conn:
            material, edition_name, policy = await self._load_inputs(
                conn,
                source_item_revision_id=source_item_revision_id,
                edition_id=edition_id,
                policy_id=policy_id,
            )

        result = await self.decide_revision(material, edition_name, policy)
        logger.info(
            "relevance verdict revision=%s edition=%s policy=%s status=%s",
            source_item_revision_id,
            edition_id,
            policy_id,
            result.status,
        )
        return await self._persist_result(
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            policy=policy,
            result=result,
        )

    async def persist_uncertain(
        self,
        source_item_revision_id: int,
        edition_id: int,
        policy_id: int,
        *,
        reason: str,
    ) -> EditionRelevanceDecision:
        """Fail-open terminal write: uncertain, no confidence, explained."""
        policy = await self._get_policy_or_raise(policy_id, edition_id)
        return await self._persist_result(
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            policy=policy,
            result=RelevanceResult(status="uncertain", confidence=None, reason=reason),
        )

    async def decide_revision(
        self,
        revision: RevisionMaterial,
        edition: str,
        policy: RelevancePolicyVersion,
    ) -> RelevanceResult:
        """One AI consultation; maps cascade exhaustion to ProviderUnavailableError."""
        response = await self._complete(
            messages=[
                {"role": "system", "content": self._system_prompt(edition, policy)},
                {"role": "user", "content": self._source_payload(revision)},
            ]
        )
        return RelevanceResult.from_dict(_parse_json_object(response))

    async def _complete(self, *, messages: list[dict[str, str]]) -> str:
        try:
            return await self.provider.chat_completion(
                messages=messages,
                model=self.model,
                max_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={"type": "json_object"},
            )
        except ProviderCascadeError as exc:
            # Mapping documented in the module docstring: every exhausted
            # cascade is provider unavailability for this attempt.
            raise ProviderUnavailableError(
                f"relevance provider unavailable ({exc.diagnostic_summary()})"
            ) from exc

    async def _load_inputs(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        policy_id: int,
    ) -> tuple[RevisionMaterial, str, RelevancePolicyVersion]:
        revision = await self._ingestion_repo.get_revision(conn, source_item_revision_id)
        if revision is None:
            raise ValueError(f"source item revision {source_item_revision_id} does not exist")
        policy = await self._policy_repo.get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"relevance policy {policy_id} does not belong to edition {edition_id}"
            )
        assets = await self._ingestion_repo.list_asset_summaries(conn, source_item_revision_id)
        edition_name = await self._ingestion_repo.get_edition_name(conn, edition_id)
        if edition_name is None:
            raise ValueError(f"edition {edition_id} does not exist")
        material = RevisionMaterial(
            text_content=revision.text_content,
            payload=revision.payload,
            assets=tuple(assets),
        )
        return material, edition_name, policy

    async def _get_policy_or_raise(self, policy_id: int, edition_id: int) -> RelevancePolicyVersion:
        async with self.uow.transaction() as conn:
            policy = await self._policy_repo.get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"relevance policy {policy_id} does not belong to edition {edition_id}"
            )
        return policy

    async def _persist_result(
        self,
        *,
        source_item_revision_id: int,
        edition_id: int,
        policy: RelevancePolicyVersion,
        result: RelevanceResult,
    ) -> EditionRelevanceDecision:
        async with self.uow.transaction() as conn:
            try:
                async with conn.transaction():
                    return await self._decision_repo.insert_root(
                        conn,
                        source_item_revision_id=source_item_revision_id,
                        edition_id=edition_id,
                        relevance_policy_id=policy.id,
                        status=result.status,
                        confidence=result.confidence,
                        reason=result.reason,
                        provider=self.provider_name,
                        model=self.model,
                    )
            except psycopg.errors.UniqueViolation:
                # Duplicate execution: the canonical root row already exists
                # and is immutable, so return it instead of the fresh verdict.
                existing = await self._decision_repo.get_root(
                    conn,
                    source_item_revision_id=source_item_revision_id,
                    edition_id=edition_id,
                    relevance_policy_id=policy.id,
                )
                if existing is None:
                    raise
                return existing

    def _system_prompt(self, edition: str, policy: RelevancePolicyVersion) -> str:
        return (
            f'You are the local-news relevance editor for the "{edition}" edition. '
            "Decide whether one source post deserves coverage in this edition.\n\n"
            "Rules:\n"
            "- The source post is context, not proof: you are judging whether the "
            "topic matters locally, not verifying the claim.\n"
            "- A single resident observation can be relevant: first-hand local "
            "reports about everyday infrastructure and community life are exactly "
            "what this edition exists for.\n"
            "- Absence of corroboration is not irrelevance: do not demand "
            "additional sources before calling a local report relevant.\n"
            "- Judge only the text and metadata you are given; never invent "
            "corroboration and never reject a post for its author, style, or "
            "tone.\n\n"
            "Return EXACTLY one JSON object and nothing else:\n"
            '{"status": "<relevant|irrelevant|uncertain|needs_media>", '
            '"confidence": <number 0.0-1.0 or null>, "reason": "<short explanation>"}\n\n'
            "Status meanings:\n"
            '- "relevant": a local story, event, or observation this edition '
            "should know about.\n"
            '- "irrelevant": clearly outside the edition\'s local scope (for '
            "example distant national or international news with no local "
            "angle).\n"
            '- "needs_media": the decisive evidence is inside attached photos '
            "or video that you cannot see, so text alone cannot settle it.\n"
            '- "uncertain": you honestly cannot decide from the given material.'
        )

    def _source_payload(self, revision: RevisionMaterial) -> str:
        text = (revision.text_content or "").strip() or _NO_TEXT_PLACEHOLDER
        return json.dumps(
            {
                "text": text,
                "metadata": revision.payload,
                "attachments": [
                    {"kind": asset.get("kind"), "metadata": asset.get("metadata")}
                    for asset in revision.assets
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )


def _parse_json_object(response: str) -> Any:
    """Best-effort strict JSON extraction from a model response."""
    text = response.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
