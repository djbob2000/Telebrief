"""Story matching processing (Plan 3 Task 7).

This module currently lands ONLY the policy identity half of the pipeline:
:meth:`StoryMatchingPolicyService.ensure_current` resolves-or-creates the
edition's current ``story_matching_policy_versions`` row. The matcher
orchestration (run/candidates -> LLM -> atomic apply with stale protection)
lands in a follow-up change and will pin the exact resolved policy id.

RULINGS implemented here:

* Identity is the FULL retrieval config tuple — embedding model, embedding
  dimensions, per-signal recall limits, total candidate cap, resolved-story
  lookback — digested into ``config_hash`` (plus the separate
  ``prompt_version``). Any config change therefore creates a NEW version
  instead of silently re-matching under different semantics.
* ``editions`` carries NO current-story-matching-policy pointer column, so
  "current" is purely identity-resolved with latest-version-wins semantics
  (same shape as claim extraction policies): callers pin the resolved id onto
  runs/deferrals they create. Concurrent creators are arbitrated by
  uq_story_matching_policy_edition_version via a savepoint re-read loop.

Repositories never commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

import hashlib
import json

import psycopg

from src.domain.stories import StoryMatchingPolicyVersion
from src.repositories.story_candidates import StoryMatchingPolicyVersionRepository

STORY_MATCHING_PROMPT_VERSION = "v1"

DEFAULT_VECTOR_LIMIT = 20
DEFAULT_LEXICAL_LIMIT = 10
DEFAULT_STATE_FALLBACK_LIMIT = 20
DEFAULT_TOTAL_CANDIDATE_LIMIT = 40
DEFAULT_RESOLVED_LOOKBACK_DAYS = 30

_ENSURE_CURRENT_RACE_ATTEMPTS = 3


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
    """Resolve-or-create the edition's current story matching policy version."""

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
