"""Batch triage service for low-support story clusters."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from src.domain.event_clusters import StoryClusterState
from src.repositories.event_clusters import EventClusterRepository

logger = logging.getLogger(__name__)

TRIAGE_VERSION = "v1"

_TRIAGE_SYSTEM_PROMPT = """You are a fast triage classifier for a regional newsroom digest.
Review the following candidate stories (each with 1-2 source excerpts).
Classify each story as either:
- "ANALYZE": Likely genuine local news, municipal incident, announcement, utility outage, or community event.
- "IGNORE": Commercial classified ad, services spam, greeting/chatter, or obvious noise.

Respond ONLY with a valid JSON array of objects:
[
  {
    "story_id": 123,
    "decision": "ANALYZE | IGNORE",
    "exclusion_reason": "commercial_classified | obvious_noise | null",
    "confidence": 0.98,
    "reason": "Brief explanation"
  }
]
"""


@dataclass(frozen=True)
class StoryTriageResult:
    story_id: int
    decision: str  # "ANALYZE" | "IGNORE"
    exclusion_reason: str | None
    confidence: float
    reason: str


class StoryTriageService:
    """Classifies low-support story clusters in cost-efficient batches."""

    def __init__(
        self,
        ai_cascade: Any,
        cluster_repo: EventClusterRepository | None = None,
        model: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self.ai = ai_cascade
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.model = model or "default"
        self.logger = logger_instance or logger

    async def triage_stories_batch(
        self,
        conn: psycopg.AsyncConnection,
        stories: list[StoryClusterState],
        *,
        excerpt_chars: int = 320,
        min_ignore_confidence: float = 0.95,
    ) -> list[int]:
        """Run batch triage on low-support stories.

        Returns list of story_ids that require full rich event analysis.
        """
        if not stories:
            return []

        # 1. Fetch excerpts for each story
        story_ids = [s.story_id for s in stories]
        cursor = await conn.execute(
            """
            SELECT sf.story_id, f.text_content, s.name, s.role
            FROM story_fragments sf
            JOIN source_fragments f ON f.id = sf.fragment_id
            JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            WHERE sf.story_id = ANY(%s)
            ORDER BY sf.story_id, sf.id ASC
            """,
            (story_ids,),
        )

        story_excerpts: dict[int, list[str]] = {sid: [] for sid in story_ids}
        async for row in cursor:
            sid = int(row[0])
            text = str(row[1])[:excerpt_chars]
            src = str(row[2])
            story_excerpts[sid].append(f"[{src}]: {text}")

        # Build prompt
        prompt_lines = ["Stories for triage:"]
        for s in stories:
            excerpts = story_excerpts.get(s.story_id, [])
            excerpts_str = " | ".join(excerpts) if excerpts else "(No text)"
            prompt_lines.append(
                f"Story #{s.story_id} (fragments={s.fragment_count}): {excerpts_str}"
            )

        user_prompt = "\n".join(prompt_lines)
        prompt_hash = hashlib.sha256(
            f"{_TRIAGE_SYSTEM_PROMPT}\n{user_prompt}".encode("utf-8")
        ).hexdigest()

        # 2. Record batch audit run start
        provider_name = getattr(self.ai, "primary_provider_name", None) or getattr(
            self.ai, "provider_name", "ai_cascade"
        )
        model_name = getattr(self.ai, "model_name", None) or self.model or "default"

        cursor = await conn.execute(
            """
            INSERT INTO story_event_triage_runs (
                triage_version, provider, model, prompt_hash, story_count, input_chars, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running')
            RETURNING id
            """,
            (
                TRIAGE_VERSION,
                str(provider_name),
                str(model_name),
                prompt_hash,
                len(stories),
                len(user_prompt),
            ),
        )
        run_row = await cursor.fetchone()
        if run_row is None:
            raise RuntimeError("Failed to insert story_event_triage_runs")
        run_id = int(run_row[0])

        # 3. Call LLM
        to_analyze: list[int] = []
        try:
            if hasattr(self.ai, "generate_text"):
                raw_response = await self.ai.generate_text(
                    prompt=user_prompt,
                    system_prompt=_TRIAGE_SYSTEM_PROMPT,
                    temperature=0.0,
                    json_mode=True,
                )
            elif hasattr(self.ai, "chat_completion"):
                raw_response = await self.ai.chat_completion(
                    messages=[
                        {"role": "system", "content": _TRIAGE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=self.model,
                    temperature=0.0,
                    max_tokens=16384,
                    response_format={"type": "json_object"},
                )
            else:
                raise TypeError(f"Unsupported AI provider type: {type(self.ai)}")

            cleaned_json = raw_response.strip()
            if cleaned_json.startswith("```"):
                lines = cleaned_json.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                cleaned_json = "\n".join(lines).strip()

            items = json.loads(cleaned_json)
            if not isinstance(items, list):
                items = [items]

            decisions_by_id: dict[int, StoryTriageResult] = {}
            for item in items:
                sid = int(item.get("story_id", 0))
                dec = str(item.get("decision", "ANALYZE")).upper()
                ex_reason = item.get("exclusion_reason")
                if ex_reason not in ("commercial_classified", "obvious_noise"):
                    ex_reason = None
                conf = float(item.get("confidence", 0.5))
                reason = str(item.get("reason", ""))
                decisions_by_id[sid] = StoryTriageResult(
                    story_id=sid,
                    decision=dec,
                    exclusion_reason=ex_reason,
                    confidence=conf,
                    reason=reason,
                )

            await conn.execute(
                """
                UPDATE story_event_triage_runs
                SET status = 'succeeded', output_chars = %s, completed_at = now()
                WHERE id = %s
                """,
                (len(raw_response), run_id),
            )
        except Exception as exc:
            await conn.execute(
                """
                UPDATE story_event_triage_runs
                SET status = 'failed', error_kind = %s, completed_at = now()
                WHERE id = %s
                """,
                (type(exc).__name__, run_id),
            )
            self.logger.warning("Story triage batch failed: %s; falling back to analyze all", exc)
            return [s.story_id for s in stories]

        # 4. Process decisions & update cluster states
        now = dt.datetime.now(dt.timezone.utc)
        for s in stories:
            res = decisions_by_id.get(s.story_id)
            if res is None:
                to_analyze.append(s.story_id)
                continue

            await conn.execute(
                """
                INSERT INTO story_event_triage_decisions (
                    run_id, story_id, latest_assignment_id, triage_version,
                    decision, exclusion_reason, confidence, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (story_id, latest_assignment_id, triage_version)
                DO UPDATE SET
                    decision = EXCLUDED.decision,
                    exclusion_reason = EXCLUDED.exclusion_reason,
                    confidence = EXCLUDED.confidence,
                    reason = EXCLUDED.reason
                """,
                (
                    run_id,
                    s.story_id,
                    s.latest_assignment_id,
                    TRIAGE_VERSION,
                    res.decision,
                    res.exclusion_reason,
                    res.confidence,
                    res.reason,
                ),
            )

            is_safe_ignore = (
                res.decision == "IGNORE"
                and res.confidence >= min_ignore_confidence
                and res.exclusion_reason in ("commercial_classified", "obvious_noise")
            )
            if is_safe_ignore:
                # Mark cluster state settled without rich analysis
                await self.cluster_repo.update_cluster_analysis_analyzed(
                    conn,
                    story_id=s.story_id,
                    assignment_id=s.latest_assignment_id,
                    analyzed_at=now,
                )
            else:
                to_analyze.append(s.story_id)

        return to_analyze
