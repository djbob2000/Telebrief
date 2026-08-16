"""Non-blocking light fact checks and targeted repairs for article drafts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.ai_providers import AIProvider, is_token_budget_error
from src.editorial_models import EditorialAnalysis, PreparedBundle, is_expected_language
from src.editorial_writer import ArticleDraft, AuditUnitLocator, render_story_contexts


def _parse_publication_blocking(value: Any, severity: str) -> bool:
    if severity != "fix":
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, int) and value in {0, 1}:
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False

    # Missing/malformed provider field on FIX:
    # default to False (soft fail-open) so missing JSON field does not trigger fallback.
    return False


@dataclass
class AuditIssue:
    unit_id: str
    code: str
    original_excerpt: str
    reason: str
    suggested_direction: str
    source_refs: list[str]
    severity: str = "fix"
    publication_blocking: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditIssue":
        severity = str(data.get("severity", "fix")).lower()
        if severity not in {"fix", "warn"}:
            severity = "fix"
        publication_blocking = _parse_publication_blocking(
            data.get("publication_blocking"), severity
        )
        unit_id = str(data.get("unit_id") or data.get("id") or "")
        code = str(data.get("code") or "unspecified")
        original_excerpt = str(
            data.get("original_excerpt")
            or data.get("fragment")
            or data.get("excerpt")
            or data.get("text")
            or ""
        )
        reason = str(data.get("reason") or data.get("explanation") or data.get("message") or "")
        suggested = str(
            data.get("suggested_direction") or data.get("suggestion") or data.get("direction") or ""
        )
        raw_refs = data.get("source_refs") or data.get("refs") or data.get("sources") or []
        refs = [str(r) for r in raw_refs if isinstance(r, (str, int))]
        return cls(
            unit_id=unit_id,
            code=code,
            original_excerpt=original_excerpt,
            reason=reason,
            suggested_direction=suggested,
            source_refs=refs,
            severity=severity,
            publication_blocking=publication_blocking,
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
            "publication_blocking": self.publication_blocking,
        }


@dataclass
class FactCheckResult:
    status: str
    systemic_problem: bool = False
    issues: list[AuditIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = self._normalize_status(self.status, self.issues)

    @staticmethod
    def _normalize_status(status: str, issues: list[AuditIssue]) -> str:
        if any(issue.severity == "fix" for issue in issues) or status.upper() == "FIX":
            return "FIX"
        if issues or status.upper() == "WARN":
            return "WARN"
        return "PASS"

    @property
    def passed(self) -> bool:
        return self.status.upper() in {"PASS", "WARN"}

    @property
    def needs_repair(self) -> bool:
        return any(issue.severity == "fix" for issue in self.issues)

    @property
    def has_blocking_fixes(self) -> bool:
        return any(issue.severity == "fix" and issue.publication_blocking for issue in self.issues)

    @property
    def needs_regeneration(self) -> bool:
        return self.systemic_problem or self.has_blocking_fixes

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "systemic_problem": self.systemic_problem,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class FactCheckUnavailableError(RuntimeError):
    """Raised when the fact checking model call cannot be completed."""


class LightFactChecker:
    """Non-blocking fact-checker enforcing evidence bounds on the final draft."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        logger: logging.Logger,
        max_output_tokens: int = 65_536,
        repair_max_output_tokens: int | None = None,
        output_language: str = "Russian",
    ):
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if repair_max_output_tokens is not None and repair_max_output_tokens <= 0:
            raise ValueError("repair_max_output_tokens must be positive")

        self.provider = provider
        self.model = model
        self.logger = logger
        self.max_output_tokens = max_output_tokens
        self.repair_max_output_tokens = (
            repair_max_output_tokens if repair_max_output_tokens is not None else max_output_tokens
        )
        self.output_language = output_language
        self.last_raw_response: str | None = None
        self.last_stage: str | None = None
        self.last_reason: str | None = None
        self.last_response_chars: int | None = None

    def _build_system_prompt(self) -> str:
        return (
            "You are a light newsroom fact checker. Inspect the whole draft against the Story "
            "Cards and original source records. Return JSON only: status PASS, WARN or FIX, "
            "systemic_problem boolean, and issues array. Every issue in issues MUST contain: "
            "unit_id, code, original_excerpt, reason, suggested_direction, severity ('warn' | 'fix'), "
            "and publication_blocking (true | false). "
            f"Language requirement: Write all human-readable diagnostics (reason, suggested_direction) in {self.output_language}. "
            "Keep machine schema keys and enums (status: PASS|WARN|FIX, severity: fix|warn, publication_blocking: true|false, code, unit_id) in canonical English. "
            "Core distinction: severity='fix' means the wording should be repaired if possible. "
            "publication_blocking=true means that publishing the CURRENT wording would create a material misinformation or safety risk. "
            "Unverified claims are not blocking by themselves: publication_blocking=true applies only when a dangerous material claim is presented as established fact without attribution. "
            "When a report clearly states resident discussions, rumors, or unconfirmed status ('в чатах обсуждают', 'по сообщениям жителей', 'подтверждения пока нет'), it is non-blocking. "
            "publication_blocking is NOT a measure of stylistic quality, journalistic elegance, or whether epistemic wording could be more precise. "
            "For severity='warn', publication_blocking is always false. "
            "Non-blocking FIX (severity='fix', publication_blocking=false): "
            "Corpus boundary notes (e.g. 'официальных сроков нет' when absence from supplied reporting material occurs but blackout is confirmed); "
            "soft scale overstatements (e.g. 'в значительной части города' / 'significant part of the city' when observations come from several districts but broad prevalence or denominator is unproven); "
            "minor source/attribution characterization nuances (e.g. 'жительница' vs 'участник чата'); "
            "and headline compression that punchily summarizes without inventing new events. "
            "Blocking FIX (severity='fix', publication_blocking=true): "
            "Unverified casualties, injuries, or medical claims presented as fact; "
            "fabricated official actions, evacuation orders, or emergency instructions; "
            "accusations of specific individuals or fabricated legal/criminal claims; "
            "hard unsupported scale claims (e.g. 'весь город' / 'entire city', 'most districts', '90% города' based on narrow, street observations or single-point evidence without denominator); "
            "invented numbers, prices, payouts, laws, dates/times, or phone numbers / contacts that prompt harmful real-world action; "
            "invented causes, responsibility, or damage without basis. "
            "story_contexts is deterministic interpretation and aggregation metadata. It is authoritative for resolved entity identity, distinct-area counting and explicit flags such as majority_supported. It does not independently establish that the reported phenomenon affected an entire area and is not additional current-event evidence. "
            "Street observations indicate that a phenomenon was observed on that street/area, not across the entire district or whole city. "
            "Legitimate collective synthesis of resident observations and discussions is not a FIX merely because no single "
            "message literally contains the whole synthesized sentence. Use PASS when well supported and WARN for soft "
            "overstatement or debatable framing. "
            "Systemic problem criteria: Set systemic_problem=true only when the draft cannot be made safe by independently "
            "replacing/removing the listed audit units because unsupported material affects the core narrative structure "
            "or is distributed throughout the article. Set systemic_problem=false for localized problems in specific "
            "TITLE, LEAD, heading or paragraph units — including several such issues — when each can be repaired independently. "
            "3–5 local FIX issues do not by themselves constitute a systemic problem. "
            "If systemic_problem=true describes a material publication hazard, at least one corresponding FIX issue must have publication_blocking=true. "
            "systemic_problem by itself is an escalation hint for regeneration, not a publication ban."
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

    @staticmethod
    def _resolve_issue_unit_ids(
        result: FactCheckResult, parsed: dict[str, Any], units: dict[str, AuditUnitLocator]
    ) -> None:
        path_to_unit = {locator.path: unit_id for unit_id, locator in units.items()}
        raw_issues = parsed.get("issues", []) if isinstance(parsed, dict) else []
        for i, issue in enumerate(result.issues):
            if (
                issue.unit_id not in units
                and i < len(raw_issues)
                and isinstance(raw_issues[i], dict)
            ):
                raw_path = raw_issues[i].get("path")
                if isinstance(raw_path, list):
                    candidate_path = tuple(str(x) for x in raw_path)
                    if candidate_path in path_to_unit:
                        issue.unit_id = path_to_unit[candidate_path]
                if issue.unit_id not in units and issue.original_excerpt:
                    for uid, locator in units.items():
                        if (
                            issue.original_excerpt in locator.text
                            or locator.text in issue.original_excerpt
                        ):
                            issue.unit_id = uid
                            break

    async def _run_check_request(
        self,
        payload: dict[str, Any],
        units: dict[str, AuditUnitLocator],
        *,
        compact_retry: bool = False,
    ) -> FactCheckResult:
        system = self._build_system_prompt()
        if compact_retry:
            system += (
                "\n\nCOMPACT RETRY: Return only a minimal JSON object with status, systemic_problem, "
                "and substantive issues including severity ('warn'|'fix') and publication_blocking (true|false)."
            )
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

        self._resolve_issue_unit_ids(result, parsed, units)

        unknown_units = sorted({issue.unit_id for issue in result.issues} - set(units))
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
        rendered_contexts = render_story_contexts(getattr(bundle, "story_contexts", {}))
        normal_payload = {
            "audit_units": {
                unit_id: {"path": locator.path, "text": locator.text}
                for unit_id, locator in units.items()
            },
            "story_cards": self._compact_story_cards(analysis, minimal=False),
            "story_contexts": rendered_contexts,
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
                    "story_contexts": rendered_contexts,
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
        rendered_contexts = render_story_contexts(getattr(bundle, "story_contexts", {}))
        prompt = json.dumps(
            {
                "instruction": (
                    f"Return JSON only as {{replacements: {{unit_id: replacement_text}}}}. "
                    f"Write all replacement_text strictly in {self.output_language}. "
                    "Repair only listed units. Keep all other units unchanged. "
                    "Remove unsupported concrete details or use the most conservative wording supported by the cards."
                ),
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
                "story_contexts": rendered_contexts,
                "source_records": bundle.prompt_text,
            },
            ensure_ascii=False,
        )
        try:
            response = await self.provider.chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": f"You are a precise local-news repair editor. Write replacement text strictly in {self.output_language}. Return only the requested replacement map.",
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
            valid_replacements: dict[str, str] = {}
            for unit_id, text in replacements.items():
                if isinstance(text, str):
                    if not is_expected_language(text, self.output_language):
                        self.logger.warning(
                            "Discarding repaired unit %s: replacement text failed language validation (%s)",
                            unit_id,
                            self.output_language,
                        )
                        continue
                    valid_replacements[str(unit_id)] = text
            return draft.apply_replacements(valid_replacements)
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
