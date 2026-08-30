from __future__ import annotations

import re

from src.publication.article_context import (
    ArticleEditorialContext,
    _support_framing,
)
from src.publication.article_coverage import ArticleCoveragePlan

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s()\-]{8,}\d)")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\bt\.me/\S+", re.IGNORECASE)


def sanitize_writer_source_text(text: str) -> str:
    """Hide direct phone numbers and URLs from writer prompt while preserving facts."""
    if not text:
        return ""
    out = _URL_RE.sub("[link omitted]", text)
    out = _PHONE_RE.sub("[contact omitted]", out)
    return out


def _render_coverage_plan(plan: ArticleCoveragePlan) -> str:
    lines = ["ARTICLE COVERAGE PLAN"]
    for item in plan.stories:
        lines.append(f"- {item.prominence} {item.story_id}: {item.topic}")
        lines.append(f"  SUPPORTS: {', '.join(item.support_ids)}")
        if item.detail_support_ids:
            lines.append(f"  DETAIL SUPPORTS: {', '.join(item.detail_support_ids)}")
    return "\n".join(lines)


def render_article_writer_context(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
) -> str:
    """Render coverage-aware and sanitized support context for single-call writer."""
    blocks: list[str] = []
    if context.edition_name:
        blocks.append(f"EDITION CONTEXT: {context.edition_name}")
    if context.publication_window is not None:
        blocks.append(
            f"REPORT WINDOW: {context.publication_window.lookback_start.isoformat()} .. {context.publication_window.snapshot_at.isoformat()}"
        )
    if coverage_plan is not None:
        blocks.append(_render_coverage_plan(coverage_plan))

    for sup in context.support_index:
        if sup.publication_use == "EXCLUDE":
            continue
        roles = ",".join(sup.source_roles) if sup.source_roles else "unknown"
        lines = [
            f"[SUPPORT {sup.support_id}]",
            f"role={sup.temporal_role} kind={sup.support_kind} publication_use={sup.publication_use}",
            f"evidence_kind={sup.evidence_kind} source_roles={roles}",
            f"framing={_support_framing(sup)}",
        ]
        if sup.observed_at:
            lines.append(f"observed_at={sup.observed_at.isoformat()}")
        if sup.effective_from:
            lines.append(f"effective_from={sup.effective_from.isoformat()}")
        if sup.effective_until:
            lines.append(f"effective_until={sup.effective_until.isoformat()}")
        lines.append(f"fact={sup.text}")
        if sup.source_text:
            sanitized_source = sanitize_writer_source_text(sup.source_text)
            lines.append(f"source={sanitized_source}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks).strip()
