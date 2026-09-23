"""Read-only replay of an existing frozen article publication run."""

from __future__ import annotations

import datetime as dt
import logging
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.article_generator import ArticleGenerator
from src.config_loader import Config
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.policies import ARTICLE_PUBLICATION_TYPES
from src.publication.repository import PublicationRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleRunPreview:
    """Article text and safe diagnostics generated from a frozen publication run."""

    run_id: int
    publication_type: str
    edition_slug: str
    snapshot_at: dt.datetime
    title: str
    lead: str
    body: str
    diagnostics: dict[str, object]

    @property
    def markdown(self) -> str:
        """Render the article as Markdown without adding synthetic prose."""
        parts = [f"# {self.title}" if self.title else "# Вечерняя статья"]
        if self.lead and not (self.body and self.body.startswith(self.lead)):
            parts.extend(["", self.lead])
        if self.body:
            parts.extend(["", self.body])
        return "\n".join(parts).rstrip() + "\n"


@dataclass
class _CapturedAttempt:
    attempt_id: int
    kind: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    status: str = "started"
    error_kind: str | None = None
    writer_metadata: dict[str, Any] = field(default_factory=dict)
    final_metadata: dict[str, Any] = field(default_factory=dict)


class _MemoryGenerationAttemptObserver:
    """Capture attempt diagnostics in process memory without database writes."""

    def __init__(self) -> None:
        self._next_id = 0
        self._attempts: dict[int, _CapturedAttempt] = {}

    async def attempt_started(
        self,
        kind: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._next_id += 1
        attempt = _CapturedAttempt(
            attempt_id=self._next_id,
            kind=kind,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            writer_metadata=_safe_writer_metadata(metadata),
        )
        self._attempts[attempt.attempt_id] = attempt
        return attempt.attempt_id

    async def attempt_finished(
        self,
        attempt_id: int,
        status: str,
        *,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.error_kind = error_kind
        attempt.final_metadata = _safe_final_metadata(metadata)

    def to_metadata(self) -> dict[str, object]:
        return {
            "attempt_count": len(self._attempts),
            "attempts": [
                {
                    "kind": attempt.kind,
                    "status": attempt.status,
                    **({"provider": attempt.provider} if attempt.provider else {}),
                    **({"model": attempt.model} if attempt.model else {}),
                    **({"prompt_hash": attempt.prompt_hash} if attempt.prompt_hash else {}),
                    **({"error_kind": attempt.error_kind} if attempt.error_kind else {}),
                    **({"writer": attempt.writer_metadata} if attempt.writer_metadata else {}),
                    **({"final": attempt.final_metadata} if attempt.final_metadata else {}),
                }
                for attempt in self._attempts.values()
            ],
        }


def _safe_writer_metadata(value: Any) -> dict[str, Any]:
    """Apply the article finalizer's explicit metadata allowlist."""
    if not isinstance(value, dict):
        return {}
    from src.publication.article_finalization import _safe_writer_metadata as safe_metadata

    return safe_metadata(value)


def _safe_final_metadata(value: Any) -> dict[str, Any]:
    """Keep versioned quality and composition diagnostics, never article/source text."""
    if not isinstance(value, dict):
        return {}
    from src.publication.article_finalization import (
        _compact_composition_value,
        _compact_quality_value,
    )
    from src.publication.article_finalization import (
        _safe_writer_metadata as safe_writer_metadata,
    )

    result: dict[str, Any] = {
        key: value[key]
        for key in (
            "status",
            "winning_kind",
            "writer_status",
            "recovery_mode",
            "planned_story_count",
            "ai_covered_story_count",
            "supplemented_story_count",
            "final_covered_story_count",
            "ai_story_coverage",
            "final_story_coverage",
            "evidence_boundary_passed",
            "quality_gate_passed",
            "coverage_only_diagnostic",
        )
        if key in value and isinstance(value[key], (str, int, float, bool, type(None)))
    }
    for key in (
        "reader_quality",
        "quality_before_edit",
        "quality_after_edit",
        "quality_after_finalization",
    ):
        quality = _compact_quality_value(value.get(key))
        if quality is not None:
            result[key] = quality

    composition = _compact_composition_value(value.get("composition"))
    if composition is not None:
        result["composition"] = composition

    writer = safe_writer_metadata(value.get("writer_attempt"))
    if writer:
        result["writer_attempt"] = writer

    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = {
            key: coverage[key]
            for key in (
                "planned_story_count",
                "covered_story_count",
                "develop_story_coverage",
                "weave_story_coverage",
                "brief_story_coverage",
                "planned_detail_support_count",
                "covered_detail_support_count",
                "detail_support_coverage",
            )
            if isinstance(coverage.get(key), (int, float))
        }
    return result


async def build_article_preview_from_run(
    run_id: int,
    *,
    config: Config,
    expected_edition_slug: str | None = None,
) -> ArticleRunPreview:
    """Generate an article from a run's sealed inputs without changing publication state."""
    runtime = get_runtime()
    repo = PublicationRepository()
    adapter = EventEditorialAdapter(uow=runtime.uow, repo=repo)

    async with runtime.uow.transaction() as conn:
        run = await repo.get_run_by_id(conn, run_id)
        if run is None:
            raise ValueError(f"publication run {run_id} not found")
        if run.publication_type not in ARTICLE_PUBLICATION_TYPES:
            raise ValueError(
                f"publication run {run_id} has unsupported article preview type "
                f"{run.publication_type!r}"
            )

        edition_cursor = await conn.execute(
            "SELECT slug, timezone FROM editions WHERE id = %s",
            (run.edition_id,),
        )
        edition_row = await edition_cursor.fetchone()
        if edition_row is None or not edition_row[0]:
            raise ValueError(f"publication run {run_id} has no edition")
        edition_slug = str(edition_row[0]).strip()
        if expected_edition_slug is not None and expected_edition_slug != edition_slug:
            raise ValueError(
                f"requested edition {expected_edition_slug!r} conflicts with frozen run "
                f"edition {edition_slug!r}"
            )
        timezone_name = str(edition_row[1]).strip() if edition_row[1] else ""
        if not timezone_name:
            raise ValueError(f"publication run {run_id} edition is missing timezone")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"publication run {run_id} edition has invalid timezone {timezone_name!r}"
            ) from exc

        inputs = await repo.load_sealed_inputs(conn, run_id)
        if not inputs:
            raise ValueError(f"publication run {run_id} has no sealed inputs")
        frozen = await adapter.adapt_inputs_on(
            conn,
            run_id,
            inputs=inputs,
            include_anchor_publications=False,
        )

    article_context = getattr(frozen.analysis, "article_context", None)
    if article_context is not None and article_context.edition_timezone != timezone_name:
        # Guard the adapter's legacy UTC default: replay must use the stored
        # edition zone, and missing timezone data must never pass silently.
        frozen = replace(
            frozen,
            analysis=replace(
                frozen.analysis,
                article_context=replace(article_context, edition_timezone=timezone_name),
            ),
        )

    observer = _MemoryGenerationAttemptObserver()
    # Production may persist prompt/draft debug artifacts under the mounted data
    # directory. Frozen replay is a dry-run, so keep that setting isolated from
    # the caller and disable those writes for this generator instance.
    preview_config = deepcopy(config)
    preview_config.settings.article.save_debug_artifacts = False
    generator = ArticleGenerator(config=preview_config, logger=logger)
    title, lead, body = await generator.generate_from_frozen_input(
        frozen,
        attempt_observer=observer,
    )

    return ArticleRunPreview(
        run_id=run.id,
        publication_type=run.publication_type,
        edition_slug=edition_slug,
        snapshot_at=run.snapshot_at,
        title=title,
        lead=lead,
        body=body,
        diagnostics={
            "schema_version": "article-run-preview-v1",
            "run_id": run.id,
            "publication_type": run.publication_type,
            "edition_slug": edition_slug,
            "snapshot_at": run.snapshot_at.isoformat(),
            "edition_timezone": timezone_name,
            "generation": observer.to_metadata(),
        },
    )


__all__ = ["ArticleRunPreview", "build_article_preview_from_run"]
