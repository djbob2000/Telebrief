"""Batch triage service for low-support story clusters."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from src.config_loader import EditionScopeConfig
from src.domain.event_clusters import StoryClusterState
from src.processing.edition_scope import (
    SCOPE_VERSION,
    EditionScopeClass,
    build_scope_contract,
)
from src.repositories.event_clusters import EventClusterRepository

logger = logging.getLogger(__name__)

TRIAGE_VERSION = "v1"

_TRIAGE_SYSTEM_PROMPT = """You are a fast geographic and triage classifier for a regional newsroom digest.
You are evaluating candidate event Stories for ONE configured edition.
For every Story make TWO judgments in the same result:
1. geographic edition scope (scope, scope_confidence, scope_reason);
2. the triage/enrichment action (decision, exclusion_reason, confidence, reason).

Scope must be exactly LOCAL, DIRECT_IMPACT, OUT_OF_SCOPE, or UNCERTAIN.
Source membership alone does not make an event local.
An event in another city/region is OUT_OF_SCOPE unless the excerpts explicitly state a concrete consequence inside the configured focus area.
Same-region, national importance, front-line direction names, and broad strategic relevance are not DIRECT_IMPACT by themselves.

Decision must be:
- "ANALYZE": Genuine news/incident/event.
- "IGNORE": Commercial classified ad, services spam, greeting/chatter, or obvious noise.

Respond ONLY with a valid JSON object containing a "results" array:
{
  "results": [
    {
      "story_id": 123,
      "scope": "LOCAL | DIRECT_IMPACT | OUT_OF_SCOPE | UNCERTAIN",
      "scope_confidence": 0.98,
      "scope_reason": "Brief geographic explanation",
      "decision": "ANALYZE | IGNORE",
      "exclusion_reason": "commercial_classified | obvious_noise | null",
      "confidence": 0.98,
      "reason": "Brief explanation"
    }
  ]
}
"""


@dataclass(frozen=True)
class StoryTriageResult:
    story_id: int
    scope: EditionScopeClass
    scope_confidence: float
    scope_reason: str
    decision: str  # "ANALYZE" | "IGNORE"
    exclusion_reason: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class StoryTriageBatchResult:
    results: tuple[StoryTriageResult, ...]
    deferred_story_ids: tuple[int, ...]


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
        edition_id: int,
        scope_config: EditionScopeConfig,
        scope_hash: str,
        excerpt_chars: int = 320,
        min_ignore_confidence: float = 0.95,
    ) -> StoryTriageBatchResult:
        """Run batch triage and scope classification on story clusters.

        Returns StoryTriageBatchResult with valid decisions and deferred story IDs.
        """
        if not stories:
            return StoryTriageBatchResult(results=(), deferred_story_ids=())

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
        contract_text = build_scope_contract(scope_config)
        prompt_lines = [contract_text, "", "Stories for triage:"]
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

            payload = json.loads(cleaned_json)
            if not isinstance(payload, dict):
                raise ValueError("gate response must be a JSON object")
            raw_items = payload.get("results")
            if not isinstance(raw_items, list):
                raise ValueError("gate response missing results list")

            items_by_id: dict[int, dict[str, Any]] = {}
            for item in raw_items:
                if isinstance(item, dict) and isinstance(item.get("story_id"), int):
                    items_by_id[item["story_id"]] = item

            valid_results: list[StoryTriageResult] = []
            deferred_ids: list[int] = []

            for s in stories:
                item = items_by_id.get(s.story_id)
                if item is None or not isinstance(item, dict):
                    deferred_ids.append(s.story_id)
                    continue

                scope = str(item.get("scope", ""))
                if scope not in {"LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"}:
                    deferred_ids.append(s.story_id)
                    continue

                scope_conf = item.get("scope_confidence")
                if (
                    isinstance(scope_conf, bool)
                    or not isinstance(scope_conf, (int, float))
                    or not (0.0 <= float(scope_conf) <= 1.0)
                ):
                    deferred_ids.append(s.story_id)
                    continue
                scope_confidence = float(scope_conf)

                scope_reason = item.get("scope_reason")
                if not isinstance(scope_reason, str) or not scope_reason.strip():
                    deferred_ids.append(s.story_id)
                    continue

                dec = str(item.get("decision", "ANALYZE")).upper()
                if dec not in {"ANALYZE", "IGNORE"}:
                    deferred_ids.append(s.story_id)
                    continue

                ex_reason = item.get("exclusion_reason")
                if ex_reason not in ("commercial_classified", "obvious_noise"):
                    ex_reason = None

                conf = item.get("confidence")
                if (
                    isinstance(conf, bool)
                    or not isinstance(conf, (int, float))
                    or not (0.0 <= float(conf) <= 1.0)
                ):
                    deferred_ids.append(s.story_id)
                    continue
                confidence = float(conf)

                reason = str(item.get("reason", ""))

                triage_res = StoryTriageResult(
                    story_id=s.story_id,
                    scope=scope,  # type: ignore[arg-type]
                    scope_confidence=scope_confidence,
                    scope_reason=scope_reason.strip(),
                    decision=dec,
                    exclusion_reason=ex_reason,
                    confidence=confidence,
                    reason=reason,
                )
                valid_results.append(triage_res)

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
            self.logger.warning("Story triage batch failed: %s; deferring stories", exc)
            return StoryTriageBatchResult(
                results=(),
                deferred_story_ids=tuple(s.story_id for s in stories),
            )

        # 4. Process valid decisions & persist scope and triage records
        s_map = {s.story_id: s for s in stories}
        for res in valid_results:
            st = s_map[res.story_id]
            # Persist scope decision
            await conn.execute(
                """
                INSERT INTO story_edition_scope_decisions (
                    triage_run_id,
                    story_id,
                    edition_id,
                    latest_assignment_id,
                    scope_version,
                    scope_config_hash,
                    scope_class,
                    confidence,
                    reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    story_id,
                    latest_assignment_id,
                    scope_version,
                    scope_config_hash
                ) DO NOTHING
                """,
                (
                    run_id,
                    res.story_id,
                    edition_id,
                    st.latest_assignment_id,
                    SCOPE_VERSION,
                    scope_hash,
                    res.scope,
                    res.scope_confidence,
                    res.scope_reason,
                ),
            )

            # Persist triage decision
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
                    res.story_id,
                    st.latest_assignment_id,
                    TRIAGE_VERSION,
                    res.decision,
                    res.exclusion_reason,
                    res.confidence,
                    res.reason,
                ),
            )

        return StoryTriageBatchResult(
            results=tuple(valid_results),
            deferred_story_ids=tuple(deferred_ids),
        )
