from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    _support_framing,
)
from src.publication.article_coverage import ArticleCoveragePlan
from src.timezones import get_timezone, normalize_timezone_name

if TYPE_CHECKING:
    from src.publication.article_brief import ArticleEditorialBrief
    from src.publication.article_composition import ArticleCompositionPlan
    from src.publication.article_material import ArticleMaterialProjection

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s()\-–—]{8,}\d)")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\bt\.me/\S+", re.IGNORECASE)
_SUPPORT_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")

# Keep the writer request compact enough that the model has room for a
# coherent article response.  The complete ArticleEditorialContext remains
# available to deterministic validation; this is only the prompt projection.
# Both production writer models have approximately a 1M-token context window.
# Keep a large margin for the system prompt, request envelope, tokenizer
# expansion, and the 65,536-token Event-First writer completion ceiling while
# allowing broad city-life editions to retain every selected Story packet.
ARTICLE_WRITER_CONTEXT_MAX_CHARS = 500_000
_SUPPORT_FACT_MAX_CHARS = 900
_SUPPORT_SOURCE_MAX_CHARS = 1_800
_SUPPORT_COMPACT_FACT_MAX_CHARS = 360
_QUOTE_ALLOWLIST_MAX_CHARS = 12_000
ArticleWriterMaterializationMode = Literal["packetized", "holistic", "brief"]


@dataclass(frozen=True)
class ArticleWriterMaterializationStats:
    """Counts describing the material projected into the writer prompt."""

    coverage_story_count: int
    story_packet_count: int
    packets_with_citable_support: int
    citable_support_count: int
    bundle_count: int = 0
    narrative_line_count: int = 0
    composition_group_count: int = 0
    group_size_distribution: tuple[tuple[int, int], ...] = ()
    rendered_packet_representation: str = "full"
    materialization_mode: ArticleWriterMaterializationMode = "packetized"

    def to_prompt_block(self) -> str:
        if self.materialization_mode in {"holistic", "brief"}:
            if self.materialization_mode == "brief":
                return "\n".join(
                    (
                        "ARTICLE MATERIAL INVENTORY",
                        "materialization mode: validated editorial brief",
                        f"narrative lines: {self.narrative_line_count}",
                        f"citable support entries: {self.citable_support_count}",
                    )
                )
            return "\n".join(
                (
                    "ARTICLE MATERIAL INVENTORY",
                    "materialization mode: holistic evidence dossier",
                    f"coverage stories: {self.coverage_story_count}",
                    f"composition groups: {self.composition_group_count}",
                    f"narrative lines: {self.narrative_line_count}",
                    "group sizes: "
                    + (
                        ", ".join(f"{size}={count}" for size, count in self.group_size_distribution)
                        or "none"
                    ),
                    f"citable support entries: {self.citable_support_count}",
                )
            )
        return "\n".join(
            (
                "ARTICLE MATERIAL INVENTORY",
                f"coverage stories: {self.coverage_story_count}",
                f"story packets: {self.story_packet_count}",
                f"composition groups: {self.composition_group_count}",
                f"narrative lines: {self.narrative_line_count}",
                "group sizes: "
                + (
                    ", ".join(f"{size}={count}" for size, count in self.group_size_distribution)
                    or "none"
                ),
                f"rendered packet representation: {self.rendered_packet_representation}",
                f"packets with citable support: {self.packets_with_citable_support}",
                f"citable support entries: {self.citable_support_count}",
            )
        )

    def to_metadata(self) -> dict[str, object]:
        return {
            "coverage_story_count": self.coverage_story_count,
            "story_packet_count": self.story_packet_count,
            "bundle_count": self.bundle_count,
            "narrative_line_count": self.narrative_line_count,
            "composition_group_count": self.composition_group_count,
            "group_size_distribution": {
                str(size): count for size, count in self.group_size_distribution
            },
            "rendered_packet_representation": self.rendered_packet_representation,
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
        timezone_name = normalize_timezone_name(timezone_name)
        zone = get_timezone(timezone_name)
    except ValueError as exc:
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


def _support_story_id(support_id: str) -> str:
    match = _SUPPORT_STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""


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


def _render_composition_plan(
    plan: ArticleCoveragePlan,
    composition_plan: ArticleCompositionPlan,
    *,
    context: ArticleEditorialContext | None = None,
    material_projection: ArticleMaterialProjection | None = None,
) -> str:
    """Render one compact roadmap; exact claims and support IDs live in packets."""
    plan_by_id = plan.by_story_id
    suppressed = set(composition_plan.suppressed_story_ids)
    groups_by_id = {group.group_id: group for group in composition_plan.groups}
    lines = [
        "ARTICLE COMPOSITION ROADMAP",
        "Depth controls space, not eligibility. Exact facts, attribution, and time fields are in each Story packet.",
    ]
    for narrative_line in composition_plan.narrative_lines:
        visible_groups = [
            groups_by_id[group_id]
            for group_id in narrative_line.group_ids
            if group_id in groups_by_id
            and any(
                member.story_id in plan_by_id and member.story_id not in suppressed
                for member in groups_by_id[group_id].members
            )
        ]
        if not visible_groups:
            continue
        heading = (
            f" heading_hint={_compact_text(narrative_line.heading_hint, 120)!r}"
            if narrative_line.heading_hint
            else ""
        )
        lines.append(
            f"\nLINE {narrative_line.line_id} depth={narrative_line.prominence}{heading}: "
            f"{_compact_text(narrative_line.narrative_intent, 240)}"
        )
        for group in visible_groups:
            role = _compact_text(group.relation.replace("_", " "), 100)
            lines.append(f"  GROUP {group.group_id} relation={group.relation} role={role}")
    lines.append(
        "Time fields: effective_from/effective_until describe event or service time; "
        "observed_at describes when a report was made and does not establish an event start."
    )
    return "\n".join(lines)


def _render_article_story_packets(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    material_projection: ArticleMaterialProjection | None = None,
    composition_plan: ArticleCompositionPlan | None = None,
) -> tuple[list[str], list[str], ArticleWriterMaterializationStats]:
    """Materialize the zero-loss coverage plan into bounded writer packets.

    The complete plan and support index remain available to deterministic
    validation. The writer receives a small, story-local set of facts for
    every Story instead of every duplicate evidence row and pooled source
    text.
    """
    support_by_id = getattr(context, "support_by_id", {})
    coverage_by_story = {item.story_id: item for item in coverage_plan.stories}
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    planned_supports_by_owner: dict[str, list[str]] = defaultdict(list)
    for item in coverage_plan.stories:
        if item.story_id in suppressed:
            continue
        planned_ids = dict.fromkeys((*item.detail_support_ids, *item.support_ids))
        for support_id in planned_ids:
            support = support_by_id.get(support_id)
            if support is None:
                raise ValueError(f"article coverage plan references missing support {support_id!r}")
            if support.support_id != support_id:
                raise ValueError(
                    f"article support map key {support_id!r} resolves to {support.support_id!r}"
                )
            # Longitudinal StoryThread plans can pool IDs from the full
            # provenance index, including context-only evidence. A writer
            # packet is a citable-material boundary, so only PUBLISH supports
            # may cross it; validation can still retain CONTEXT evidence.
            if support.publication_use != "PUBLISH" or support.evidence_kind == "resident_question":
                continue

            encoded_owner = _support_story_id(support_id)
            owner = support.story_id or encoded_owner or item.story_id
            if encoded_owner and encoded_owner != owner:
                raise ValueError(
                    f"article support {support_id!r} encodes owner {encoded_owner!r} "
                    f"but belongs to {owner!r}"
                )
            if owner not in coverage_by_story:
                raise ValueError(
                    f"article coverage plan has no packet for owner {owner!r} "
                    f"of planned support {support_id!r}"
                )
            if owner in suppressed:
                continue
            owner_support_ids = planned_supports_by_owner[owner]
            if support_id not in owner_support_ids:
                owner_support_ids.append(support_id)

    packets: list[str] = []
    compact_packets: list[str] = []
    packets_with_citable_support = 0
    citable_support_count = 0
    group_by_story_id = composition_plan.group_by_story_id if composition_plan is not None else {}
    group_size_counts = Counter(
        member_count
        for group in (composition_plan.groups if composition_plan is not None else ())
        if (member_count := sum(1 for member in group.members if member.story_id not in suppressed))
    )
    for item in coverage_plan.stories:
        if item.story_id in suppressed:
            continue
        depth = str(item.prominence)
        selected_ids = planned_supports_by_owner.get(item.story_id, [])
        if material_projection is not None:
            materialized_ids: list[str] = []
            for support_id in selected_ids:
                action = material_projection.actions_by_support_id.get(support_id)
                if action == "SUPPRESS_PROMOTION_ONLY":
                    continue
                projected_text = material_projection.text_by_support_id.get(support_id, "")
                if action == "TRIM_DIRECTORY" and not projected_text.strip():
                    continue
                if action not in {"KEEP", "TRIM_DIRECTORY"} or not projected_text.strip():
                    raise ValueError(
                        f"planned article support {support_id!r} has no projected citable text"
                    )
                materialized_ids.append(support_id)
            selected_ids = materialized_ids
        else:
            for support_id in selected_ids:
                support = support_by_id[support_id]
                raw_text = sanitize_writer_source_text(
                    support.text.strip() or support.source_text.strip()
                )
                if not raw_text.strip():
                    raise ValueError(f"planned article support {support_id!r} has no citable text")
        selected_supports = [support_by_id[sid] for sid in selected_ids]
        if selected_supports:
            packets_with_citable_support += 1
            citable_support_count += len(selected_supports)

        composition_group = group_by_story_id.get(item.story_id)
        line_reference = (
            f" line={composition_group.narrative_line_id}" if composition_group is not None else ""
        )
        group_reference = (
            f" group={composition_group.group_id}" if composition_group is not None else ""
        )
        header = (
            f"[ARTICLE STORY PACKET {item.story_id}] depth={depth}"
            f"{line_reference}{group_reference} "
            f"topic={_compact_text(item.topic, 180)}"
        )
        full_lines = []
        compact_lines = []
        full_lines.append(header)
        compact_lines.append(header)
        for support in selected_supports:
            raw_fact = (
                material_projection.text_by_support_id.get(support.support_id, "")
                if material_projection is not None
                else sanitize_writer_source_text(
                    support.text.strip() or support.source_text.strip()
                )
            )
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
                f"framing={framing} {temporal} fact={raw_fact}"
            )
            compact_lines.append(
                f"  support={support.support_id} framing={framing} {temporal} fact={raw_fact}"
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
            bundle_count=(len(composition_plan.groups) if composition_plan is not None else 0),
            narrative_line_count=(
                len(composition_plan.narrative_lines) if composition_plan is not None else 0
            ),
            composition_group_count=(
                len(composition_plan.groups) if composition_plan is not None else 0
            ),
            group_size_distribution=tuple(sorted(group_size_counts.items())),
        ),
    )


def _fit_story_packets(
    prefix: str,
    full_packets: Sequence[str],
    compact_packets: Sequence[str],
) -> tuple[str, str]:
    """Fit every Story packet in budget without dropping facts or Story IDs."""
    remaining = max(0, ARTICLE_WRITER_CONTEXT_MAX_CHARS - len(prefix))

    def join_if_fits(packets: Sequence[str]) -> str | None:
        body = "\n\n".join(packets)
        return body if len(body) <= remaining else None

    body = join_if_fits(full_packets)
    representation = "full"
    if body is None:
        body = join_if_fits(compact_packets)
        representation = "compact"
    if body is None:
        raise ValueError(
            "article Story packets exceed writer context budget after compact materialization"
        )
    return "\n\n".join(part for part in (prefix, body) if part).strip(), representation


def render_article_writer_context_with_stats(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
    *,
    include_coverage_plan: bool = True,
    material_projection: ArticleMaterialProjection | None = None,
    composition_plan: ArticleCompositionPlan | None = None,
    materialization_mode: ArticleWriterMaterializationMode = "packetized",
) -> tuple[str, ArticleWriterMaterializationStats | None]:
    """Render writer context and return the materialization stats used to build it."""
    if materialization_mode not in {"packetized", "holistic"}:
        raise ValueError(f"Invalid article writer materialization mode: {materialization_mode!r}")
    # Validate even when this particular context has no timestamps. Article metadata
    # and all future packet times must use the configured edition timezone.
    try:
        get_timezone(context.edition_timezone)
    except ValueError as exc:
        raise ValueError(f"Invalid article context timezone: {context.edition_timezone!r}") from exc
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
                "- Organize the article into coherent thematic movements supported by the material.\n"
                "- Trace trajectory evolution with precise effective-time anchors when those times are supplied. observed_at is report chronology, not an event start.\n"
                "- Finish on a supported development, consequence, or unresolved question when the material provides one; do not use a fixed closing heading or repeat the lead's premise."
            )

    if coverage_plan is not None and include_coverage_plan:
        blocks.append(
            _render_composition_plan(
                coverage_plan,
                composition_plan,
                context=context,
                material_projection=material_projection,
            )
            if composition_plan is not None
            else _render_coverage_plan(
                coverage_plan, context=context, material_projection=material_projection
            )
        )

    allowed_support_ids: set[str] | None = None
    if coverage_plan is not None:
        allowed_support_ids = set()
        suppressed_story_ids = (
            set(material_projection.suppressed_story_ids) if material_projection else set()
        )
        for item in coverage_plan.stories:
            if item.story_id in suppressed_story_ids:
                continue
            allowed_support_ids.update(item.support_ids)
            allowed_support_ids.update(item.detail_support_ids)

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
    if materialization_mode == "holistic" and allowed_support_ids is not None:
        suppressed_support_ids.update(
            support.support_id
            for support in context.support_index
            if support.support_id not in allowed_support_ids
        )
    allowlist = build_article_quote_allowlist(
        context,
        excluded_support_ids=suppressed_support_ids,
        excluded_story_ids=(
            material_projection.suppressed_story_ids if material_projection is not None else ()
        ),
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

    if coverage_plan is not None and materialization_mode == "packetized":
        prefix = "\n\n".join(blocks).strip()
        packet_blocks, compact_packet_blocks, stats = _render_article_story_packets(
            context, coverage_plan, material_projection, composition_plan
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
        representation = stats.rendered_packet_representation
        for _ in range(3):
            stats = replace(stats, rendered_packet_representation=representation)
            prefix_with_inventory = "\n\n".join(
                part for part in (prefix, stats.to_prompt_block()) if part
            ).strip()
            rendered, actual_representation = _fit_story_packets(
                prefix_with_inventory, packet_blocks, compact_packet_blocks
            )
            if actual_representation == representation:
                return rendered, stats
            representation = actual_representation
        raise ValueError("article writer packet representation did not stabilize")

    # Several evidence rows often carry the same fact and source text (for
    # example, one fact linked to multiple fragments).  They all remain
    # available in ArticleEditorialContext for traceability, but repeating
    # their prose in the LLM prompt needlessly multiplies token usage.
    grouped_supports: list[list[ArticleSupport]] = []
    groups_by_key: dict[tuple[str, str, str, str, str], list[ArticleSupport]] = {}
    for sup in context.support_index:
        if sup.publication_use == "EXCLUDE":
            continue
        if materialization_mode == "holistic" and sup.publication_use != "PUBLISH":
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

    holistic_stats: ArticleWriterMaterializationStats | None = None
    if coverage_plan is not None and materialization_mode == "holistic":
        suppressed_story_ids = (
            set(material_projection.suppressed_story_ids) if material_projection else set()
        )
        group_size_counts = Counter(
            member_count
            for group in (composition_plan.groups if composition_plan is not None else ())
            if (
                member_count := sum(
                    1 for member in group.members if member.story_id not in suppressed_story_ids
                )
            )
        )
        holistic_stats = ArticleWriterMaterializationStats(
            coverage_story_count=len(coverage_plan.stories),
            story_packet_count=0,
            packets_with_citable_support=0,
            citable_support_count=len(
                {support.support_id for group in grouped_supports for support in group}
            ),
            bundle_count=(len(composition_plan.groups) if composition_plan is not None else 0),
            narrative_line_count=(
                len(composition_plan.narrative_lines) if composition_plan is not None else 0
            ),
            composition_group_count=(
                len(composition_plan.groups) if composition_plan is not None else 0
            ),
            group_size_distribution=tuple(sorted(group_size_counts.items())),
            rendered_packet_representation="holistic",
            materialization_mode="holistic",
        )
        blocks.append(holistic_stats.to_prompt_block())

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

    return rendered, holistic_stats


def render_article_writer_context(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
    *,
    include_coverage_plan: bool = True,
    material_projection: ArticleMaterialProjection | None = None,
    composition_plan: ArticleCompositionPlan | None = None,
    materialization_mode: ArticleWriterMaterializationMode = "packetized",
) -> str:
    """Render coverage-aware and sanitized support context for single-call writer."""
    rendered, _stats = render_article_writer_context_with_stats(
        context,
        coverage_plan,
        include_coverage_plan=include_coverage_plan,
        material_projection=material_projection,
        composition_plan=composition_plan,
        materialization_mode=materialization_mode,
    )
    return rendered


def render_article_editorial_brief_context(
    context: ArticleEditorialContext,
    brief: ArticleEditorialBrief,
    material_projection: ArticleMaterialProjection,
) -> tuple[str, ArticleWriterMaterializationStats]:
    """Render only the validated brief and its cited projected evidence for the writer."""
    try:
        get_timezone(context.edition_timezone)
    except ValueError as exc:
        raise ValueError(f"Invalid article context timezone: {context.edition_timezone!r}") from exc

    dispositions_by_story = {item.story_id: item for item in brief.dispositions}
    omitted_story_ids = {
        story_id
        for story_id, disposition in dispositions_by_story.items()
        if disposition.depth == "OMIT"
    }
    visible_story_ids = {story_id for line in brief.lines for story_id in line.story_ids}
    if visible_story_ids & omitted_story_ids:
        raise ValueError("article editorial brief places an OMIT Story in a narrative line")

    blocks: list[str] = []
    if context.edition_name:
        blocks.append(f"EDITION CONTEXT: {context.edition_name}")
        from src.domain.edition_geography import resolve_edition_geography

        geography = resolve_edition_geography(context.edition_name.lower(), context.edition_name)
        geography_section = geography.to_prompt_section().strip()
        if geography_section:
            blocks.append(geography_section)
    blocks.append(f"EDITION TIMEZONE: {context.edition_timezone}")
    if context.publication_window is not None:
        window = context.publication_window
        blocks.append(
            f"REPORT WINDOW: {window.lookback_start.isoformat()} .. {window.snapshot_at.isoformat()}"
        )
        as_of = format_article_context_time(window.snapshot_at, context.edition_timezone)
        if as_of:
            blocks.append(f"PUBLICATION AS OF: {as_of}")

    central_support_ids = tuple(dict.fromkeys(brief.central_support_ids))
    lines = [
        "ARTICLE EDITORIAL BRIEF",
        "Follow this central line and narrative order. Depth sets space, not eligibility.",
        f"CENTRAL LINE: {brief.central_line.strip()}",
        "CENTRAL SUPPORTS: " + ", ".join(central_support_ids),
        "NARRATIVE LINES (in order):",
    ]

    # Keep each reference on its line, while rendering the underlying evidence
    # once below even when the central line or several movements cite it.
    cited_by: dict[str, list[str]] = defaultdict(list)
    for support_id in central_support_ids:
        cited_by.setdefault(support_id, []).append("central line")
        support = getattr(context, "support_by_id", {}).get(support_id)
        if support is not None and support.story_id in omitted_story_ids:
            raise ValueError(
                f"article editorial brief central line cites omitted Story {support.story_id!r}"
            )
    visible_lines = []
    for line in brief.lines:
        if any(story_id in omitted_story_ids for story_id in line.story_ids):
            raise ValueError(
                f"article editorial brief line {line.line_id!r} contains an OMIT Story"
            )
        visible_lines.append(line)
        support_ids = tuple(dict.fromkeys(line.support_ids))
        salient_ids = tuple(dict.fromkeys(line.salient_support_ids))
        caveat_ids = tuple(dict.fromkeys(line.caveat_support_ids))
        lines.extend(
            (
                f"\nLINE {line.line_id} depth={line.depth} relation={line.relation}",
                f"INTENT: {line.editorial_intent.strip()}",
                f"STORIES: {', '.join(line.story_ids)}",
                "SUPPORTS: " + ", ".join(support_ids),
                "SALIENT SUPPORTS: " + (", ".join(salient_ids) or "none"),
                "CAVEAT SUPPORTS: " + (", ".join(caveat_ids) or "none"),
            )
        )
        for support_id in support_ids:
            support = getattr(context, "support_by_id", {}).get(support_id)
            support_owner = (
                support.story_id or _support_story_id(support_id) if support is not None else ""
            )
            if support_owner and support_owner not in line.story_ids:
                raise ValueError(
                    f"article editorial brief line {line.line_id!r} cites support owned by "
                    f"{support_owner!r}"
                )
            cited_by.setdefault(support_id, []).append(line.line_id)

    support_by_id = getattr(context, "support_by_id", {})
    rendered_support_ids: set[str] = set()
    evidence_blocks: list[str] = []
    for support_id, references in cited_by.items():
        support = support_by_id.get(support_id)
        if support is None or support.support_id != support_id:
            raise ValueError(f"article editorial brief references missing support {support_id!r}")
        if support.publication_use != "PUBLISH" or support.evidence_kind == "resident_question":
            raise ValueError(
                f"article editorial brief references non-citable support {support_id!r}"
            )
        action = material_projection.actions_by_support_id.get(support_id)
        if action not in {"KEEP", "TRIM_DIRECTORY"}:
            raise ValueError(
                f"article editorial brief support {support_id!r} did not survive projection"
            )
        fact = sanitize_writer_source_text(
            material_projection.text_by_support_id.get(support_id, "").strip()
        )
        if not fact:
            raise ValueError(
                f"article editorial brief support {support_id!r} has no projected text"
            )
        temporal_fields = [f"role={support.temporal_role}"]
        for field_name, value in (
            ("observed_at", support.observed_at),
            ("effective_from", support.effective_from),
            ("effective_until", support.effective_until),
        ):
            formatted = format_article_context_time(value, context.edition_timezone)
            if formatted is not None:
                temporal_fields.append(f"{field_name}={formatted}")
        story_id = support.story_id
        if not story_id:
            story_id = _support_story_id(support_id)
        if story_id in omitted_story_ids:
            raise ValueError(
                f"article editorial brief cites support for omitted Story {story_id!r}"
            )
        evidence_blocks.append(
            "\n".join(
                (
                    f"[SUPPORT {support_id}] story={story_id} kind={support.evidence_kind} "
                    f"framing={_support_framing(support)}",
                    f"cited_by={', '.join(dict.fromkeys(references))}",
                    " ".join(temporal_fields),
                    f"fact={fact}",
                )
            )
        )
        rendered_support_ids.add(support_id)

    lines.append(
        "\nCITED PROJECTED EVIDENCE (each support appears once; preserve its attribution and time):"
    )
    lines.extend(evidence_blocks)

    from src.publication.article_quote_allowlist import build_article_quote_allowlist

    excluded_quote_support_ids = set(support_by_id) - rendered_support_ids
    quote_allowlist = build_article_quote_allowlist(
        context,
        excluded_support_ids=excluded_quote_support_ids,
        excluded_story_ids=omitted_story_ids,
        candidate_text_by_support_id=material_projection.text_by_support_id,
    )
    if quote_allowlist:
        lines.append(
            "\nQUOTE ALLOWLIST (only these exact primary-source phrases may appear in quotation marks):"
        )
        lines.extend(f"- «{quote}" for quote in quote_allowlist)
    else:
        lines.append("\nQUOTE ALLOWLIST: none; use indirect speech only.")

    rendered = "\n".join(part for part in (*blocks, "\n".join(lines)) if part).strip()
    if len(rendered) > ARTICLE_WRITER_CONTEXT_MAX_CHARS:
        raise ValueError(
            "article editorial brief writer context exceeds writer context budget "
            f"({len(rendered)}/{ARTICLE_WRITER_CONTEXT_MAX_CHARS} characters)"
        )

    brief_story_ids = {story_id for line in visible_lines for story_id in line.story_ids}
    stats = ArticleWriterMaterializationStats(
        coverage_story_count=len(brief_story_ids),
        story_packet_count=0,
        packets_with_citable_support=0,
        citable_support_count=len(rendered_support_ids),
        bundle_count=len(visible_lines),
        narrative_line_count=len(visible_lines),
        composition_group_count=0,
        group_size_distribution=tuple(
            sorted(Counter(len(line.story_ids) for line in visible_lines).items())
        ),
        rendered_packet_representation="validated brief",
        materialization_mode="brief",
    )
    return rendered, stats
