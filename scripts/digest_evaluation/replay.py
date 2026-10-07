"""Read-only replay of sealed inputs through production writer/editor/assessment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from scripts.digest_evaluation.fixtures import FrozenDigestCase
from src.ai_providers import AIProvider
from src.publication.digest_assessment import DigestAssessment, assess_digest_candidate
from src.publication.digest_editor import DigestEditor
from src.publication.digest_narrative import DigestNarrativeWriter
from src.publication.generation import _repair_digest_candidate


class DigestReplayDependencyError(ValueError):
    """An offline geographic dependency no longer matches the sealed snapshot."""


@dataclass(frozen=True)
class DigestReplayResult:
    status: Literal["accepted", "rejected", "failed"]
    assessment: DigestAssessment | None
    diagnostics: dict[str, Any]


class ReplayObserver:
    def __init__(self) -> None:
        self.outcomes: list[dict[str, Any]] = []

    async def attempt_started(self, kind: str, **kwargs: Any) -> int:
        return len(self.outcomes)

    async def attempt_finished(self, attempt_id: int, status: str, **kwargs: Any) -> None:
        metadata = kwargs.get("metadata") or {}
        self.outcomes.append(
            {
                "status": status,
                "error_kind": kwargs.get("error_kind"),
                "editor_outcome": metadata.get("editor_outcome"),
            }
        )


class CountingProvider(AIProvider):
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.calls = 0

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int = 65536,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        self.calls += 1
        return cast(
            str,
            await self.provider.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
                response_format=response_format,
            ),
        )


async def replay_digest(case: FrozenDigestCase, *, provider: Any) -> DigestReplayResult:
    start = time.monotonic()
    counted = CountingProvider(provider)
    observer = ReplayObserver()
    assessment: DigestAssessment | None = None
    status: Literal["accepted", "rejected", "failed"] = "failed"
    diagnostics: dict[str, Any] = {
        "case": case.name,
        "versions": case.implementation_versions,
        "cost": None,
        "cost_availability": "not_available",
        "writer_material_format": case.generation.get("writer_material_format", "legacy"),
        "editor_scope": case.generation.get("editor_scope", "targeted_items"),
    }
    cfg = case.generation
    try:
        slug = case.context.plan.edition_slug
        if slug:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
                raise DigestReplayDependencyError("DIGEST_REPLAY_GEOGRAPHY_ID")
            profile = Path("data/city_profiles") / f"{slug}.yaml"
            expected = cfg.get("geography_profile_hash")
            if (
                not expected
                or not profile.is_file()
                or hashlib.sha256(profile.read_bytes()).hexdigest() != expected
            ):
                raise DigestReplayDependencyError("DIGEST_REPLAY_GEOGRAPHY_CHANGED")
            diagnostics["geography_profile_hash"] = expected
        async with asyncio.timeout(float(cfg["timeout_seconds"])):
            writer = DigestNarrativeWriter(
                provider=counted,
                writer_material_format=cfg.get("writer_material_format", "legacy"),
                reasoning_effort=cfg.get("writer_reasoning_effort", "none"),
                reasoning_headroom_tokens=int(cfg.get("reasoning_headroom_tokens", 12000)),
            )
            draft = await writer.generate_narrative_draft(
                plan=case.context.plan,
                cards=case.context.frozen.analysis.cards,
                evidence=case.context.evidence,
                language=cfg["language"],
                max_output_tokens=cfg["max_output_tokens"],
                model=cfg["model"],
            )
            assessment = assess_digest_candidate(
                draft, context=case.context, allow_incomplete_coverage=True
            )
            checkpoint, _, calls = await _repair_digest_candidate(
                checkpoint=assessment.checkpoint(),
                plan=case.context.plan,
                evidence=case.context.evidence,
                editor=DigestEditor(
                    provider=counted,
                    reasoning_effort=cfg.get("editor_reasoning_effort", "none"),
                    reasoning_headroom_tokens=int(cfg.get("reasoning_headroom_tokens", 12000)),
                ),
                observer=observer,
                evaluate_candidate=lambda candidate: assess_digest_candidate(
                    candidate, context=case.context
                ).checks(),
                model=cfg["model"],
                timeout_seconds=float(cfg["timeout_seconds"]),
                implementation_versions=case.implementation_versions,
                editor_scope=cfg.get("editor_scope", "targeted_items"),
                review_without_findings=bool(cfg.get("review_without_findings", False)),
                deadline_at=start + float(cfg["timeout_seconds"]),
                support_text_by_id=case.context.support_text_by_id,
            )
            assessment = DigestAssessment(*checkpoint)
            status = "accepted" if assessment.is_safe else "rejected"
            diagnostics["editor_calls"] = calls
    except Exception as exc:
        diagnostics["error_kind"] = type(exc).__name__
    diagnostics.update(
        provider_calls=counted.calls,
        elapsed_seconds=time.monotonic() - start,
        editor_attempts=observer.outcomes,
    )
    if assessment:
        diagnostics.update(
            story_coverage=assessment.coverage.story_coverage,
            material_fact_coverage=assessment.coverage.material_fact_coverage,
            artifact_hash=assessment.artifact.content_hash,
            utf16_character_count=assessment.artifact.utf16_character_count,
            validation_codes=[v.split(":", 1)[0] for v in assessment.validation.violations],
            not_evaluated=list(assessment.validation.not_evaluated),
            quality_codes=[c.code for c in assessment.audit.checks],
            style_codes=[w.code for w in assessment.audit.prose_audit.warnings],
        )
    return DigestReplayResult(status, assessment, diagnostics)


def write_replay_result(result: DigestReplayResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        ""
        if result.status == "accepted"
        else f"{result.status.upper()} PREVIEW — DO NOT PUBLISH\n\n"
    )
    prose = (
        result.assessment.artifact.visible_text
        if result.assessment
        else "No assessed draft is available."
    )
    path.with_suffix(".txt").write_text(header + prose + "\n", encoding="utf-8")
    metadata = {"status": result.status, **result.diagnostics}
    path.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
