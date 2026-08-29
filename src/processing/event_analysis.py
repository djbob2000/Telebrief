"""Rich Event-level LLM analysis and verification service."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

from src.domain.event_pipeline import SourceFragment
from src.domain.stories import NewStoryRevision, StoryRevision
from src.processing.evidence_sampling import (
    FragmentWithContext,
    RepresentativeEvidenceSampler,
)
from src.repositories.embeddings import _vec_to_list
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "v1"

_EVENT_ANALYSIS_SYSTEM_PROMPT = """You are an expert investigative regional news editor.
Analyze the following chronological source fragments from multiple channels regarding a single local event.
Extract objective facts, distinguish official statements from community observations, highlight contradictions or uncertainties, and summarize the event.

Tags are descriptive metadata, not digest sections. Use whatever concise terms best describe the event (3-8 short topic tags in Russian; open vocabulary; do not choose from a predefined taxonomy). Never force an event into a predefined city category.

Respond ONLY with a valid JSON object with the exact keys:
{
  "topic": "Concise topic label (e.g. Авария на водоводе в микрорайоне АКЗ)",
  "tags": ["3-8 short topic tags in Russian; open vocabulary; do not choose from a predefined taxonomy"],
  "urgency": "critical | high | normal | low",
  "publishability": "news | brief | internal_only | noise",
  "headline": "Professional informative headline in Russian",
  "digest_summary": "1-3 concise sentences summarizing what happened, who is affected, and current status",
  "key_facts": ["List of confirmed facts"],
  "official_positions": [{"source": "Source name", "statement": "Summary of official position"}],
  "community_observations": ["Key citizen reports/observations"],
  "conflicts_or_uncertainties": ["Unclear, disputed, or contradictory details"],
  "affected_areas": ["List of streets, microdistricts, or city areas mentioned"],
  "timeline_summary": "Short timeline of developments",
  "confidence_score": 0.95
}
"""


def _normalize_open_tags(value: Any, legacy_category: Any = None) -> list[str]:
    raw = value if isinstance(value, list) else []
    if not raw and isinstance(legacy_category, str) and legacy_category.strip():
        raw = [legacy_category]

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item).strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag[:80])
        if len(result) == 12:
            break
    return result


@dataclass(frozen=True)
class EventAnalysisPayload:
    """Structured rich analysis output stored on story_revisions."""

    analysis_version: str
    topic: str
    urgency: str
    publishability: str
    headline: str
    digest_summary: str
    key_facts: list[str]
    official_positions: list[dict[str, str]]
    community_observations: list[str]
    conflicts_or_uncertainties: list[str]
    affected_areas: list[str]
    timeline_summary: str
    confidence_score: float
    representative_fragment_ids: list[int]
    tags: list[str] = field(default_factory=list)
    category: str = ""  # deprecated compatibility field

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventAnalysisPayload:
        legacy_cat = str(data.get("category", "")).strip()
        tags = _normalize_open_tags(data.get("tags"), legacy_category=legacy_cat)
        return cls(
            analysis_version=str(data.get("analysis_version", ANALYSIS_VERSION)),
            topic=str(data.get("topic", "")),
            category=legacy_cat,
            tags=tags,
            urgency=str(data.get("urgency", "normal")),
            publishability=str(data.get("publishability", "news")),
            headline=str(data.get("headline", "")),
            digest_summary=str(data.get("digest_summary", "")),
            key_facts=list(data.get("key_facts", [])),
            official_positions=list(data.get("official_positions", [])),
            community_observations=list(data.get("community_observations", [])),
            conflicts_or_uncertainties=list(data.get("conflicts_or_uncertainties", [])),
            affected_areas=list(data.get("affected_areas", [])),
            timeline_summary=str(data.get("timeline_summary", "")),
            confidence_score=float(data.get("confidence_score", 0.9)),
            representative_fragment_ids=[
                int(x) for x in data.get("representative_fragment_ids", [])
            ],
        )


class EventAnalysisService:
    """Coordinates rich event analysis for dirty stories using ProviderCascade."""

    def __init__(
        self,
        ai_cascade: Any,
        sampler: RepresentativeEvidenceSampler | None = None,
        cluster_repo: EventClusterRepository | None = None,
        story_repo: StoryRepository | None = None,
        fragment_repo: FragmentRepository | None = None,
        model: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self.ai = ai_cascade
        self.sampler = sampler or RepresentativeEvidenceSampler()
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.story_repo = story_repo or StoryRepository()
        self.fragment_repo = fragment_repo or FragmentRepository()
        self.model = model or "default"
        self.logger = logger_instance or logger

    async def analyze_story(
        self,
        conn: psycopg.AsyncConnection,
        story_id: int,
        *,
        max_representative_fragments: int = 16,
        max_input_chars: int = 24000,
    ) -> StoryRevision | None:
        """Run rich LLM event analysis for a story cluster and persist the new revision."""
        cluster_state = await self.cluster_repo.get_cluster_state(conn, story_id)
        if cluster_state is None:
            return None

        # 1. Fetch all fragments for this story with metadata and embeddings
        cursor = await conn.execute(
            """
            SELECT f.id, f.source_item_revision_id, f.ordinal, f.text_content,
                   f.normalized_hash, f.fragmenter_version, f.is_candidate, f.drop_reason, f.created_at,
                   fev.embedding, s.id, s.name, COALESCE(s.role, s.kind, 'unknown'),
                   COALESCE(si.first_collected_at, f.created_at)
            FROM story_fragments sf
            JOIN source_fragments f ON f.id = sf.fragment_id
            JOIN source_fragment_embeddings sfe ON sfe.fragment_id = f.id
            JOIN fragment_embedding_vectors fev ON fev.id = sfe.vector_id
            JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            WHERE sf.story_id = %s
            ORDER BY sf.id ASC
            """,
            (story_id,),
        )

        contexts: list[FragmentWithContext] = []
        async for row in cursor:
            frag = SourceFragment(
                id=int(row[0]),
                source_item_revision_id=int(row[1]),
                ordinal=int(row[2]),
                text_content=str(row[3]),
                normalized_hash=str(row[4]),
                fragmenter_version=str(row[5]),
                is_candidate=bool(row[6]),
                drop_reason=row[7],
                created_at=row[8],
            )
            vec = _vec_to_list(row[9])
            ctx = FragmentWithContext(
                fragment=frag,
                vector=vec,
                source_id=int(row[10]),
                source_name=str(row[11]),
                source_type=str(row[12]),
                timestamp=row[13],
            )
            contexts.append(ctx)

        if not contexts:
            return None

        # 2. Sample representative fragments
        sampled = self.sampler.sample_fragments(
            contexts,
            centroid=cluster_state.centroid,
            limit=max_representative_fragments,
        )
        if not sampled:
            return None

        # 3. Format prompt
        prompt_lines = ["Source fragments for analysis:"]
        for s in sampled:
            role_tag = "[OFFICIAL]" if s.is_official else f"[{s.source_type.upper()}]"
            time_str = s.timestamp.strftime("%Y-%m-%d %H:%M UTC")
            prompt_lines.append(
                f"- (ID {s.fragment_id}) {time_str} {role_tag} {s.source_name}: {s.text_content}"
            )

        user_prompt = "\n".join(prompt_lines)
        if len(user_prompt) > max_input_chars:
            user_prompt = user_prompt[:max_input_chars]

        prompt_hash = hashlib.sha256(
            f"{_EVENT_ANALYSIS_SYSTEM_PROMPT}\n{user_prompt}".encode("utf-8")
        ).hexdigest()

        # 4. Audit start
        provider_name = getattr(self.ai, "primary_provider_name", None) or getattr(
            self.ai, "provider_name", "ai_cascade"
        )
        model_name = getattr(self.ai, "model_name", None) or self.model or "default"

        cursor = await conn.execute(
            """
            INSERT INTO story_event_analysis_runs (
                story_id, latest_assignment_id, analysis_version, provider, model, prompt_hash,
                input_fragment_count, input_chars, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running')
            RETURNING id
            """,
            (
                story_id,
                cluster_state.latest_assignment_id,
                ANALYSIS_VERSION,
                str(provider_name),
                str(model_name),
                prompt_hash,
                len(sampled),
                len(user_prompt),
            ),
        )
        run_row = await cursor.fetchone()
        if run_row is None:
            raise RuntimeError("Failed to insert story_event_analysis_runs")
        run_id = int(run_row[0])

        # 5. Call LLM
        now = dt.datetime.now(dt.timezone.utc)
        try:
            if hasattr(self.ai, "generate_text"):
                raw_response = await self.ai.generate_text(
                    prompt=user_prompt,
                    system_prompt=_EVENT_ANALYSIS_SYSTEM_PROMPT,
                    temperature=0.2,
                    json_mode=True,
                )
            elif hasattr(self.ai, "chat_completion"):
                raw_response = await self.ai.chat_completion(
                    messages=[
                        {"role": "system", "content": _EVENT_ANALYSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=self.model,
                    temperature=0.2,
                    max_tokens=16384,
                    response_format={"type": "json_object"},
                )
            else:
                raise TypeError(f"Unsupported AI provider type: {type(self.ai)}")

            # Parse JSON
            cleaned_json = raw_response.strip()
            if cleaned_json.startswith("```"):
                lines = cleaned_json.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                cleaned_json = "\n".join(lines).strip()

            parsed = json.loads(cleaned_json)
            parsed["analysis_version"] = ANALYSIS_VERSION
            parsed["representative_fragment_ids"] = [s.fragment_id for s in sampled]
            payload = EventAnalysisPayload.from_dict(parsed)

            await conn.execute(
                """
                UPDATE story_event_analysis_runs
                SET status = 'succeeded', output_chars = %s, completed_at = now()
                WHERE id = %s
                """,
                (len(raw_response), run_id),
            )
        except Exception as exc:
            await conn.execute(
                """
                UPDATE story_event_analysis_runs
                SET status = 'failed', error_kind = %s, completed_at = now()
                WHERE id = %s
                """,
                (type(exc).__name__, run_id),
            )
            self.logger.warning("Event analysis failed for story %s: %s", story_id, exc)
            return None

        # 6. Create new story revision with event_payload
        content_hash = hashlib.sha256(
            json.dumps(payload.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()

        new_rev = NewStoryRevision(
            current_state="active",
            semantic_text=payload.digest_summary,
            content_hash=content_hash,
            created_at=now,
            title=payload.headline,
            summary=payload.digest_summary,
            reason=f"event_analysis_{ANALYSIS_VERSION}",
            event_payload=payload.to_dict(),
        )

        rev = await self.story_repo.create_revision_if_semantic_change(
            conn,
            story_id=story_id,
            semantic_changed=True,
            revision=new_rev,
        )

        # 7. Update cluster state as analyzed
        await self.cluster_repo.update_cluster_analysis_analyzed(
            conn,
            story_id=story_id,
            assignment_id=cluster_state.latest_assignment_id,
            analyzed_at=now,
        )

        return rev
