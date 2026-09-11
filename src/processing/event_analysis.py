"""Rich Event-level LLM analysis and verification service."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg

from src.ai_providers import classify_provider_failure
from src.domain.event_payload import (
    EventPayload,
    ensure_keep_publishability,
    normalize_question_evidence,
)
from src.domain.event_pipeline import SourceFragment
from src.domain.stories import NewStoryRevision, StoryRevision
from src.llm_telemetry import llm_call_context
from src.processing.evidence_sampling import (
    FragmentWithContext,
    RepresentativeEvidenceSampler,
)
from src.processing.operational_semantics import (
    has_unstructured_publish_service_access,
    normalize_service_state_evidence,
)
from src.repositories.embeddings import _vec_to_list
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "v6"

_EVENT_ANALYSIS_SYSTEM_PROMPT = """You are an expert investigative regional news editor.
Analyze the following chronological source fragments from multiple channels regarding a single local event.
Extract objective facts, distinguish official statements from community observations, highlight contradictions or uncertainties, and summarize the event.

A Story reaches this rich-analysis stage only after Event-First retention has kept it. Rich analysis may express uncertainty through evidence kinds, community_observations, conflicts_or_uncertainties, and confidence_score, but must not reverse KEEP merely because the report is single-source, community-sourced, conversational, or unverified.

Tags are descriptive metadata, not digest sections. Use whatever concise terms best describe the event (3-8 short topic tags in Russian; open vocabulary; do not choose from a predefined taxonomy). Never force an event into a predefined city category.

SERVICE-STATE CONTRACT:
- Operational service truth exists only inside a PUBLISH evidence item with kind=service_access and a non-null service_state object.
- Do not create a separate operational observations array.
- service_state describes the external resident-facing service outcome, not the resident workaround mechanism.
- A generator, battery, private well, neighbor collection, charging action, VPN choice, fuel burden, or other coping action is not itself a resident-facing service state.
- If a workaround causes an explicitly stated service outcome, keep the coping action as separate evidence and attach service_state only to the evidence sentence that states the water/internet/banking/transport/etc. outcome.
- For UNAVAILABLE, DEGRADED, or RESTRICTED, expected_now MUST be true and the excerpts must establish that the service is expected to operate now or explicitly describe a current failure/restriction.
- Do not infer expected_now from the calendar or general season knowledge.
- For SCHEDULED, basis must be scheduled_change and effective_from is required.
- Valid basis values: normal_operation, direct_failure, degraded_access, explicit_restriction, scheduled_change.
- Broad regional totals or incidents that do not state a concrete consequence inside the edition focus area must not be promoted into local key_facts, service_access, or digest_summary merely because they share the edition's region.

Respond ONLY with a valid JSON object with the exact keys:
{
  "topic": "Concise topic label (e.g. Авария на водоводе в микрорайоне АКЗ)",
  "tags": ["3-8 short topic tags in Russian; open vocabulary; do not choose from a predefined taxonomy"],
  "urgency": "critical | high | normal | low",
  "publishability": "news | brief",
  "headline": "Professional informative headline in Russian",
  "digest_summary": "1-3 concise sentences summarizing what happened, who is affected, and current status",
  "key_facts": ["List of confirmed facts"],
  "evidence_items": [
    {
      "text": "Fact or service access detail",
      "kind": "established_fact | community_report | service_access | official_statement | commercial_offer | resident_question",
      "publication_use": "PUBLISH | CONTEXT | EXCLUDE",
      "source_fragment_ids": [101],
      "service_state": {
        "subject_key": "water_supply",
        "subject_label": "Водоснабжение",
        "dimension": "availability",
        "state": "UNAVAILABLE | AVAILABLE | DEGRADED | RESTRICTED | UNKNOWN | SCHEDULED",
        "location": "АКЗ",
        "entity": "водовод",
        "expected_now": true,
        "basis": "direct_failure | normal_operation | degraded_access | explicit_restriction | scheduled_change",
        "effective_from": "2026-08-30T08:00:00+00:00",
        "effective_until": "2026-08-30T17:00:00+00:00"
      }
    }
  ],
  "official_positions": [{"source": "Source name", "statement": "Summary of official position"}],
  "community_observations": ["Key citizen reports/observations"],
  "conflicts_or_uncertainties": ["Unclear, disputed, or contradictory details"],
  "affected_areas": ["List of streets, microdistricts, or city areas mentioned"],
  "timeline_summary": "Short timeline of developments",
  "confidence_score": 0.95
}
"""

EventAnalysisPayload = EventPayload


@dataclass(frozen=True)
class EventAnalysisOutcome:
    """Structured outcome separating semantic no-op from provider failure."""

    succeeded: bool
    revision: StoryRevision | None
    error_kind: str | None
    prompt_hash: str


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
        uow: Any | None = None,
        max_output_tokens: int = 8_192,
        reasoning_effort: str | None = "low",
    ) -> None:
        self.ai = ai_cascade
        self.sampler = sampler or RepresentativeEvidenceSampler()
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.story_repo = story_repo or StoryRepository()
        self.fragment_repo = fragment_repo or FragmentRepository()
        self.model = model or "default"
        self.logger = logger_instance or logger
        self.uow = uow
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort

    async def analyze_story_outcome(
        self,
        conn: psycopg.AsyncConnection | None = None,
        story_id: int = 0,
        *,
        max_representative_fragments: int = 16,
        max_input_chars: int = 24000,
    ) -> EventAnalysisOutcome:
        """Run rich LLM event analysis for a story cluster and persist the new revision."""

        prompt_hash = ""

        @asynccontextmanager
        async def _get_conn():
            if conn is not None:
                yield conn
            elif self.uow is not None:
                async with self.uow.transaction() as c:
                    yield c
            else:
                raise ValueError("Either conn or uow must be provided for analyze_story")

        async with _get_conn() as read_conn:
            cluster_state = await self.cluster_repo.get_cluster_state(read_conn, story_id)
            if cluster_state is None:
                return EventAnalysisOutcome(
                    succeeded=False,
                    revision=None,
                    error_kind="story_not_found",
                    prompt_hash=prompt_hash,
                )

            # 1. Fetch all fragments for this story with metadata and embeddings
            cursor = await read_conn.execute(
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
                return EventAnalysisOutcome(
                    succeeded=False,
                    revision=None,
                    error_kind="no_evidence",
                    prompt_hash=prompt_hash,
                )

            # 3. Format prompt with geographic context
            cur_ed = await read_conn.execute(
                """
                SELECT e.slug, e.name
                FROM stories st
                JOIN editions e ON e.id = st.edition_id
                WHERE st.id = %s
                """,
                (story_id,),
            )
            row_ed = await cur_ed.fetchone()
            ed_slug = str(row_ed[0]) if row_ed else "unknown"
            ed_name = str(row_ed[1]) if row_ed and row_ed[1] else ed_slug.capitalize()

        # 2. Sample representative fragments (CPU)
        sampled = self.sampler.sample_fragments(
            contexts,
            centroid=cluster_state.centroid,
            limit=max_representative_fragments,
        )
        if not sampled:
            return EventAnalysisOutcome(
                succeeded=False,
                revision=None,
                error_kind="no_evidence",
                prompt_hash=prompt_hash,
            )

        from src.domain.edition_geography import resolve_edition_geography

        geo_context = resolve_edition_geography(ed_slug, ed_name)

        prompt_lines = [
            geo_context.to_prompt_section(),
            "",
            "Source fragments for analysis:",
        ]
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

        # 4. Prepare audit run info (written to DB after LLM call to avoid holding locks)
        provider_name = getattr(self.ai, "primary_provider_name", None) or getattr(
            self.ai, "provider_name", "ai_cascade"
        )
        model_name = getattr(self.ai, "model_name", None) or self.model or "default"

        # 5. Call LLM
        now = dt.datetime.now(dt.timezone.utc)
        try:
            with llm_call_context(stage="event_analysis", prompt_hash=prompt_hash, story_count=1):
                if hasattr(self.ai, "generate_text"):
                    raw_response = await self.ai.generate_text(
                        prompt=user_prompt,
                        system_prompt=_EVENT_ANALYSIS_SYSTEM_PROMPT,
                        temperature=0.2,
                        json_mode=True,
                        max_tokens=self.max_output_tokens,
                        reasoning_effort=self.reasoning_effort,
                    )
                elif hasattr(self.ai, "chat_completion"):
                    raw_response = await self.ai.chat_completion(
                        messages=[
                            {"role": "system", "content": _EVENT_ANALYSIS_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        model=self.model,
                        temperature=0.2,
                        max_tokens=self.max_output_tokens,
                        reasoning_effort=self.reasoning_effort,
                        response_format={"type": "json_object"},
                    )
                else:
                    raise TypeError(f"Unsupported AI provider type: {type(self.ai)}")

            # Parse JSON
            from src.utils import robust_extract_json

            parsed = robust_extract_json(raw_response)
            if not isinstance(parsed, dict):
                raise ValueError("event analysis response must be a JSON object")
            parsed["analysis_version"] = ANALYSIS_VERSION
            parsed["representative_fragment_ids"] = [s.fragment_id for s in sampled]
            parsed_payload = ensure_keep_publishability(
                normalize_question_evidence(EventAnalysisPayload.from_dict(parsed)),
                default="brief",
            )
            sampled_map = {s.fragment_id: s.text_content for s in sampled}
            payload, service_audit = normalize_service_state_evidence(parsed_payload, sampled_map)
            if service_audit.rejected_count > 0:
                self.logger.debug(
                    "Analysis rejected %s invalid service states for story %s: %s",
                    service_audit.rejected_count,
                    story_id,
                    service_audit.rejection_reasons,
                )
            if has_unstructured_publish_service_access(payload):
                self.logger.debug(
                    "Analysis story %s has unstructured publish service_access evidence",
                    story_id,
                )

            async with _get_conn() as write_conn:
                if not await self.cluster_repo.is_current_assignment(
                    write_conn,
                    story_id=story_id,
                    assignment_id=cluster_state.latest_assignment_id,
                ):
                    return EventAnalysisOutcome(
                        succeeded=False,
                        revision=None,
                        error_kind="superseded",
                        prompt_hash=prompt_hash,
                    )

                await write_conn.execute(
                    """
                    INSERT INTO story_event_analysis_runs (
                        story_id, latest_assignment_id, analysis_version, provider, model, prompt_hash,
                        input_fragment_count, input_chars, output_chars, status, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', now())
                    ON CONFLICT DO NOTHING
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
                        len(raw_response),
                    ),
                )

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
                    write_conn,
                    story_id=story_id,
                    semantic_changed=True,
                    revision=new_rev,
                    event_assignment_id=cluster_state.latest_assignment_id,
                )

                if rev is None:
                    return EventAnalysisOutcome(
                        succeeded=False,
                        revision=None,
                        error_kind="superseded",
                        prompt_hash=prompt_hash,
                    )

                # 7. Update cluster state as analyzed
                await self.cluster_repo.update_cluster_analysis_analyzed(
                    write_conn,
                    story_id=story_id,
                    assignment_id=cluster_state.latest_assignment_id,
                    analyzed_at=now,
                )

                return EventAnalysisOutcome(
                    succeeded=True,
                    revision=rev,
                    error_kind=None,
                    prompt_hash=prompt_hash,
                )
        except Exception as exc:
            try:
                async with _get_conn() as err_conn:
                    await err_conn.execute(
                        """
                        INSERT INTO story_event_analysis_runs (
                            story_id, latest_assignment_id, analysis_version, provider, model, prompt_hash,
                            input_fragment_count, input_chars, status, error_kind, completed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'failed', %s, now())
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
                            type(exc).__name__,
                        ),
                    )
            except Exception as log_exc:
                self.logger.warning("Failed to record failed analysis run: %s", log_exc)
            self.logger.warning("Event analysis failed for story %s: %s", story_id, exc)
            return EventAnalysisOutcome(
                succeeded=False,
                revision=None,
                error_kind=classify_provider_failure(exc),
                prompt_hash=prompt_hash,
            )

    async def analyze_story(
        self,
        conn: psycopg.AsyncConnection | None = None,
        story_id: int = 0,
        *,
        max_representative_fragments: int = 16,
        max_input_chars: int = 24000,
    ) -> StoryRevision | None:
        """Compatibility wrapper returning only the persisted revision."""
        outcome = await self.analyze_story_outcome(
            conn,
            story_id,
            max_representative_fragments=max_representative_fragments,
            max_input_chars=max_input_chars,
        )
        return outcome.revision
