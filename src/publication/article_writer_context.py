from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    _support_framing,
)
from src.publication.article_coverage import ArticleCoveragePlan

if TYPE_CHECKING:
    from src.publication.article_material import ArticleMaterialProjection

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s()\-–—]{8,}\d)")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\bt\.me/\S+", re.IGNORECASE)

# Keep the writer request compact enough that the model has room for a
# coherent article response.  The complete ArticleEditorialContext remains
# available to deterministic validation; this is only the prompt projection.
ARTICLE_WRITER_CONTEXT_MAX_CHARS = 120_000
_SUPPORT_FACT_MAX_CHARS = 900
_SUPPORT_SOURCE_MAX_CHARS = 1_800
_SUPPORT_COMPACT_FACT_MAX_CHARS = 360
_PACKET_FACT_MAX_CHARS = 420
_PACKET_COMPACT_FACT_MAX_CHARS = 180
_PACKET_SUPPORT_LIMIT = {"DEVELOP": 3, "WEAVE": 2, "BRIEF": 1}
_QUOTE_ALLOWLIST_MAX_CHARS = 12_000


@dataclass(frozen=True)
class ArticleWriterMaterializationStats:
    """Counts describing the material projected into the writer prompt."""

    coverage_story_count: int
    story_packet_count: int
    packets_with_citable_support: int
    citable_support_count: int

    def to_prompt_block(self) -> str:
        return "\n".join(
            (
                "ARTICLE MATERIAL INVENTORY",
                f"coverage stories: {self.coverage_story_count}",
                f"story packets: {self.story_packet_count}",
                f"packets with citable support: {self.packets_with_citable_support}",
                f"citable support entries: {self.citable_support_count}",
            )
        )

    def to_metadata(self) -> dict[str, int]:
        return {
            "coverage_story_count": self.coverage_story_count,
            "story_packet_count": self.story_packet_count,
            "packets_with_citable_support": self.packets_with_citable_support,
            "citable_support_count": self.citable_support_count,
        }


def sanitize_writer_source_text(text: str) -> str:
    """Hide direct phone numbers and URLs from writer prompt while preserving facts."""
    if not text:
        return ""
    out = _URL_RE.sub("[link omitted]", text)
    out = _PHONE_RE.sub("[contact omitted]", out)
    return out


def format_article_context_time(value: dt.datetime | None, timezone_name: str) -> str | None:
    """Format a stored UTC timestamp in the edition's local timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid article context timezone: {timezone_name!r}") from exc
    local_value = value.astimezone(zone)
    return f"{local_value:%Y-%m-%d %H:%M} ({timezone_name})"


def _compact_text(text: str, max_chars: int) -> str:
    cleaned = " ".join((text or "").split()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= 3:
        return cleaned[:max_chars]
    return cleaned[: max_chars - 3].rstrip() + "..."


def _extract_story_microdetails(
    story_id: str,
    support_ids: Sequence[str],
    context: ArticleEditorialContext | None,
    material_projection: ArticleMaterialProjection | None = None,
) -> list[str]:
    if context is None or not hasattr(context, "support_by_id"):
        return []
    details: list[str] = []
    seen: set[str] = set()
    for sid in support_ids:
        sup = context.support_by_id.get(sid)
        if not sup:
            continue
        if material_projection is not None:
            if sid in material_projection.text_by_support_id:
                if material_projection.actions_by_support_id.get(sid) == "SUPPRESS_PROMOTION_ONLY":
                    continue
                raw = material_projection.text_by_support_id[sid]
            else:
                raw = ""
        else:
            # Prefer rich source text if it provides concrete details, otherwise text
            raw = (
                sup.source_text
                if (sup.source_text and len(sup.source_text.strip()) > 10)
                else sup.text
            )
        if not raw:
            continue
        cleaned = sanitize_writer_source_text(raw) if material_projection is None else raw
        # Collapse multiple spaces / newlines
        cleaned = " ".join(cleaned.split()).strip()
        if len(cleaned) > 130:
            cleaned = cleaned[:127] + "..."
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            details.append(cleaned)
    return details[:3]


def _render_coverage_plan(
    plan: ArticleCoveragePlan,
    context: ArticleEditorialContext | None = None,
    material_projection: ArticleMaterialProjection | None = None,
) -> str:
    lines = ["ARTICLE COVERAGE PLAN"]
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    develop_stories = [
        s for s in plan.stories if s.prominence == "DEVELOP" and s.story_id not in suppressed
    ]
    if develop_stories:
        lines.append(
            "\nОБЯЗАТЕЛЬНЫЕ КЛЮЧЕВЫЕ ТЕМЫ (DEVELOP) — КАЖДАЯ ДОЛЖНА БЫТЬ ПОДРОБНО ОТРАЖЕНА В ТЕКСТЕ:"
        )
        for ds in develop_stories:
            lines.append(f"★ DEVELOP {ds.story_id}: {ds.topic}")
            if ds.support_ids:
                lines.append(f"  Опорные факты: {', '.join(ds.support_ids[:4])}")

    if plan.sections:
        lines.append(f"\nTHEMATIC SECTIONS COUNT: {len(plan.sections)}")
        plan_by_id = plan.by_story_id
        for sec in plan.sections:
            visible_assignments = [a for a in sec.story_assignments if a.story_id not in suppressed]
            if not visible_assignments:
                continue
            lines.append(f"\nSECTION: {sec.title}")
            lines.append(f"NARRATIVE INTENT: {sec.narrative_intent}")
            visible_lead = (
                sec.lead_story_id
                if sec.lead_story_id not in suppressed
                else visible_assignments[0].story_id
            )
            lines.append(f"LEAD STORY: {visible_lead}")
            for a in visible_assignments:
                item = plan_by_id.get(a.story_id)
                topic = item.topic if item else a.story_id
                sups = item.support_ids if item else a.primary_evidence_ids
                det_sups = item.detail_support_ids if item else ()
                lines.append(f"- {a.depth} {a.story_id}: {topic}")
                lines.append(f"  SUPPORTS: {', '.join(sups)}")
                if det_sups:
                    lines.append(f"  DETAIL SUPPORTS: {', '.join(det_sups)}")

                # Extract rich human-readable microdetails
                micro_targets = list(det_sups) if det_sups else list(sups[:2])
                micro_details = _extract_story_microdetails(
                    a.story_id, micro_targets, context, material_projection
                )
                if not micro_details and a.concrete_details:
                    micro_details = [
                        d
                        for d in a.concrete_details
                        if not d.startswith("story:") and not d.startswith("op:")
                    ]
                if micro_details:
                    lines.append(f"  MICRODETAILS: {' | '.join(micro_details)}")
    else:
        for item in plan.stories:
            if item.story_id in suppressed:
                continue
            lines.append(f"- {item.prominence} {item.story_id}: {item.topic}")
            lines.append(f"  SUPPORTS: {', '.join(item.support_ids)}")
            if item.detail_support_ids:
                lines.append(f"  DETAIL SUPPORTS: {', '.join(item.detail_support_ids)}")
            micro_details = _extract_story_microdetails(
                item.story_id,
                item.detail_support_ids or item.support_ids[:2],
                context,
                material_projection,
            )
            if micro_details:
                lines.append(f"  MICRODETAILS: {' | '.join(micro_details)}")
    return "\n".join(lines)


def _render_article_story_packets(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    material_projection: ArticleMaterialProjection | None = None,
) -> tuple[list[str], list[str], ArticleWriterMaterializationStats]:
    """Materialize the zero-loss coverage plan into bounded writer packets.

    The complete plan and support index remain available to deterministic
    validation. The writer receives a small, story-local set of facts for
    every Story instead of every duplicate evidence row and pooled source
    text.
    """
    support_by_id = getattr(context, "support_by_id", {})
    supports_by_story: dict[str, list[ArticleSupport]] = defaultdict(list)
    for support in context.support_index:
        if support.publication_use != "EXCLUDE" and support.story_id:
            supports_by_story[support.story_id].append(support)

    packets: list[str] = []
    compact_packets: list[str] = []
    packets_with_citable_support = 0
    citable_support_count = 0
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    for item in coverage_plan.stories:
        if item.story_id in suppressed:
            continue
        depth = str(item.prominence)
        limit = _PACKET_SUPPORT_LIMIT.get(depth, 1)
        planned_ids = list(dict.fromkeys((*item.detail_support_ids, *item.support_ids)))

        # Longitudinal plans may pool support IDs across a thread. Prefer the
        # Story's own evidence first, then use pooled evidence only to fill
        # the small packet budget.
        own_ids = [
            support.support_id
            for support in supports_by_story.get(item.story_id, ())
            if support.support_id in planned_ids
        ]
        selected_ids = list(dict.fromkeys((*own_ids, *planned_ids)))[:limit]
        selected_supports = [
            support_by_id[sid]
            for sid in selected_ids
            if sid in support_by_id
            and (
                material_projection is None
                or material_projection.actions_by_support_id.get(sid) != "SUPPRESS_PROMOTION_ONLY"
            )
            and (
                material_projection is None
                or material_projection.text_by_support_id.get(sid, "").strip()
            )
        ]
        if selected_supports:
            packets_with_citable_support += 1
            citable_support_count += len(selected_supports)

        header = (
            f"[ARTICLE STORY PACKET {item.story_id}] depth={depth} "
            f"topic={_compact_text(item.topic, 180)}"
        )
        full_lines = [header]
        compact_lines = [header]
        for support in selected_supports:
            raw_fact = (
                material_projection.text_by_support_id.get(support.support_id, "")
                if material_projection is not None
                else sanitize_writer_source_text(support.text or support.source_text)
            )
            full_fact = _compact_text(raw_fact, _PACKET_FACT_MAX_CHARS)
            compact_fact = _compact_text(raw_fact, _PACKET_COMPACT_FACT_MAX_CHARS)
            framing = _support_framing(support)
            temporal_fields = [f"role={support.temporal_role}"]
            for field_name, value in (
                ("observed_at", support.observed_at),
                ("effective_from", support.effective_from),
                ("effective_until", support.effective_until),
            ):
                formatted = format_article_context_time(value, context.edition_timezone)
                if formatted is not None:
                    temporal_fields.append(f"{field_name}={formatted}")
            temporal = " ".join(temporal_fields)
            full_lines.append(
                f"  support={support.support_id} kind={support.evidence_kind} "
                f"framing={framing} {temporal} fact={full_fact}"
            )
            compact_lines.append(
                f"  support={support.support_id} kind={support.evidence_kind} "
                f"{temporal} fact={compact_fact}"
            )

        if not selected_supports:
            full_lines.append("  support=none fact=No citable support was materialized.")
            compact_lines.append("  support=none fact=No citable support was materialized.")

        packets.append("\n".join(full_lines))
        compact_packets.append("\n".join(compact_lines))

    return (
        packets,
        compact_packets,
        ArticleWriterMaterializationStats(
            coverage_story_count=len(coverage_plan.stories),
            story_packet_count=len(packets),
            packets_with_citable_support=packets_with_citable_support,
            citable_support_count=citable_support_count,
        ),
    )


def _fit_story_packets(
    prefix: str,
    full_packets: Sequence[str],
    compact_packets: Sequence[str],
) -> str:
    """Fit every Story packet in budget, reducing facts before Story IDs."""
    remaining = max(0, ARTICLE_WRITER_CONTEXT_MAX_CHARS - len(prefix))

    def join_if_fits(packets: Sequence[str]) -> str | None:
        body = "\n\n".join(packets)
        return body if len(body) <= remaining else None

    body = join_if_fits(full_packets) or join_if_fits(compact_packets)
    if body is None:
        # Headers are the last-resort materialization. This keeps the full
        # coverage map in the prompt while deterministic validation retains
        # the complete evidence index outside the prompt.
        headers = [packet.split("\n", 1)[0] for packet in compact_packets]
        body = join_if_fits(headers)
    if body is None:
        raise ValueError("article story packet headers exceed writer context budget")
    return "\n\n".join(part for part in (prefix, body) if part).strip()


def render_article_writer_context_with_stats(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
    *,
    include_coverage_plan: bool = True,
    material_projection: ArticleMaterialProjection | None = None,
) -> tuple[str, ArticleWriterMaterializationStats | None]:
    """Render writer context and return the materialization stats used to build it."""
    blocks: list[str] = []
    if context.edition_name:
        blocks.append(f"EDITION CONTEXT: {context.edition_name}")
        from src.domain.edition_geography import resolve_edition_geography

        geo_ctx = resolve_edition_geography(context.edition_name.lower(), context.edition_name)
        geo_section = geo_ctx.to_prompt_section().strip()
        if geo_section:
            blocks.append(geo_section)
    if context.publication_window is not None:
        blocks.append(
            f"REPORT WINDOW: {context.publication_window.lookback_start.isoformat()} .. {context.publication_window.snapshot_at.isoformat()}"
        )
        publication_as_of = format_article_context_time(
            context.publication_window.snapshot_at,
            context.edition_timezone,
        )
        if publication_as_of is not None:
            blocks.append(f"PUBLICATION AS OF: {publication_as_of}")
        lookback_hours = int(
            (
                context.publication_window.snapshot_at - context.publication_window.lookback_start
            ).total_seconds()
            // 3600
        )
        if lookback_hours >= 120:
            blocks.append(
                "LONGITUDINAL PUBLICATION DIRECTIVE:\n"
                "- Structure into comprehensive thematic chapters (e.g. Infrastructure, Transport, Consumer Market, Social Life).\n"
                "- Trace trajectory evolution over time using date anchors (e.g. [01.09] or natural dates like '1 сентября... к 4 сентября...').\n"
                "- Conclude with a dedicated closing section or outlook ('## Городской горизонт') covering upcoming deadlines, scheduled works, and unresolved questions strictly grounded in FUTURE_SCHEDULED and unresolved supports."
            )

    if coverage_plan is not None and include_coverage_plan:
        blocks.append(
            _render_coverage_plan(
                coverage_plan, context=context, material_projection=material_projection
            )
        )

    from src.publication.article_quote_allowlist import build_article_quote_allowlist

    suppressed_support_ids = (
        {
            support_id
            for support_id, action in material_projection.actions_by_support_id.items()
            if action == "SUPPRESS_PROMOTION_ONLY"
        }
        if material_projection is not None
        else set()
    )
    allowlist = build_article_quote_allowlist(
        context,
        excluded_support_ids=suppressed_support_ids,
        candidate_text_by_support_id=(
            material_projection.text_by_support_id if material_projection is not None else None
        ),
    )
    if allowlist:
        quote_lines = [
            'QUOTE ALLOWLIST (ONLY these exact primary-source phrases may be in quotation marks «...» / "..."):'
        ]
        quote_budget = _QUOTE_ALLOWLIST_MAX_CHARS - len(quote_lines[0])
        for q in allowlist:
            line = f"- «{q}»"
            if quote_budget - len(line) < 0:
                quote_lines.append("- Additional quotes are not exposed to the writer.")
                break
            quote_lines.append(line)
            quote_budget -= len(line)
        blocks.append("\n".join(quote_lines))
    else:
        blocks.append(
            "QUOTE ALLOWLIST: (NONE — quotation marks are strictly forbidden; use indirect speech only)"
        )

    allowed_support_ids: set[str] | None = None
    if coverage_plan is not None:
        allowed_support_ids = set()
        for item in coverage_plan.stories:
            allowed_support_ids.update(item.support_ids)
            allowed_support_ids.update(item.detail_support_ids)

        prefix = "\n\n".join(blocks).strip()
        packet_blocks, compact_packet_blocks, stats = _render_article_story_packets(
            context, coverage_plan, material_projection
        )
        plan_story_ids = {item.story_id for item in coverage_plan.stories}
        suppressed_count = (
            len(set(material_projection.suppressed_story_ids) & plan_story_ids)
            if material_projection
            else 0
        )
        if stats.story_packet_count + suppressed_count != stats.coverage_story_count:
            raise ValueError(
                "article story packet materialization lost coverage stories: "
                f"{stats.story_packet_count + suppressed_count}/{stats.coverage_story_count}"
            )
        prefix_with_inventory = "\n\n".join(
            part for part in (prefix, stats.to_prompt_block()) if part
        ).strip()
        return (
            _fit_story_packets(prefix_with_inventory, packet_blocks, compact_packet_blocks),
            stats,
        )

    # Several evidence rows often carry the same fact and source text (for
    # example, one fact linked to multiple fragments).  They all remain
    # available in ArticleEditorialContext for traceability, but repeating
    # their prose in the LLM prompt needlessly multiplies token usage.
    grouped_supports: list[list[ArticleSupport]] = []
    groups_by_key: dict[tuple[str, str, str, str, str], list[ArticleSupport]] = {}
    for sup in context.support_index:
        if sup.publication_use == "EXCLUDE":
            continue
        if allowed_support_ids is not None and sup.support_id not in allowed_support_ids:
            # Exclude supports that do not belong to selected stories in the coverage plan
            continue
        if material_projection is not None:
            if sup.story_id in material_projection.suppressed_story_ids:
                continue
            if (
                material_projection.actions_by_support_id.get(sup.support_id)
                == "SUPPRESS_PROMOTION_ONLY"
            ):
                continue
            projected_text = material_projection.text_by_support_id.get(sup.support_id, "")
            if not projected_text.strip():
                continue
            # The projection combines claim text with any useful source detail;
            # render it once as the fact to avoid duplicating the same payload.
            source_text = ""
            fact_text = projected_text
        else:
            source_text = sanitize_writer_source_text(sup.source_text)
            fact_text = sanitize_writer_source_text(sup.text)
        group_key = (
            _compact_text(fact_text, _SUPPORT_FACT_MAX_CHARS),
            _compact_text(source_text, _SUPPORT_SOURCE_MAX_CHARS),
            sup.support_kind,
            sup.publication_use,
            _support_framing(sup),
        )
        group = groups_by_key.get(group_key)
        if group is None:
            group = []
            groups_by_key[group_key] = group
            grouped_supports.append(group)
        group.append(sup)

    support_blocks: list[str] = []
    compact_support_blocks: list[str] = []
    for group in grouped_supports:
        sup = group[0]
        support_ids = ", ".join(item.support_id for item in group)
        roles = ",".join(sup.source_roles) if sup.source_roles else "unknown"
        role_tag: str = str(sup.temporal_role)
        if sup.publication_use == "PUBLISH" and sup.temporal_role == "CURRENT_WINDOW":
            role_tag = "CURRENT_WINDOW (VALID FOR TITLE/LEAD)"
        elif sup.temporal_role == "HISTORICAL_CONTEXT":
            role_tag = "HISTORICAL_CONTEXT (Background only - do NOT cite for Title/Lead)"

        lines = [
            f"[SUPPORT {support_ids}]",
            f"role={role_tag} kind={sup.support_kind} publication_use={sup.publication_use}",
            f"evidence_kind={sup.evidence_kind} source_roles={roles}",
            f"framing={_support_framing(sup)}",
        ]

        if sup.observed_at:
            lines.append(f"observed_at={sup.observed_at.isoformat()}")
        if sup.effective_from:
            lines.append(f"effective_from={sup.effective_from.isoformat()}")
        if sup.effective_until:
            lines.append(f"effective_until={sup.effective_until.isoformat()}")
        lines.append(f"fact={_compact_text(fact_text, _SUPPORT_FACT_MAX_CHARS)}")
        if source_text:
            lines.append(f"source={_compact_text(source_text, _SUPPORT_SOURCE_MAX_CHARS)}")
        support_blocks.append("\n".join(lines))

        compact_support_blocks.append(
            "\n".join(
                [
                    f"[SUPPORT {support_ids}]",
                    f"kind={sup.support_kind} publication_use={sup.publication_use}",
                    f"evidence_kind={sup.evidence_kind} source_roles={roles}",
                    f"framing={_support_framing(sup)}",
                    f"fact={_compact_text(fact_text, _SUPPORT_COMPACT_FACT_MAX_CHARS)}",
                ]
            )
        )

    prefix = "\n\n".join(blocks).strip()
    remaining = max(0, ARTICLE_WRITER_CONTEXT_MAX_CHARS - len(prefix))
    rendered_supports = list(compact_support_blocks)

    # First reserve a compact representation for every distinct support group
    # so a large corpus does not lose its tail merely because early sources
    # contain long prose.  Spend whatever room remains upgrading groups to
    # richer fact/source blocks.
    compact_size = sum(len(block) for block in rendered_supports) + max(
        0, (len(rendered_supports) - 1) * 2
    )
    if compact_size <= remaining:
        detail_budget = remaining - compact_size
        for index, full_block in enumerate(support_blocks):
            delta = len(full_block) - len(rendered_supports[index])
            if delta <= detail_budget:
                rendered_supports[index] = full_block
                detail_budget -= delta

    if compact_size > remaining:
        marker = (
            "[SUPPORT CONTEXT TRUNCATED]\n"
            "Additional support remains available to deterministic validation; "
            "use only the facts shown above for drafting."
        )
        available = max(0, remaining - len(marker) - 2)
        selected: list[str] = []
        used = 0
        for block in compact_support_blocks:
            extra = len(block) + (2 if selected else 0)
            if used + extra > available:
                break
            selected.append(block)
            used += extra
        rendered_supports = selected + [marker]

    rendered = "\n\n".join(([prefix] if prefix else []) + rendered_supports).strip()
    if len(rendered) > ARTICLE_WRITER_CONTEXT_MAX_CHARS:
        rendered = rendered[: ARTICLE_WRITER_CONTEXT_MAX_CHARS - 40].rstrip()
        rendered += "\n[SUPPORT CONTEXT TRUNCATED]"

    return rendered, None


def render_article_writer_context(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
    *,
    include_coverage_plan: bool = True,
    material_projection: ArticleMaterialProjection | None = None,
) -> str:
    """Render coverage-aware and sanitized support context for single-call writer."""
    rendered, _stats = render_article_writer_context_with_stats(
        context,
        coverage_plan,
        include_coverage_plan=include_coverage_plan,
        material_projection=material_projection,
    )
    return rendered
