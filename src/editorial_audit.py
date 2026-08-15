"""Non-blocking light fact checks and targeted repairs for article drafts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.ai_providers import AIProvider, is_token_budget_error
from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.editorial_writer import ArticleDraft, AuditUnitLocator


@dataclass
class AuditIssue:
    unit_id: str
    code: str
    original_excerpt: str
    reason: str
    suggested_direction: str
    source_refs: list[str]
    severity: str = "fix"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditIssue":
        severity = str(data.get("severity", "fix")).lower()
        if severity not in {"fix", "warn"}:
            severity = "fix"
        return cls(
            unit_id=str(data.get("unit_id", "")),
            code=str(data.get("code", "unspecified")),
            original_excerpt=str(data.get("original_excerpt", "")),
            reason=str(data.get("reason", "")),
            suggested_direction=str(data.get("suggested_direction", "")),
            source_refs=list(data.get("source_refs", [])),
            severity=severity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "code": self.code,
            "original_excerpt": self.original_excerpt,
            "reason": self.reason,
            "suggested_direction": self.suggested_direction,
            "source_refs": self.source_refs,
            "severity": self.severity,
        }


@dataclass
class FactCheckResult:
    status: str
    systemic_problem: bool
    issues: list[AuditIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = self._normalize_status(self.status, self.issues)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FactCheckResult":
        if not isinstance(data, dict):
            raise ValueError("fact-check result must be an object")
        issues = [AuditIssue.from_dict(item) for item in data.get("issues", [])]
        return cls(
            status=str(data.get("status", "PASS")),
            systemic_problem=bool(data.get("systemic_problem", False)),
            issues=issues,
        )

    @staticmethod
    def _normalize_status(status: str, issues: list[AuditIssue]) -> str:
        if any(issue.severity == "fix" for issue in issues) or status.upper() == "FIX":
            return "FIX"
        if issues or status.upper() == "WARN":
            return "WARN"
        return "PASS"


class FactCheckUnavailableError(RuntimeError):
    """The optional light fact-check provider was unavailable."""


class LightFactChecker:
    """Ask a model to find concrete unsupported additions and repair them locally."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        logger: logging.Logger,
        max_output_tokens: int = 65_536,
        repair_max_output_tokens: int | None = None,
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if repair_max_output_tokens is not None and repair_max_output_tokens <= 0:
            raise ValueError("repair_max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.logger = logger
        self.max_output_tokens = max_output_tokens
        self.repair_max_output_tokens = repair_max_output_tokens or max_output_tokens
        self.last_raw_response: str | None = None
        self.last_stage: str | None = None
        self.last_reason: str | None = None
        self.last_response_chars: int | None = None

    def _build_system_prompt(self) -> str:
        return (
            "You are a light newsroom fact checker. Inspect the whole draft against the Story "
            "Cards and original source records. Return JSON only: status PASS, WARN or FIX, "
            "systemic_problem boolean, and issues. Find only new concrete independently "
            "verifiable facts without support: numbers, prices, dates, names, official actions, "
            "causes, mechanisms, damage, sales, medical/legal/military claims, casualties and "
            "precise scale. "
            "Scale claims such as 'most districts', 'across most of the city', 'massively', 'citywide shortage' "
            "require evidence supporting the claimed denominator or sufficiently broad geographic coverage; "
            "multiple observations establish geographic spread, but do not automatically establish a majority (flag unsupported majority claims as FIX). "
            "Absence from supplied reporting material does not prove absence in the outside world (corpus boundary): "
            "when evidence is corpus absence, the text must state 'в доступных сообщениях...' rather than absolute 'официального графика нет' "
            "unless an authorized source explicitly establishes that fact (flag unsupported absolute absence claims as FIX). "
            "Legitimate collective synthesis of resident observations and discussions is not a FIX merely because no single "
            "message literally contains the whole synthesized sentence. Use PASS when well supported and WARN for soft "
            "overstatement or debatable framing; reserve FIX for unsupported verifiable facts, "
            "lost attribution, false causality or high-risk escalation. Emotional or mood assertions "
            "require direct source evidence. Attribution and source_refs are inspection aids, "
            "not proof by themselves. WARN is non-blocking; use FIX only when a local fragment "
            "must be changed."
        )

    def _parse_payload(self, response: str) -> dict[str, Any]:
        if not response or not response.strip():
            self.last_stage = "empty_response"
            self.last_reason = "empty response from provider"
            raise FactCheckUnavailableError("empty response from provider")

        self.last_stage = "json_parse"
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
            cleaned = cleaned.removesuffix("```").strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            self.last_reason = f"JSON decode error: {exc}"
            raise FactCheckUnavailableError(f"invalid JSON response: {exc}") from exc

        if not isinstance(payload, dict):
            self.last_stage = "response_shape"
            self.last_reason = "fact check response is not a dict"
            raise FactCheckUnavailableError("fact check response is not a dict")
        return payload

    @staticmethod
    def _compact_story_cards(
        analysis: EditorialAnalysis,
        *,
        minimal: bool = False,
    ) -> list[dict[str, Any]]:
        compact_cards: list[dict[str, Any]] = []
        for card in analysis.cards:
            base: dict[str, Any] = {
                "id": card.id,
                "topic": card.topic,
                "summary": card.summary,
                "source_refs": sorted(card.all_source_refs()),
            }
            if minimal:
                compact_cards.append(base)
                continue
            base["hard_facts"] = [
                {
                    "text": elem.text,
                    "status": elem.status,
                    "attribution": elem.attribution,
                    "source_refs": list(elem.source_refs),
                }
                for elem in card.hard_facts
            ]
            base["community_observations"] = [
                {
                    "text": elem.text,
                    "status": elem.status,
                    "attribution": elem.attribution,
                    "source_refs": list(elem.source_refs),
                }
                for elem in card.community_observations
            ]
            base["useful_details"] = [
                {
                    "text": elem.text,
                    "status": elem.status,
                    "attribution": elem.attribution,
                    "source_refs": list(elem.source_refs),
                }
                for elem in card.useful_details
            ]
            base["uncertainties"] = [
                {
                    "text": unc.text,
                    "basis": unc.basis,
                    "related_source_refs": list(unc.related_source_refs),
                }
                for unc in card.uncertainties
            ]
            compact_cards.append(base)
        return compact_cards

    async def _run_check_request(
        self,
        payload: dict[str, Any],
        units: dict[str, AuditUnitLocator],
        *,
        compact_retry: bool = False,
    ) -> FactCheckResult:
        system = self._build_system_prompt()
        if compact_retry:
            system += "\n\nCOMPACT RETRY: Return only a minimal JSON object with status and substantive issues."
        user = json.dumps(payload, ensure_ascii=False)
        self.last_stage = "provider_call"
        response = await self.provider.chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=self.model,
            temperature=0.1,
            max_tokens=self.max_output_tokens,
            response_format={"type": "json_object"},
        )
        self.last_raw_response = response
        self.last_response_chars = len(response) if response is not None else 0
        parsed = self._parse_payload(response)
        self.last_stage = "result_parse"
        try:
            result = FactCheckResult.from_dict(parsed)
        except Exception as exc:
            self.last_reason = f"failed to parse FactCheckResult: {exc}"
            raise FactCheckUnavailableError(f"failed to parse FactCheckResult: {exc}") from exc

        unknown_units = sorted(set(issue.unit_id for issue in result.issues) - set(units))
        if unknown_units:
            self.last_reason = (
                f"fact-check returned unknown audit units: {', '.join(unknown_units)}"
            )
            raise ValueError(f"fact-check returned unknown audit units: {', '.join(unknown_units)}")
        self.last_stage = None
        self.last_reason = None
        return result

    async def check(
        self,
        draft: ArticleDraft,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        audit_units: dict[str, AuditUnitLocator] | None = None,
    ) -> FactCheckResult:
        self.last_raw_response = None
        self.last_stage = None
        self.last_reason = None
        self.last_response_chars = None

        units = audit_units or draft.audit_units()
        normal_payload = {
            "audit_units": {
                unit_id: {"path": locator.path, "text": locator.text}
                for unit_id, locator in units.items()
            },
            "story_cards": self._compact_story_cards(analysis, minimal=False),
            "source_records": bundle.prompt_text,
        }
        try:
            return await self._run_check_request(normal_payload, units, compact_retry=False)
        except Exception as exc:
            if is_token_budget_error(exc) or (
                isinstance(exc, FactCheckUnavailableError)
                and is_token_budget_error(exc.__cause__ or exc)
            ):
                self.logger.warning(
                    "Light fact-check token budget exhausted; retrying compact audit"
                )
                minimal_payload = {
                    "audit_units": {
                        unit_id: {"path": locator.path, "text": locator.text}
                        for unit_id, locator in units.items()
                    },
                    "story_cards": self._compact_story_cards(analysis, minimal=True),
                    "source_records": bundle.prompt_text,
                }
                try:
                    return await self._run_check_request(minimal_payload, units, compact_retry=True)
                except Exception as retry_exc:
                    if not self.last_reason:
                        self.last_reason = str(retry_exc)
                    raise FactCheckUnavailableError(
                        f"compact audit retry failed: {self.last_reason}"
                    ) from retry_exc
            if not self.last_reason:
                self.last_reason = str(exc)
            raise FactCheckUnavailableError(f"fact-check unavailable: {exc}") from exc

    async def repair(
        self,
        draft: ArticleDraft,
        result: FactCheckResult,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
    ) -> ArticleDraft:
        issues = [issue for issue in result.issues if issue.severity == "fix"]
        if not issues:
            return draft
        units = draft.audit_units()
        prompt = json.dumps(
            {
                "instruction": "Return JSON only as {replacements: {unit_id: replacement_text}}. Repair only listed units. Keep all other units unchanged. Remove unsupported concrete details or use the most conservative wording supported by the cards.",
                "issues": [issue.to_dict() for issue in issues],
                "units": {
                    issue.unit_id: {
                        "text": units[issue.unit_id].text,
                        "path": units[issue.unit_id].path,
                    }
                    for issue in issues
                    if issue.unit_id in units
                },
                "story_cards": analysis.to_dict(),
                "source_records": bundle.prompt_text,
            },
            ensure_ascii=False,
        )
        try:
            response = await self.provider.chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise local-news repair editor. Return only the requested replacement map.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
                temperature=0.1,
                max_tokens=self.repair_max_output_tokens,
                response_format={"type": "json_object"},
            )
            payload = json.loads(response.strip())
            replacements = payload.get("replacements", {})
            if not isinstance(replacements, dict):
                return draft
            return draft.apply_replacements(
                {str(unit_id): str(text) for unit_id, text in replacements.items()}
            )
        except Exception:
            self.logger.exception("Targeted article repair failed")
            return draft


_INTERNAL_MARKERS = re.compile(r"\b(?:TITLE|LEAD|P\d{3}|H\d{3}|S\d{6})\b")


def deterministic_preflight(markdown: str) -> None:
    """Reject structurally unusable output before publication."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("article is empty")
    if markdown.lstrip().startswith("{") or '"headline"' in markdown[:200]:
        raise ValueError("article still contains raw JSON")
    if not re.match(r"^#\s+\S", markdown.strip()):
        raise ValueError("article must start with a Markdown headline")
    match = _INTERNAL_MARKERS.search(markdown)
    if match:
        raise ValueError(f"article contains internal marker: {match.group(0)}")
