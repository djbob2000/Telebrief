"""Rich Event-level LLM analysis and verification service."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

import psycopg

from src.domain.event_payload import (
    EventPayload,
    ensure_keep_publishability,
    normalize_question_evidence,
)
from src.domain.event_pipeline import SourceFragment
from src.domain.stories import NewStoryRevision, StoryRevision
from src.processing.evidence_sampling import (
    FragmentWithContext,
    RepresentativeEvidenceSampler,
)
from src.processing.operational_semantics import normalize_operational_payload
from src.repositories.embeddings import _vec_to_list
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "v5"

_EVENT_ANALYSIS_SYSTEM_PROMPT = """You are an expert investigative regional news editor.
Analyze the following chronological source fragments from multiple channels regarding a single local event.
Extract objective facts, distinguish official statements from community observations, highlight contradictions or uncertainties, and summarize the event.

A Story reaches this rich-analysis stage only after Event-First retention has kept it. Rich analysis may express uncertainty through evidence kinds, community_observations, conflicts_or_uncertainties, and confidence_score, but must not reverse KEEP merely because the report is single-source, community-sourced, conversational, or unverified.

Tags are descriptive metadata, not digest sections. Use whatever concise terms best describe the event (3-8 short topic tags in Russian; open vocabulary; do not choose from a predefined taxonomy). Never force an event into a predefined city category.

Publication use is semantic, not topic-based.
- Evidence kind describes semantic content, not source trust.
- Use service_access for a concrete current or scheduled resident-facing service availability/access state even when reported by a community source.
- Use community_report for useful community facts that are not themselves service availability/access states.
- Every PUBLISH service_access evidence item SHOULD have a matching operational_observation with overlapping source_fragment_ids.
- Do not label resident coping behavior, household tools, safety advice, personal burden, demand, sentiment, discussion, or future concern as service_access merely to create an operational observation.
- Absence of a seasonal or optional service is not a current outage unless the excerpts establish that operation is currently expected, was operating and failed, or explicitly report a current system failure/restriction.
- If a workaround creates a concrete service outcome, preserve the coping action separately and create a service state only for the explicitly supported outcome.
- A service-access fact may be PUBLISH even when a business or bank is named (e.g. ATM cash availability, backup power for telecom, state fee / document procedures).
- A sales offer, discount, product listing, seller phone number, or promotional price is EXCLUDE.
- Do not convert EXCLUDE commercial details into useful_details merely to preserve them.
- Use resident_question for a resident asking whether/where/when/how something works when the excerpt itself does not provide the answer.
- resident_question is CONTEXT, not PUBLISH.
- A question alone MUST NOT create an operational_observation or service state.
- If another fragment answers the question, represent the answer separately as service_access/community_report/official_statement as appropriate.
- Do not infer trends such as "повышенный спрос" or "участились вопросы" from one question.
- operational_observations are ONLY for a concrete current/scheduled state of resident-facing utility, infrastructure, transport, communications, financial/municipal service, or service-access function.
- Do NOT create an operational observation for resident coping behavior, safety advice, personal burden, demand/interest, discussion sentiment, or broad regional incident totals unless the excerpt explicitly establishes a concrete current local service state.
- Such facts may remain PUBLISH evidence and may remain in the digest thematic layer.


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
      "source_fragment_ids": [101]
    }
  ],
  "operational_observations": [
    {
      "subject_key": "power_supply",
      "subject_label": "Электроснабжение",
      "dimension": "availability",
      "location": "Центр",
      "entity": "электросеть",
      "state": "UNAVAILABLE | AVAILABLE | DEGRADED | RESTRICTED | UNKNOWN | SCHEDULED",
      "detail": "Аварийное отключение",
      "source_fragment_ids": [101],
      "effective_from": "2026-08-30T08:00:00+00:00",
      "effective_until": "2026-08-30T17:00:00+00:00"
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

        # 3. Format prompt with geographic context
        cur_ed = await conn.execute(
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
            parsed_payload = ensure_keep_publishability(
                normalize_question_evidence(EventAnalysisPayload.from_dict(parsed)),
                default="brief",
            )
            payload, operational_audit = normalize_operational_payload(parsed_payload)
            if operational_audit.dropped_observation_count > 0:
                self.logger.debug(
                    "Analysis dropped %s invalid operational observations for story %s: %s",
                    operational_audit.dropped_observation_count,
                    story_id,
                    operational_audit.dropped_observation_subject_keys,
                )
            if operational_audit.uncovered_service_access_fragment_ids:
                self.logger.debug(
                    "Analysis story %s has uncovered service_access fragments: %s",
                    story_id,
                    operational_audit.uncovered_service_access_fragment_ids,
                )

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
