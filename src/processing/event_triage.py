"""Batch Gate V2 triage and brief generation service for story clusters."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from src.config_loader import EditionScopeConfig
from src.domain.event_clusters import StoryClusterState
from src.domain.event_payload import (
    EventPayload,
    ensure_keep_publishability,
    normalize_question_evidence,
    parse_event_payload,
)
from src.processing.edition_scope import (
    SCOPE_VERSION,
    EditionScopeClass,
    build_scope_contract,
)
from src.repositories.event_clusters import EventClusterRepository

logger = logging.getLogger(__name__)

TRIAGE_VERSION = "v4"

_GATE_V2_SYSTEM_PROMPT = """You are a fast geographic, editorial retention, and operational triage classifier for a regional newsroom digest.
You are evaluating candidate event Stories for ONE configured edition.
For every Story return three judgments in the same result:
1. geographic scope: exactly LOCAL, DIRECT_IMPACT, OUT_OF_SCOPE, or UNCERTAIN;
2. retention: KEEP or DROP;
3. enrichment: NONE, BRIEF, or ANALYZE.

Scope is evaluated first. Source membership alone does not make an event local.
An event in another city/region is OUT_OF_SCOPE unless the excerpts explicitly state a concrete consequence inside the configured focus area.
Same-region, national importance, front-line direction names, and broad strategic relevance are not DIRECT_IMPACT by themselves.
OUT_OF_SCOPE or UNCERTAIN is normalized to DROP+NONE without requiring a brief.

For LOCAL or DIRECT_IMPACT content:
- Lack of corroboration, a single community source, or lack of official confirmation is NOT by itself a reason to DROP an otherwise legitimate LOCAL or DIRECT_IMPACT report.
- Represent source uncertainty through evidence kind, wording, and confidence; do not erase the event.
- For retention=KEEP, brief_payload.publishability must be "news" or "brief". Do not use "internal_only" or "noise" merely because evidence is community, conversational, single-source, or unverified.
- DROP is only for high-confidence hard noise/commercial-only content and must use enrichment=NONE with exclusion_reason in ('commercial_classified', 'obvious_noise').
- In-scope KEEP uses BRIEF for simple useful local information, and ANALYZE only when rich synthesis is justified.
- Publication use is semantic, not topic-based.
- A service-access fact may be PUBLISH even when a business or bank is named (e.g. ATM cash availability, backup power for telecom, state fee / document procedures).
- A sales offer, discount, product listing, seller phone number, or promotional price is EXCLUDE.
- Do not convert EXCLUDE commercial details into useful_details merely to preserve them.
- Resident questions, resident answers, service availability, outage reports, and operational workarounds are not noise merely because they are conversational. Preserve current local actionable information about everyday civilian access to services.
- Use resident_question for a resident asking whether/where/when/how something works when the excerpt itself does not provide the answer.
- resident_question is CONTEXT, not PUBLISH.
- A question alone MUST NOT create an operational_observation or service state.
- If another fragment answers the question, represent the answer separately as service_access/community_report/official_statement as appropriate.
- Do not infer trends such as "повышенный спрос" or "участились вопросы" from one question.
- For KEEP, provide a brief_payload with topic, tags, urgency, publishability, headline, digest_summary, operational_observations, and evidence_items.
- Every operational observation MUST cite one or more exact source_fragment_ids from the excerpts for that Story. Valid states: AVAILABLE, UNAVAILABLE, DEGRADED, RESTRICTED, UNKNOWN, SCHEDULED. Limit to at most 4 operational observations. Include effective_from / effective_until in ISO-8601 when the source reports a future or scheduled window.
- Every evidence item MUST have text, kind (established_fact, community_report, service_access, official_statement, commercial_offer, resident_question), publication_use (PUBLISH, CONTEXT, EXCLUDE), and exact source_fragment_ids.

Respond ONLY with a valid JSON object containing a "results" array:
{
  "results": [
    {
      "story_id": 123,
      "scope": "LOCAL | DIRECT_IMPACT | OUT_OF_SCOPE | UNCERTAIN",
      "scope_confidence": 0.98,
      "scope_reason": "Brief geographic explanation",
      "retention": "KEEP | DROP",
      "enrichment": "NONE | BRIEF | ANALYZE",
      "exclusion_reason": "commercial_classified | obvious_noise | null",
      "confidence": 0.98,
      "reason": "Brief explanation",
      "brief_payload": {
        "enrichment_level": "brief",
        "topic": "Concise topic label",
        "tags": ["3-8 short open tags in Russian"],
        "urgency": "critical | high | normal | low",
        "publishability": "news | brief | internal_only | noise",
        "headline": "Informative headline",
        "digest_summary": "1-2 concise sentences",
        "operational_observations": [
          {
            "subject_key": "power_supply",
            "subject_label": "Электроснабжение",
            "dimension": "availability",
            "location": "Центр",
            "entity": "электросеть",
            "state": "UNAVAILABLE",
            "detail": "Аварийное отключение",
            "source_fragment_ids": [101],
            "effective_from": "2026-08-30T08:00:00+00:00",
            "effective_until": "2026-08-30T17:00:00+00:00"
          }
        ],
        "evidence_items": [
          {
            "text": "Fact or service access detail",
            "kind": "established_fact | community_report | service_access | official_statement | commercial_offer | resident_question",
            "publication_use": "PUBLISH | CONTEXT | EXCLUDE",
            "source_fragment_ids": [101]
          }
        ]
      }
    }
  ]
}
"""


@dataclass(frozen=True)
class StoryGateResult:
    story_id: int
    scope: EditionScopeClass
    scope_confidence: float
    scope_reason: str
    retention: Literal["KEEP", "DROP"]
    enrichment: Literal["NONE", "BRIEF", "ANALYZE"]
    exclusion_reason: str | None
    confidence: float
    reason: str
    brief_payload: EventPayload | None

    @property
    def decision(self) -> str:
        """Compatibility projection for legacy audit code."""
        return "ANALYZE" if self.retention == "KEEP" else "IGNORE"


@dataclass(frozen=True)
class StoryGateBatchResult:
    results: tuple[StoryGateResult, ...]
    deferred_story_ids: tuple[int, ...]


# Backward compatibility aliases
StoryTriageResult = StoryGateResult
StoryTriageBatchResult = StoryGateBatchResult


class StoryTriageService:
    """Classifies candidate story clusters via Gate V2 (scope, retention, enrichment, brief)."""

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
        max_gate_fragments: int = 6,
    ) -> StoryGateBatchResult:
        """Run batch Gate V2 classification on story clusters."""
        if not stories:
            return StoryGateBatchResult(results=(), deferred_story_ids=())

        # 1. Lookup cached Gate V2 results
        cached_results = await self._lookup_cached_decisions(conn, stories, scope_hash)
        uncached_stories = [s for s in stories if s.story_id not in cached_results]

        valid_results: list[StoryGateResult] = [
            cached_results[s.story_id] for s in stories if s.story_id in cached_results
        ]
        deferred_ids: list[int] = []

        if not uncached_stories:
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=(),
            )

        # 2. Fetch fragment metadata for uncached stories
        story_ids = [s.story_id for s in uncached_stories]
        cursor = await conn.execute(
            """
            SELECT sf.story_id, f.id, f.text_content, s.id, s.name,
                   COALESCE(s.role, s.kind, 'unknown'),
                   COALESCE(si.published_at, si.first_collected_at, f.created_at)
            FROM story_fragments sf
            JOIN source_fragments f ON f.id = sf.fragment_id
            JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            WHERE sf.story_id = ANY(%s)
            ORDER BY sf.story_id, f.id DESC
            """,
            (story_ids,),
        )

        story_fragments_map: dict[int, list[dict[str, Any]]] = {sid: [] for sid in story_ids}
        all_story_frag_ids: dict[int, set[int]] = {sid: set() for sid in story_ids}
        async for row in cursor:
            sid = int(row[0])
            fid = int(row[1])
            text = str(row[2])[:excerpt_chars]
            source_id = int(row[3])
            source_name = str(row[4])
            source_role = str(row[5])
            obs_time = row[6]
            story_fragments_map[sid].append(
                {
                    "fragment_id": fid,
                    "text": text,
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_role": source_role,
                    "observed_at": obs_time,
                }
            )
            all_story_frag_ids[sid].add(fid)

        # Select at most max_gate_fragments per story deterministically:
        # first newest fragment from each distinct source, then newest unused
        story_sampled_excerpts: dict[int, list[str]] = {}
        for sid, frags in story_fragments_map.items():
            selected_frags: list[dict[str, Any]] = []
            seen_sources: set[int] = set()
            # 1. Newest from distinct sources
            for frag in frags:
                src_id = frag["source_id"]
                if src_id not in seen_sources:
                    seen_sources.add(src_id)
                    selected_frags.append(frag)
                if len(selected_frags) >= max_gate_fragments:
                    break
            # 2. Fill remaining slots
            if len(selected_frags) < max_gate_fragments:
                selected_ids = {f["fragment_id"] for f in selected_frags}
                for frag in frags:
                    if frag["fragment_id"] not in selected_ids:
                        selected_frags.append(frag)
                    if len(selected_frags) >= max_gate_fragments:
                        break

            # Sort selected by fragment_id for stable reading
            selected_frags.sort(key=lambda x: x["fragment_id"])
            excerpt_lines: list[str] = []
            for sf in selected_frags:
                iso_time = (
                    sf["observed_at"].isoformat()
                    if hasattr(sf["observed_at"], "isoformat")
                    else str(sf["observed_at"])
                )
                excerpt_lines.append(
                    f"- [frag={sf['fragment_id']} time={iso_time} role={sf['source_role']} source={sf['source_name']}] {sf['text']}"
                )
            story_sampled_excerpts[sid] = excerpt_lines

        # 3. Load dynamic recent subject hints
        recent_hints = await self._load_recent_subject_hints(conn, edition_id)
        hint_text = ""
        if recent_hints:
            hint_lines = [f"- {k}: {lbl}" for k, lbl in recent_hints]
            hint_text = (
                "Recent open subject keys for consistency (reuse a key only when semantically exact; create a new key when none fits):\n"
                + "\n".join(hint_lines)
                + "\n\n"
            )

        # 4. Build prompt with geographic context
        cur_slug = await conn.execute("SELECT slug FROM editions WHERE id = %s", (edition_id,))
        row_slug = await cur_slug.fetchone()
        edition_slug = str(row_slug[0]) if row_slug else "unknown"

        from src.domain.edition_geography import resolve_edition_geography

        geo_context = resolve_edition_geography(edition_slug, scope_config.name)
        contract_text = build_scope_contract(scope_config, geo_context)
        prompt_lines = [
            contract_text,
            "",
            hint_text,
            "Stories for Gate V2 triage and brief synthesis:",
        ]

        for s in uncached_stories:
            excerpts = story_sampled_excerpts.get(s.story_id, [])
            excerpts_str = "\n".join(excerpts) if excerpts else "(No text)"
            prompt_lines.append(
                f"Story #{s.story_id} (fragments={s.fragment_count}, sources={s.unique_source_count}):\n{excerpts_str}\n"
            )

        user_prompt = "\n".join(prompt_lines)
        prompt_hash = hashlib.sha256(
            f"{_GATE_V2_SYSTEM_PROMPT}\n{user_prompt}".encode("utf-8")
        ).hexdigest()

        # 5. Record batch audit run start
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
                len(uncached_stories),
                len(user_prompt),
            ),
        )
        run_row = await cursor.fetchone()
        if run_row is None:
            raise RuntimeError("Failed to insert story_event_triage_runs")
        run_id = int(run_row[0])

        # 6. Call LLM
        try:
            if hasattr(self.ai, "generate_text"):
                raw_response = await self.ai.generate_text(
                    prompt=user_prompt,
                    system_prompt=_GATE_V2_SYSTEM_PROMPT,
                    temperature=0.0,
                    json_mode=True,
                )
            elif hasattr(self.ai, "chat_completion"):
                raw_response = await self.ai.chat_completion(
                    messages=[
                        {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
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

            new_valid_results: list[StoryGateResult] = []

            for s in uncached_stories:
                item = items_by_id.get(s.story_id)
                if item is None or not isinstance(item, dict):
                    deferred_ids.append(s.story_id)
                    continue

                scope_raw = str(item.get("scope", "")).strip()
                if scope_raw not in {"LOCAL", "DIRECT_IMPACT", "OUT_OF_SCOPE", "UNCERTAIN"}:
                    deferred_ids.append(s.story_id)
                    continue
                scope: EditionScopeClass = scope_raw  # type: ignore[assignment]

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

                conf = item.get("confidence")
                if (
                    isinstance(conf, bool)
                    or not isinstance(conf, (int, float))
                    or not (0.0 <= float(conf) <= 1.0)
                ):
                    deferred_ids.append(s.story_id)
                    continue
                confidence = float(conf)

                reason = str(item.get("reason", "")).strip()
                retention_raw = str(item.get("retention", "")).strip().upper()
                enrichment_raw = str(item.get("enrichment", "")).strip().upper()
                ex_reason_raw = item.get("exclusion_reason")
                ex_reason = (
                    str(ex_reason_raw).strip()
                    if ex_reason_raw in ("commercial_classified", "obvious_noise")
                    else None
                )

                allowed_fids = all_story_frag_ids.get(s.story_id, set())

                # Parse brief_payload if present
                raw_brief = item.get("brief_payload")
                brief_payload: EventPayload | None = None
                if isinstance(raw_brief, dict):
                    try:
                        brief_payload = normalize_question_evidence(
                            parse_event_payload(raw_brief, allowed_fragment_ids=allowed_fids)
                        )
                    except Exception as e:
                        self.logger.debug(
                            "Brief payload parsing error for story %s: %s", s.story_id, e
                        )
                        brief_payload = None

                # Normalization rules
                if scope in {"OUT_OF_SCOPE", "UNCERTAIN"}:
                    retention: Literal["KEEP", "DROP"] = "DROP"
                    enrichment: Literal["NONE", "BRIEF", "ANALYZE"] = "NONE"
                    ex_reason = None
                    brief_payload = None
                elif scope in {"LOCAL", "DIRECT_IMPACT"}:
                    if retention_raw == "DROP":
                        # High-confidence hard exclusions only
                        if (
                            enrichment_raw == "NONE"
                            and ex_reason in ("commercial_classified", "obvious_noise")
                            and confidence >= min_ignore_confidence
                        ):
                            retention = "DROP"
                            enrichment = "NONE"
                            brief_payload = None
                        elif brief_payload is not None:
                            # Unsafe drop normalized to KEEP+BRIEF
                            retention = "KEEP"
                            enrichment = "BRIEF"
                            ex_reason = None
                            brief_payload = ensure_keep_publishability(
                                normalize_question_evidence(brief_payload), default="brief"
                            )
                        else:
                            # Unsafe drop without a valid brief must defer
                            deferred_ids.append(s.story_id)
                            continue
                    elif retention_raw == "KEEP":
                        if enrichment_raw not in ("BRIEF", "ANALYZE") or brief_payload is None:
                            deferred_ids.append(s.story_id)
                            continue
                        retention = "KEEP"
                        enrichment = enrichment_raw  # type: ignore[assignment]
                        ex_reason = None
                        brief_payload = ensure_keep_publishability(
                            normalize_question_evidence(brief_payload), default="brief"
                        )
                    else:
                        deferred_ids.append(s.story_id)
                        continue
                else:
                    deferred_ids.append(s.story_id)
                    continue

                gate_res = StoryGateResult(
                    story_id=s.story_id,
                    scope=scope,
                    scope_confidence=scope_confidence,
                    scope_reason=scope_reason.strip(),
                    retention=retention,
                    enrichment=enrichment,
                    exclusion_reason=ex_reason,
                    confidence=confidence,
                    reason=reason,
                    brief_payload=brief_payload,
                )
                new_valid_results.append(gate_res)

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
            return StoryGateBatchResult(
                results=tuple(valid_results),
                deferred_story_ids=tuple(s.story_id for s in uncached_stories),
            )

        # 7. Persist decisions
        s_map = {s.story_id: s for s in uncached_stories}
        for res in new_valid_results:
            st = s_map[res.story_id]
            # Scope decision
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

            # Gate V2 decision
            brief_json = json.dumps(res.brief_payload.to_dict()) if res.brief_payload else None
            await conn.execute(
                """
                INSERT INTO story_event_triage_decisions (
                    run_id, story_id, latest_assignment_id, triage_version,
                    scope_config_hash, decision, retention, enrichment,
                    exclusion_reason, confidence, reason, brief_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (story_id, latest_assignment_id, triage_version, scope_config_hash)
                DO UPDATE SET
                    decision = EXCLUDED.decision,
                    retention = EXCLUDED.retention,
                    enrichment = EXCLUDED.enrichment,
                    exclusion_reason = EXCLUDED.exclusion_reason,
                    confidence = EXCLUDED.confidence,
                    reason = EXCLUDED.reason,
                    brief_payload = EXCLUDED.brief_payload
                """,
                (
                    run_id,
                    res.story_id,
                    st.latest_assignment_id,
                    TRIAGE_VERSION,
                    scope_hash,
                    res.decision,
                    res.retention,
                    res.enrichment,
                    res.exclusion_reason,
                    res.confidence,
                    res.reason,
                    brief_json,
                ),
            )

        all_results_by_id = {r.story_id: r for r in valid_results + new_valid_results}
        final_results = [
            all_results_by_id[s.story_id] for s in stories if s.story_id in all_results_by_id
        ]

        return StoryGateBatchResult(
            results=tuple(final_results),
            deferred_story_ids=tuple(deferred_ids),
        )

    async def _lookup_cached_decisions(
        self,
        conn: psycopg.AsyncConnection,
        stories: list[StoryClusterState],
        scope_hash: str,
    ) -> dict[int, StoryGateResult]:
        if not stories:
            return {}

        story_ids = [s.story_id for s in stories]
        assignment_ids = [s.latest_assignment_id for s in stories]

        cursor = await conn.execute(
            """
            SELECT setd.story_id, sesd.scope_class, sesd.confidence, sesd.reason,
                   setd.retention, setd.enrichment, setd.exclusion_reason,
                   setd.confidence, setd.reason, setd.brief_payload
            FROM story_event_triage_decisions setd
            JOIN story_edition_scope_decisions sesd
              ON sesd.story_id = setd.story_id
             AND sesd.latest_assignment_id = setd.latest_assignment_id
             AND sesd.scope_config_hash = setd.scope_config_hash
            WHERE setd.triage_version = %s
              AND setd.scope_config_hash = %s
              AND setd.story_id = ANY(%s)
              AND setd.latest_assignment_id = ANY(%s)
            """,
            (
                TRIAGE_VERSION,
                scope_hash,
                story_ids,
                assignment_ids,
            ),
        )

        cached: dict[int, StoryGateResult] = {}
        async for row in cursor:
            sid = int(row[0])
            scope_cls = str(row[1])
            scope_conf = float(row[2])
            scope_reason = str(row[3])
            retention = str(row[4])
            enrichment = str(row[5])
            ex_reason = row[6]
            conf = float(row[7])
            reason = str(row[8])
            raw_brief = row[9]
            brief_payload = parse_event_payload(raw_brief) if raw_brief else None

            cached[sid] = StoryGateResult(
                story_id=sid,
                scope=scope_cls,  # type: ignore[arg-type]
                scope_confidence=scope_conf,
                scope_reason=scope_reason,
                retention=retention,  # type: ignore[arg-type]
                enrichment=enrichment,  # type: ignore[arg-type]
                exclusion_reason=ex_reason,
                confidence=conf,
                reason=reason,
                brief_payload=brief_payload,
            )

        return cached

    async def _load_recent_subject_hints(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int,
        limit: int = 30,
    ) -> list[tuple[str, str]]:
        cursor = await conn.execute(
            """
            SELECT DISTINCT
                obs->>'subject_key' AS subject_key,
                obs->>'subject_label' AS subject_label
            FROM story_event_triage_decisions setd
            JOIN story_edition_scope_decisions sesd
              ON sesd.story_id = setd.story_id
             AND sesd.latest_assignment_id = setd.latest_assignment_id
             AND sesd.scope_config_hash = setd.scope_config_hash
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(setd.brief_payload->'operational_observations', '[]'::jsonb)) AS obs
            WHERE sesd.edition_id = %s
              AND sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')
              AND setd.retention = 'KEEP'
              AND setd.triage_version = %s
              AND obs->>'subject_key' IS NOT NULL
            ORDER BY subject_key
            LIMIT %s
            """,
            (edition_id, TRIAGE_VERSION, limit),
        )
        hints: list[tuple[str, str]] = []
        async for row in cursor:
            k, label = str(row[0] or "").strip(), str(row[1] or "").strip()
            if k and label:
                hints.append((k, label))
        return hints
