"""Provider-neutral enrichment planner and dispatcher (Plan 5 Task 5)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import psycopg

from src.ingestion.models import JSONValue
from src.runtime import get_runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichmentRequest:
    """A provider-neutral request to enrich a source item / revision."""

    kind: str
    source_item_revision_id: int
    mode: str = "incremental"  # "incremental" | "deep"
    metadata: dict[str, JSONValue] = field(default_factory=dict)


class EnrichmentPlanner:
    """Plans enrichment jobs triggered by relevance verdicts or pre-publish runs."""

    def __init__(self) -> None:
        self._rules: list[Callable[[Any, Any], EnrichmentRequest | None]] = []
        self._pre_publish_planners: dict[
            str,
            Callable[
                [psycopg.AsyncConnection, int, int], Coroutine[Any, Any, list[EnrichmentRequest]]
            ],
        ] = {}

    def register_rule(self, rule: Callable[[Any, Any], EnrichmentRequest | None]) -> None:
        self._rules.append(rule)

    def register_pre_publish_planner(
        self,
        platform: str,
        planner: Callable[
            [psycopg.AsyncConnection, int, int], Coroutine[Any, Any, list[EnrichmentRequest]]
        ],
    ) -> None:
        self._pre_publish_planners[platform] = planner

    def requests_for(self, decision: Any, revision: Any) -> list[EnrichmentRequest]:
        """Evaluate registered rules against a decision + revision pair."""
        results: list[EnrichmentRequest] = []
        for rule in self._rules:
            try:
                req = rule(decision, revision)
                if req is not None:
                    results.append(req)
            except Exception as e:
                logger.warning("Error evaluating enrichment rule: %s", e)
        return results

    async def pre_publish_requests(
        self, conn: psycopg.AsyncConnection, source_id: int, platform: str, *, limit: int = 10
    ) -> list[EnrichmentRequest]:
        planner = self._pre_publish_planners.get(platform)
        if planner is None:
            return []
        return await planner(conn, source_id, limit)


class EnrichmentDispatcher:
    """Dispatches enrichment requests into Procrastinate tasks with locks."""

    async def defer(
        self,
        conn: psycopg.AsyncConnection,
        request: EnrichmentRequest,
        *,
        priority: int = 0,
    ) -> int | None:
        """Defer task on the current domain transaction connection."""
        from src.jobs.facebook import refresh_facebook_comments

        if request.kind == "facebook_comments":
            raw_post_id = request.metadata.get("post_item_id")
            post_item_id = int(raw_post_id) if isinstance(raw_post_id, (int, str)) else 0
            auth_profile = str(request.metadata.get("auth_profile", "default"))
            q_lock = f"facebook-comments:{post_item_id}:{request.mode}"
            exec_lock = f"facebook-auth-profile:{auth_profile}"

            job_id = await refresh_facebook_comments.configure(
                connection=conn,
                lock=exec_lock,
                queueing_lock=q_lock,
                priority=priority,
            ).defer_async(
                source_item_revision_id=request.source_item_revision_id,
                post_item_id=post_item_id,
                mode=request.mode,
            )
            return job_id

        logger.warning("Unknown enrichment request kind: %s", request.kind)
        return None

    async def defer_without_domain_transaction(
        self,
        request: EnrichmentRequest,
        *,
        priority: int = 0,
    ) -> int | None:
        """Defer task outside domain transaction (e.g. from maintenance sweep)."""
        runtime = get_runtime()
        async with runtime.uow.transaction() as conn:
            return await self.defer(conn, request, priority=priority)


def plan_facebook_comments(decision: Any, revision: Any) -> EnrichmentRequest | None:
    """Facebook rule: relevant posts schedule comments enrichment."""
    if getattr(decision, "status", None) != "relevant":
        return None

    platform = getattr(revision, "platform", None) or (
        revision.payload.get("platform")
        if isinstance(getattr(revision, "payload", None), dict)
        else None
    )
    kind = getattr(revision, "kind", None)
    if platform != "facebook" and kind != "facebook_post":
        return None

    post_item_id = getattr(revision, "source_item_id", None)
    payload = getattr(revision, "payload", {}) or {}
    auth_profile = (
        getattr(revision, "auth_profile", None)
        or (payload.get("auth_profile") if isinstance(payload, dict) else None)
        or (payload.get("auth_profile_id") if isinstance(payload, dict) else None)
        or "default"
    )
    return EnrichmentRequest(
        kind="facebook_comments",
        source_item_revision_id=revision.id,
        mode="incremental",
        metadata={"post_item_id": post_item_id, "auth_profile": auth_profile},
    )


async def plan_facebook_pre_publish(
    conn: psycopg.AsyncConnection, source_id: int, limit: int = 10
) -> list[EnrichmentRequest]:
    """Facebook pre-publish rule: deep comments scan for recent active posts."""
    from src.repositories.facebook import FacebookRepository
    from src.repositories.sources import SourceRepository

    fb_repo = FacebookRepository()
    src_repo = SourceRepository()
    source = await src_repo.get(conn, source_id)
    auth_profile = (
        (source.collector_options or {}).get("auth_profile")
        or (source.collector_options or {}).get("auth_profile_id")
        or "default"
        if source
        else "default"
    )
    posts = await fb_repo.list_recent_active_posts(conn, source_id=source_id, limit=limit)
    return [
        EnrichmentRequest(
            kind="facebook_comments",
            source_item_revision_id=p.current_revision_id,
            mode="deep",
            metadata={"post_item_id": p.source_item_id, "auth_profile": auth_profile},
        )
        for p in posts
    ]


_GLOBAL_ENRICHMENT_PLANNER = EnrichmentPlanner()
_GLOBAL_ENRICHMENT_PLANNER.register_rule(plan_facebook_comments)
_GLOBAL_ENRICHMENT_PLANNER.register_pre_publish_planner("facebook", plan_facebook_pre_publish)

_GLOBAL_ENRICHMENT_DISPATCHER = EnrichmentDispatcher()


def get_enrichment_planner() -> EnrichmentPlanner:
    return _GLOBAL_ENRICHMENT_PLANNER


def get_enrichment_dispatcher() -> EnrichmentDispatcher:
    return _GLOBAL_ENRICHMENT_DISPATCHER
