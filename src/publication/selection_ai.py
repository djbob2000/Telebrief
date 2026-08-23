"""AI Editorial selection model and fail-open composite selector."""

from __future__ import annotations

import json
import logging
import re

from src.ai_providers import AIProvider, create_provider
from src.config_loader import Config, load_config
from src.publication.models import PublicationCandidate, PublicationRun
from src.publication.selection import (
    HeuristicSelectionModel,
    SelectionModel,
    SelectionProposal,
)

logger = logging.getLogger(__name__)

VALID_DECISIONS = {"INCLUDE", "OMIT"}
VALID_PRESENTATION_INTENTS = {
    "lead",
    "normal",
    "brief",
    "unverified_operational",
    "follow_up",
}


class InvalidSelectionResponse(ValueError):
    """Raised when AI selector response violates schema or invariant requirements."""


class AIPublicationSelectionModel:
    """LLM-backed editorial selector evaluating frozen candidate stories."""

    def __init__(
        self,
        provider: AIProvider | None = None,
        config: Config | None = None,
        model_name: str | None = None,
    ) -> None:
        self.config = config or load_config()
        self.model_name = (
            model_name or self.config.settings.ai_model or self.config.settings.openai_model
        )
        if provider is not None:
            self.provider = provider
        else:
            self.provider = create_provider(
                self.config.settings.ai_provider,
                logger=logger,
                openai_api_key=self.config.openai_api_key,
                anthropic_api_key=self.config.anthropic_api_key,
                google_api_key=getattr(self.config, "gemini_api_key", "")
                or getattr(self.config, "google_api_key", ""),
                ollama_base_url=self.config.settings.ollama_base_url,
                api_timeout=self.config.settings.api_timeout,
            )

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        if not candidates:
            return []

        prompt = self._build_prompt(run=run, candidates=candidates)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an editorial selection AI for regional publication digests. "
                    "Evaluate candidate stories and decide whether to INCLUDE or OMIT them.\n\n"
                    "CRITICAL RULES:\n"
                    "1. Return exactly one decision for each candidate story in the candidate set.\n"
                    "2. Single-source, community, or unverified status is NEVER by itself a valid reason to OMIT. "
                    "Low-risk useful reports from single channels or community groups should be INCLUDED with "
                    "presentation_intent='unverified_operational', 'normal', or 'brief'.\n"
                    "3. Rank included stories starting at rank 1 for the most important lead item.\n"
                    "4. Respond strictly with a JSON object containing a 'proposals' array."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        raw_output = await self.provider.chat_completion(
            messages=messages,
            model=self.model_name,
            temperature=0.2,
            max_tokens=4096,
        )

        return self._parse_and_validate(raw_output, candidates)

    def _build_prompt(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> str:
        candidates_data = []
        for cand in candidates:
            candidates_data.append(
                {
                    "story_id": cand.story_id,
                    "story_revision_id": cand.story_revision_id,
                    "deterministic_rank": cand.deterministic_rank,
                    "snapshot_features": cand.snapshot_features,
                }
            )

        return (
            f"Publication Type: {run.publication_type}\n"
            f"Snapshot At: {run.snapshot_at.isoformat()}\n"
            f"Candidate Stories ({len(candidates)} items):\n"
            f"{json.dumps(candidates_data, ensure_ascii=False, indent=2)}\n\n"
            "Respond strictly with a JSON object with this schema:\n"
            "{\n"
            '  "proposals": [\n'
            "    {\n"
            '      "story_id": <int>,\n'
            '      "story_revision_id": <int>,\n'
            '      "decision": "INCLUDE" | "OMIT",\n'
            '      "presentation_intent": "lead" | "normal" | "brief" | "unverified_operational" | "follow_up" | null,\n'
            '      "rank": <int> | null,\n'
            '      "confidence": <float between 0.0 and 1.0>,\n'
            '      "reason": <string>\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )

    def _parse_and_validate(
        self, raw_output: str, candidates: list[PublicationCandidate]
    ) -> list[SelectionProposal]:
        text = raw_output.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()

        try:
            data = json.loads(text)
        except Exception as err:
            raise InvalidSelectionResponse(f"failed to parse selector JSON: {err}") from err

        if isinstance(data, dict):
            raw_list = data.get("proposals") or data.get("decisions") or data.get("candidates")
            if not isinstance(raw_list, list):
                raise InvalidSelectionResponse("JSON response missing 'proposals' list")
        elif isinstance(data, list):
            raw_list = data
        else:
            raise InvalidSelectionResponse(f"unexpected JSON root type: {type(data)}")

        expected_keys = {(c.story_id, c.story_revision_id) for c in candidates}
        seen_keys: set[tuple[int, int]] = set()
        proposals: list[SelectionProposal] = []

        for item in raw_list:
            if not isinstance(item, dict):
                raise InvalidSelectionResponse("each proposal must be a JSON object")

            story_id = item.get("story_id")
            story_rev_id = item.get("story_revision_id")
            if not isinstance(story_id, int) or not isinstance(story_rev_id, int):
                raise InvalidSelectionResponse("story_id and story_revision_id must be integers")

            key = (story_id, story_rev_id)
            if key not in expected_keys:
                raise InvalidSelectionResponse(f"unknown candidate story in proposals: {key}")
            if key in seen_keys:
                raise InvalidSelectionResponse(f"duplicate candidate in proposals: {key}")
            seen_keys.add(key)

            decision = item.get("decision")
            if decision not in VALID_DECISIONS:
                raise InvalidSelectionResponse(
                    f"invalid decision {decision!r}, expected one of {VALID_DECISIONS}"
                )

            intent = item.get("presentation_intent")
            if intent is not None and intent not in VALID_PRESENTATION_INTENTS:
                raise InvalidSelectionResponse(
                    f"invalid presentation_intent {intent!r}, expected one of {VALID_PRESENTATION_INTENTS}"
                )

            rank = item.get("rank")
            if rank is not None and not isinstance(rank, int):
                try:
                    rank = int(rank)
                except (ValueError, TypeError):
                    rank = None

            conf = item.get("confidence")
            confidence = float(conf) if conf is not None else None
            reason = str(item.get("reason", "")) or None

            proposals.append(
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=story_rev_id,
                    decision=decision,
                    presentation_intent=intent,
                    confidence=confidence,
                    reason=reason,
                    rank=rank,
                )
            )

        if seen_keys != expected_keys:
            missing = expected_keys - seen_keys
            raise InvalidSelectionResponse(f"selector omitted decisions for candidates: {missing}")

        return proposals


class FailOpenSelectionModel:
    """Fail-open editorial selector: uses primary AI model with fallback to heuristic selection."""

    def __init__(
        self,
        primary: SelectionModel | None = None,
        fallback: SelectionModel | None = None,
    ) -> None:
        self.primary = primary or AIPublicationSelectionModel()
        self.fallback = fallback or HeuristicSelectionModel()

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        if not candidates:
            return []
        try:
            proposals = await self.primary.select_stories(run=run, candidates=candidates)
            cand_keys = {(c.story_id, c.story_revision_id) for c in candidates}
            prop_keys = {(p.story_id, p.story_revision_id) for p in proposals}
            if len(proposals) != len(candidates) or prop_keys != cand_keys:
                raise InvalidSelectionResponse(
                    f"primary selector returned {len(proposals)} proposals for {len(candidates)} candidates"
                )
            return proposals
        except Exception as exc:
            logger.warning(
                "primary editorial selection failed (%s: %s); falling back to fail-open selector",
                type(exc).__name__,
                exc,
            )
            return await self.fallback.select_stories(run=run, candidates=candidates)
