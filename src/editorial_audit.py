"""Non-blocking light fact checks and targeted repairs for article drafts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.ai_providers import AIProvider
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
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.logger = logger
        self.max_output_tokens = max_output_tokens

    async def check(
        self,
        draft: ArticleDraft,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        audit_units: dict[str, AuditUnitLocator] | None = None,
    ) -> FactCheckResult:
        units = audit_units or draft.audit_units()
        system = (
            "You are a light newsroom fact checker. Inspect the whole draft against the Story "
            "Cards and original source records. Return JSON only: status PASS, WARN or FIX, "
            "systemic_problem boolean, and issues. Find only new concrete independently "
            "verifiable facts without support: numbers, prices, dates, names, official actions, "
            "causes, mechanisms, damage, sales, medical/legal/military claims, casualties and "
            "precise scale. Ordinary synthesis such as saying a topic was prominent may pass "
            "when supported by the supplied material. Attribution and source_refs are inspection "
            "aids, not proof by themselves. WARN is non-blocking; use FIX only when a local "
            "fragment must be changed."
        )
        user = json.dumps(
            {
                "draft": draft.to_dict(),
                "audit_units": {
                    unit_id: {"path": locator.path, "text": locator.text}
                    for unit_id, locator in units.items()
                },
                "story_cards": analysis.to_dict(),
                "source_records": bundle.prompt_text,
            },
            ensure_ascii=False,
        )
        try:
            response = await self.provider.chat_completion(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=self.model,
                temperature=0.1,
                max_tokens=self.max_output_tokens,
                response_format={"type": "json_object"},
            )
            result = FactCheckResult.from_dict(json.loads(response.strip()))
        except Exception as exc:
            raise FactCheckUnavailableError("light fact-check unavailable") from exc
        unknown_units = sorted(set(issue.unit_id for issue in result.issues) - set(units))
        if unknown_units:
            raise ValueError(f"fact-check returned unknown audit units: {', '.join(unknown_units)}")
        return result

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
                max_tokens=self.max_output_tokens,
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
