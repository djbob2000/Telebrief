"""AI Editorial selection model and fail-open composite selector."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.ai_providers import AIProvider, create_provider
from src.config_loader import Config, load_config
from src.processing.relevance import ProviderUnavailableError
from src.publication.digest_contracts import (
    DIGEST_PUBLICATION_TYPES,
    HARD_EXCLUSION_REASONS,
)
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


class _NullAIProvider(AIProvider):
    """Fallback provider that always raises ProviderUnavailableError."""

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 1500,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        raise ProviderUnavailableError("No valid AI provider configured")

    async def chat_completion_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 1500,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
    ):
        raise ProviderUnavailableError("No valid AI provider configured")
        yield ""


class AIPublicationSelectionModel:
    """LLM-backed editorial selector evaluating frozen candidate stories."""

    def __init__(
        self,
        provider: AIProvider | None = None,
        config: Config | None = None,
        model_name: str | None = None,
        scope_contract: str | None = None,
    ) -> None:
        self.config = config or load_config()
        self.scope_contract = scope_contract
        self.model_name = (
            model_name or self.config.settings.ai_model or self.config.settings.openai_model
        )

        if provider is not None:
            self.provider = provider
        else:
            try:
                self.provider = create_provider(
                    self.config.settings.ai_provider,
                    logger=logger,
                    openai_api_key=self.config.openai_api_key,
                    openai_base_url=getattr(self.config.settings, "openai_base_url", ""),
                    anthropic_api_key=self.config.anthropic_api_key,
                    google_api_key=getattr(self.config, "gemini_api_key", "")
                    or getattr(self.config, "google_api_key", ""),
                    google_api_keys=getattr(self.config, "google_api_keys", None),
                    openrouter_api_key=getattr(self.config, "openrouter_api_key", ""),
                    openrouter_base_url=getattr(
                        self.config.settings, "openrouter_base_url", "https://openrouter.ai/api/v1"
                    ),
                    openrouter_model=getattr(
                        self.config.settings, "openrouter_model", "openrouter/free"
                    ),
                    ollama_base_url=self.config.settings.ollama_base_url,
                    api_timeout=self.config.settings.api_timeout,
                    reasoning_effort=getattr(self.config.settings, "reasoning_effort", None),
                )
            except Exception as exc:
                logger.warning(
                    "Could not initialize AI provider %s: %s",
                    self.config.settings.ai_provider,
                    exc,
                )
                self.provider = _NullAIProvider()

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        if not candidates:
            return []

        prompt = self._build_prompt(run=run, candidates=candidates)
        is_digest = run.publication_type in DIGEST_PUBLICATION_TYPES
        if is_digest:
            system_content = (
                "You are an editorial selection AI for regional publication digests. "
                "Every supplied candidate has already passed structural/temporal publication eligibility.\n\n"
                "For digest publications, preserve broad coverage of legitimate local civic news. "
                "Do not omit a legitimate story because it is lower priority, single-source, "
                "brief, operational, repetitive in theme, or less dramatic than the lead.\n\n"
                "The ONLY hard exclusion available in this selector is: commercial_classified.\n"
                "CRITICAL COMMERCIAL EXCLUSION RULES:\n"
                "- A story consisting ONLY of commercial offers, sales, private listings, prices, opening hours, "
                "contact details, or delivery terms is commercial_classified and MUST be OMIT with exclusion_reason='commercial_classified', "
                "regardless of what commodity or service is being sold.\n"
                "- The fact that the advertised product or service is water, medicine, transport, communications, fuel, "
                "food, housing, or another civic-relevant commodity does NOT by itself make the advertisement a civic story.\n"
                "- A commercial claim may support an INCLUDE story ONLY when the same candidate story ALSO contains independent "
                "non-commercial evidence of a broader public condition, such as an outage, shortage, disruption, documented citywide "
                "price change, municipal action, or public access problem.\n\n"
                "CRITICAL RULES:\n"
                "1. Return exactly one proposal for each candidate story in the candidate set.\n"
                "2. For INCLUDE, exclusion_reason must be null.\n"
                "3. If candidate is commercial-only, set decision='OMIT' and exclusion_reason='commercial_classified'.\n"
                "4. For subjective civic prioritization, keep decision='INCLUDE' and use rank / presentation_intent instead of OMIT.\n"
                "5. Rank included stories starting at rank 1 for the most important lead item.\n"
                "6. Respond strictly with a JSON object containing a 'proposals' array."
            )
        else:
            system_content = (
                "You are an editorial selection AI for regional publication articles. "
                "Evaluate candidate stories and decide whether to INCLUDE or OMIT them.\n\n"
                "CRITICAL RULES:\n"
                "1. Return exactly one decision for each candidate story in the candidate set.\n"
                "2. Single-source, community, or unverified status is NEVER by itself a valid reason to OMIT. "
                "Low-risk useful reports from single channels or community groups should be INCLUDED with "
                "presentation_intent='unverified_operational', 'normal', or 'brief'.\n"
                "3. Rank included stories starting at rank 1 for the most important lead item.\n"
                "4. Respond strictly with a JSON object containing a 'proposals' array."
            )

        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {"role": "user", "content": prompt},
        ]

        raw_output = await self.provider.chat_completion(
            messages=messages,
            model=self.model_name,
            temperature=0.2,
            max_tokens=65536,
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
            feat = cand.snapshot_features or {}
            summary = feat.get("semantic_text") or feat.get("summary") or ""
            candidates_data.append(
                {
                    "story_id": cand.story_id,
                    "story_revision_id": cand.story_revision_id,
                    "summary": summary if summary else "No summary",
                }
            )

        prompt_parts = []
        if self.scope_contract:
            prompt_parts.append(f"{self.scope_contract}\n")
        prompt_parts.append(
            f"Publication Type: {run.publication_type}\n"
            f"Snapshot At: {run.snapshot_at.isoformat()}\n"
            f"Candidate Stories ({len(candidates)} items):\n"
            f"{json.dumps(candidates_data, ensure_ascii=False, indent=2)}\n\n"
            "Select the most meaningful and relevant stories to INCLUDE in the publication.\n"
            "Respond strictly with a compact JSON object containing the 'included' list:\n"
            "{\n"
            '  "included": [\n'
            "    {\n"
            '      "story_id": <int>,\n'
            '      "presentation_intent": "lead" | "normal" | "brief" | "unverified_operational",\n'
            '      "rank": <int>\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "All candidate stories not listed in 'included' will be automatically omitted.\n"
        )
        return "\n".join(prompt_parts)

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

        expected_keys = {(c.story_id, c.story_revision_id) for c in candidates}
        story_to_rev = {c.story_id: c.story_revision_id for c in candidates}
        seen_keys: set[tuple[int, int]] = set()
        proposals: list[SelectionProposal] = []

        is_compact_included = False
        if isinstance(data, dict):
            if "included" in data or "selected" in data:
                raw_list = data.get("included") or data.get("selected") or []
                is_compact_included = True
            else:
                raw_list = data.get("proposals") or data.get("decisions") or data.get("candidates")
                if not isinstance(raw_list, list):
                    raise InvalidSelectionResponse(
                        "JSON response missing 'proposals' or 'included' list"
                    )
        elif isinstance(data, list):
            raw_list = data
        else:
            raise InvalidSelectionResponse(f"unexpected JSON root type: {type(data)}")

        if not isinstance(raw_list, list):
            raise InvalidSelectionResponse("proposals/included must be a JSON array")

        for item in raw_list:
            if not isinstance(item, dict):
                raise InvalidSelectionResponse("each proposal must be a JSON object")

            story_id = item.get("story_id") or item.get("id")
            story_rev_id = item.get("story_revision_id") or item.get("rev")
            if isinstance(story_id, str) and story_id.isdigit():
                story_id = int(story_id)
            if isinstance(story_rev_id, str) and story_rev_id.isdigit():
                story_rev_id = int(story_rev_id)

            if isinstance(story_id, int) and not isinstance(story_rev_id, int):
                story_rev_id = story_to_rev.get(story_id)

            if not isinstance(story_id, int) or not isinstance(story_rev_id, int):
                continue

            key = (story_id, story_rev_id)
            if key not in expected_keys:
                if is_compact_included:
                    continue
                raise InvalidSelectionResponse(f"unknown candidate story in proposals: {key}")

            if key in seen_keys:
                continue
            seen_keys.add(key)

            decision = item.get("decision")
            if is_compact_included and decision is None:
                decision = "INCLUDE"

            if decision not in VALID_DECISIONS:
                raise InvalidSelectionResponse(
                    f"invalid decision {decision!r}, expected one of {VALID_DECISIONS}"
                )

            intent = item.get("presentation_intent") or item.get("intent")
            if intent is not None and intent not in VALID_PRESENTATION_INTENTS:
                raise InvalidSelectionResponse(
                    f"invalid presentation_intent {intent!r}, expected one of {VALID_PRESENTATION_INTENTS}"
                )

            ex_reason = item.get("exclusion_reason")
            if ex_reason is not None:
                if not isinstance(ex_reason, str) or ex_reason not in HARD_EXCLUSION_REASONS:
                    raise InvalidSelectionResponse(
                        f"invalid exclusion_reason {ex_reason!r}, expected null or one of {HARD_EXCLUSION_REASONS}"
                    )
                if decision == "INCLUDE":
                    raise InvalidSelectionResponse(
                        "exclusion_reason must be null when decision is INCLUDE"
                    )
            exclusion_reason = str(ex_reason) if ex_reason is not None else None

            rank = item.get("rank")
            if rank is not None and (not isinstance(rank, int) or rank < 1):
                raise InvalidSelectionResponse("rank must be a positive integer or None")

            try:
                confidence = float(item.get("confidence", 1.0))
            except (ValueError, TypeError):
                confidence = 1.0

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
                    exclusion_reason=exclusion_reason,
                )
            )

        if is_compact_included:
            # For any candidate not in included list, create default OMIT proposal
            for cand in candidates:
                cand_key = (cand.story_id, cand.story_revision_id)
                if cand_key not in seen_keys:
                    proposals.append(
                        SelectionProposal(
                            story_id=cand.story_id,
                            story_revision_id=cand.story_revision_id,
                            decision="OMIT",
                            presentation_intent=None,
                            confidence=1.0,
                            reason="Not selected by editorial model",
                            rank=None,
                            exclusion_reason=None,
                        )
                    )
        else:
            if seen_keys != expected_keys:
                missing = expected_keys - seen_keys
                raise InvalidSelectionResponse(
                    f"selector omitted decisions for candidates: {missing}"
                )

        return proposals


class FailOpenSelectionModel:
    """Fail-open editorial selector: uses primary AI model with fallback to heuristic selection."""

    def __init__(
        self,
        primary: SelectionModel | None = None,
        fallback: SelectionModel | None = None,
        *,
        config: Config | None = None,
        scope_contract: str | None = None,
    ) -> None:
        self.primary = primary or AIPublicationSelectionModel(
            config=config, scope_contract=scope_contract
        )
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

            # Publication-first invariant:
            # editorial selection must not turn lack of corroboration,
            # community provenance, or unverified status into an
            # effective publication gate.
            # If candidates were sealed (> 0) but the primary selector omitted all of them,
            # treat as a degraded/over-conservative selection and fall back to permissive heuristic.
            included_count = sum(1 for p in proposals if p.decision == "INCLUDE")
            if candidates and included_count == 0:
                is_digest = run.publication_type in DIGEST_PUBLICATION_TYPES
                if is_digest and all(
                    p.decision == "OMIT" and p.exclusion_reason in HARD_EXCLUSION_REASONS
                    for p in proposals
                ):
                    # Valid all-commercial exclusion in digest: do not trigger heuristic fallback
                    return proposals
                raise InvalidSelectionResponse(
                    f"primary selector returned zero INCLUDE decisions for {len(candidates)} sealed candidates; triggering fail-open fallback"
                )

            return proposals
        except Exception as exc:
            logger.warning(
                "primary editorial selection failed (%s: %s); falling back to fail-open selector",
                type(exc).__name__,
                exc,
            )
            return await self.fallback.select_stories(run=run, candidates=candidates)
