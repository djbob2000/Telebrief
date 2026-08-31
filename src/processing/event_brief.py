"""Service for persisting Gate V2 BRIEF payloads as StoryRevisions without an extra LLM call."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

import psycopg

from src.domain.event_payload import (
    EventPayload,
    OperationalObservationPayload,
    parse_event_payload,
)
from src.domain.stories import NewStoryRevision, StoryRevision
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)


def merge_brief_into_existing_payload(
    existing: EventPayload,
    brief: EventPayload,
) -> EventPayload:
    """Merge a Gate V2 brief into an existing rich event payload without downgrading."""
    # Stable union of tags
    seen_tags: set[str] = {t.casefold() for t in existing.tags}
    merged_tags = list(existing.tags)
    for tag in brief.tags:
        if tag.casefold() not in seen_tags:
            seen_tags.add(tag.casefold())
            merged_tags.append(tag)

    # Merge operational observations
    seen_obs_keys: set[tuple[str, str, str, str, str]] = {
        (
            obs.subject_key.casefold(),
            obs.dimension.casefold(),
            obs.location.casefold(),
            obs.entity.casefold(),
            obs.state.upper(),
        )
        for obs in existing.operational_observations
    }

    merged_observations: list[OperationalObservationPayload] = list(
        existing.operational_observations
    )
    for obs in brief.operational_observations:
        key = (
            obs.subject_key.casefold(),
            obs.dimension.casefold(),
            obs.location.casefold(),
            obs.entity.casefold(),
            obs.state.upper(),
        )
        if key not in seen_obs_keys:
            seen_obs_keys.add(key)
            merged_observations.append(obs)

    # Merge evidence items (stable union by text + kind)
    seen_evidence: set[tuple[str, str]] = {
        (e.text.strip(), e.kind) for e in existing.evidence_items
    }
    merged_evidence = list(existing.evidence_items)
    for e in brief.evidence_items:
        if (e.text.strip(), e.kind) not in seen_evidence:
            seen_evidence.add((e.text.strip(), e.kind))
            merged_evidence.append(e)

    return EventPayload(
        topic=existing.topic or brief.topic,
        tags=tuple(merged_tags),
        urgency=existing.urgency or brief.urgency,
        publishability=existing.publishability or brief.publishability,
        headline=existing.headline or brief.headline,
        digest_summary=existing.digest_summary or brief.digest_summary,
        evidence_items=tuple(merged_evidence),
        operational_observations=tuple(merged_observations),
        enrichment_level="analysis",  # never downgrade
        key_facts=existing.key_facts,
        official_positions=existing.official_positions,
        community_observations=existing.community_observations,
        conflicts_or_uncertainties=existing.conflicts_or_uncertainties,
        affected_areas=existing.affected_areas,
        timeline_summary=existing.timeline_summary,
        confidence_score=existing.confidence_score or brief.confidence_score,
        representative_fragment_ids=existing.representative_fragment_ids,
        analysis_version=existing.analysis_version,
        category=existing.category,
    )


class EventBriefService:
    """Persists Gate V2 BRIEF payloads as first-class StoryRevisions."""

    def __init__(
        self,
        story_repo: StoryRepository | None = None,
        cluster_repo: EventClusterRepository | None = None,
    ) -> None:
        self.story_repo = story_repo or StoryRepository()
        self.cluster_repo = cluster_repo or EventClusterRepository()

    async def persist_brief(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        assignment_id: int,
        payload: EventPayload | None,
    ) -> StoryRevision | None:
        """Create or update a StoryRevision using the already-computed brief payload."""
        if payload is None:
            return None

        # Fetch latest revision if any to check if already rich
        cursor = await conn.execute(
            """
            SELECT id, story_id, revision_no, current_state, semantic_text,
                   content_hash, event_payload, created_at, title, summary, reason
            FROM story_revisions
            WHERE story_id = %s
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        existing_rev: StoryRevision | None = None
        if row is not None:
            existing_rev = StoryRevision(
                id=int(row[0]),
                story_id=int(row[1]),
                revision_no=int(row[2]),
                current_state=str(row[3]),
                semantic_text=str(row[4]),
                content_hash=str(row[5]),
                event_payload=row[6] if isinstance(row[6], dict) else {},
                created_at=row[7],
                title=str(row[8] or ""),
                summary=str(row[9] or ""),
                reason=str(row[10] or ""),
            )

        now = dt.datetime.now(dt.timezone.utc)

        final_payload: EventPayload
        reason: str
        if (
            existing_rev is not None
            and existing_rev.event_payload
            and existing_rev.event_payload.get("enrichment_level") == "analysis"
        ):
            existing_payload = parse_event_payload(existing_rev.event_payload)
            final_payload = merge_brief_into_existing_payload(existing_payload, payload)
            reason = "event_gate_v2_brief_merge"
        else:
            final_payload = payload
            reason = "event_gate_v2_brief"

        payload_dict = final_payload.to_dict()
        content_hash = hashlib.sha256(
            json.dumps(payload_dict, sort_keys=True).encode("utf-8")
        ).hexdigest()

        if existing_rev is not None and existing_rev.content_hash == content_hash:
            return None

        new_rev = NewStoryRevision(
            current_state="active",
            semantic_text=final_payload.digest_summary or final_payload.headline,
            content_hash=content_hash,
            created_at=now,
            title=final_payload.headline,
            summary=final_payload.digest_summary,
            reason=reason,
            event_payload=payload_dict,
        )

        rev = await self.story_repo.create_revision_if_semantic_change(
            conn,
            story_id=story_id,
            semantic_changed=True,
            revision=new_rev,
        )
        return rev
