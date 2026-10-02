from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    _support_framing,
    article_support_theme_hints,
    article_support_topic_context_lines,
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
ARTICLE_WRITER_CONTEXT_VERSION = "event-article-context-v2-evidence-inventory"
_SUPPORT_FACT_MAX_CHARS = 900
_SUPPORT_SOURCE_MAX_CHARS = 1_800
_SUPPORT_COMPACT_FACT_MAX_CHARS = 360
_QUOTE_ALLOWLIST_MAX_CHARS = 12_000
_ARTICLE_EVIDENCE_BEGIN = "<<<ARTICLE_EVIDENCE_INVENTORY_BEGIN>>>"
_ARTICLE_EVIDENCE_END = "<<<ARTICLE_EVIDENCE_INVENTORY_END>>>"
_ARTICLE_QUOTE_BEGIN = "<<<ARTICLE_QUOTE_ALLOWLIST_BEGIN>>>"
_ARTICLE_QUOTE_END = "<<<ARTICLE_QUOTE_ALLOWLIST_END>>>"
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


def _support_topic_hint_lines(support: ArticleSupport) -> tuple[str, ...]:
    return article_support_topic_context_lines(support)


def _group_topic_hint_lines(supports: Sequence[ArticleSupport]) -> tuple[str, ...]:
    if not supports:
        return ()
    signatures = {
        (
            support.service_subject_hint,
            article_support_theme_hints(support),
            article_support_topic_context_lines(support),
        )
        for support in supports
    }
    if len(signatures) != 1:
        return ()
    return _support_topic_hint_lines(supports[0])


def _support_topic_group_key(support: ArticleSupport) -> tuple[str, str]:
    hint = support.service_subject_hint
    structured_hint = (
        "none"
        if hint is None
        else json.dumps(
            (hint.subject_key, hint.subject_label, hint.family),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    derived_hints = json.dumps(
        article_support_theme_hints(support),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return structured_hint, derived_hints


def _support_story_id(support_id: str) -> str:
    match = _SUPPORT_STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""


def _render_coverage_plan(
    plan: ArticleCoveragePlan,
    context: ArticleEditorialContext | None = None,
    material_projection: ArticleMaterialProjection | None = None,
) -> str:
    lines = [
        "ARTICLE COVERAGE MAP (navigation only; exact evidence appears once in the inventory).",
        "Depth controls space, not eligibility. Keep each Story's evidence, place, service and time distinct unless a supported relation is given.",
    ]
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    develop_stories = [
        s for s in plan.stories if s.prominence == "DEVELOP" and s.story_id not in suppressed
    ]
    if develop_stories:
        lines.append(
            "\nDEVELOP lines are the main editorial threads; depth is guidance, not a coverage quota."
        )

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
                lines.append(f"- {a.depth} {a.story_id}: {topic}")
    else:
        for item in plan.stories:
            if item.story_id in suppressed:
                continue
            lines.append(f"- {item.prominence} {item.story_id}: {item.topic}")
    return "\n".join(lines)


def _render_composition_plan(
    plan: ArticleCoveragePlan,
    composition_plan: ArticleCompositionPlan,
    *,
    context: ArticleEditorialContext | None = None,
    material_projection: ArticleMaterialProjection | None = None,
) -> str:
    """Render one compact navigation map; factual evidence lives in the inventory."""
    plan_by_id = plan.by_story_id
    suppressed = set(composition_plan.suppressed_story_ids)
    groups_by_id = {group.group_id: group for group in composition_plan.groups}
    lines = [
        f"ARTICLE COMPOSITION MAP version={composition_plan.version} (navigation, not evidence or ready-made prose).",
        "Depth is editorial prominence, not eligibility. Each visible Story has one canonical group; exact facts and provenance appear once in the evidence inventory.",
        "Line and group order is advisory. Keep each group under its mapped theme. Synthesize across Stories only for a listed non-independent relation; independent groups remain separate.",
        "Keep places and effective times attached to their evidence. Shared headings, ordering, and area labels do not establish proximity, cause, or chronology. Preserve each local contrast and time boundary.",
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
        lines.append(f"\nLINE {narrative_line.line_id} depth={narrative_line.prominence}{heading}")
        lines.append(f"  intent: {_compact_text(narrative_line.narrative_intent, 180)}")
        for group in visible_groups:
            visible_members = [
                member
                for member in group.members
                if member.story_id in plan_by_id and member.story_id not in suppressed
            ]
            lines.append(
                f"  GROUP {group.group_id} relation={group.relation} lead={group.lead_story_id}"
            )
            if group.relation == "independent":
                lines.append(
                    "    Keep this Story distinct; the plan records no supported relation "
                    "to another Story."
                )
            else:
                lines.append(
                    "    Synthesize only the supported relation named above; keep each "
                    "member's facts and support traceable to its packet."
                )
            for member in visible_members:
                item = plan_by_id[member.story_id]
                lines.append(
                    f"    - {member.story_id} depth={member.prominence} "
                    f"topic={_compact_text(item.topic, 120)}"
                )
    return "\n".join(lines)


def expected_article_writer_support_ids(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    *,
    material_projection: ArticleMaterialProjection | None,
    composition_plan: ArticleCompositionPlan | None,
) -> tuple[str, ...]:
    """Derive the eligible writer set from frozen plan/context before rendering."""
    support_by_id = getattr(context, "support_by_id", {})
    coverage_by_story = {item.story_id: item for item in coverage_plan.stories}
    if len(coverage_by_story) != len(coverage_plan.stories):
        raise ValueError("article coverage plan contains duplicate Story IDs")
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    if composition_plan is not None:
        if set(composition_plan.suppressed_story_ids) != suppressed & set(coverage_by_story):
            raise ValueError(
                "article composition suppression does not match the material projection"
            )
        member_story_ids = [
            member.story_id for group in composition_plan.groups for member in group.members
        ]
        expected_visible_story_ids = set(coverage_by_story) - suppressed
        if (
            len(member_story_ids) != len(set(member_story_ids))
            or set(member_story_ids) != expected_visible_story_ids
        ):
            raise ValueError("article composition does not preserve visible Story membership")

    expected: list[str] = []
    owner_by_support_id: dict[str, str] = {}
    for item in coverage_plan.stories:
        if item.story_id in suppressed:
            continue
        for support_id in dict.fromkeys((*item.detail_support_ids, *item.support_ids)):
            support = support_by_id.get(support_id)
            if support is None:
                raise ValueError(f"article coverage plan references missing support {support_id!r}")
            if support.support_id != support_id:
                raise ValueError(
                    f"article support map key {support_id!r} resolves to {support.support_id!r}"
                )
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

            if material_projection is not None:
                action = material_projection.actions_by_support_id.get(support_id)
                if action == "SUPPRESS_PROMOTION_ONLY":
                    continue
                projected_text = material_projection.text_by_support_id.get(support_id, "").strip()
                if action not in {"KEEP", "TRIM_DIRECTORY"}:
                    raise ValueError(
                        f"planned article support {support_id!r} has no projected citable text"
                    )
                if not projected_text:
                    continue
            elif not sanitize_writer_source_text(
                support.text.strip() or support.source_text.strip()
            ):
                raise ValueError(f"planned article support {support_id!r} has no citable text")

            if support_id not in owner_by_support_id:
                owner_by_support_id[support_id] = owner
                expected.append(support_id)

    if composition_plan is not None:
        composition_owner_by_support: dict[str, str] = {}
        for group in composition_plan.groups:
            for member in group.members:
                for support_id in member.support_ids:
                    if support_id in composition_owner_by_support:
                        raise ValueError(
                            f"article support {support_id!r} has multiple composition memberships"
                        )
                    composition_owner_by_support[support_id] = member.story_id
        if set(composition_owner_by_support) != set(expected):
            missing = sorted(set(expected) - set(composition_owner_by_support))
            extra = sorted(set(composition_owner_by_support) - set(expected))
            raise ValueError(
                "article composition support membership differs from the projected writer set "
                f"(missing={missing!r}, extra={extra!r})"
            )
        wrong_owner = {
            support_id: (composition_owner_by_support[support_id], owner)
            for support_id, owner in owner_by_support_id.items()
            if composition_owner_by_support[support_id] != owner
        }
        if wrong_owner:
            raise ValueError(
                f"article composition assigns supports to the wrong Story: {wrong_owner!r}"
            )

    return tuple(expected)


def _render_article_story_packets(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    material_projection: ArticleMaterialProjection | None = None,
    composition_plan: ArticleCompositionPlan | None = None,
) -> tuple[list[dict[str, object]], ArticleWriterMaterializationStats]:
    """Materialize complete, grouped PUBLISH evidence records for the writer."""
    support_by_id = getattr(context, "support_by_id", {})
    coverage_by_story = {item.story_id: item for item in coverage_plan.stories}
    suppressed = set(material_projection.suppressed_story_ids) if material_projection else set()
    if composition_plan is not None:
        composition_suppressed = set(composition_plan.suppressed_story_ids)
        if composition_suppressed != (suppressed & set(coverage_by_story)):
            raise ValueError(
                "article composition suppression does not match the material projection"
            )

    def projected_fact(support: ArticleSupport) -> str:
        if material_projection is not None:
            action = material_projection.actions_by_support_id.get(support.support_id)
            text = material_projection.text_by_support_id.get(support.support_id, "").strip()
            if action == "SUPPRESS_PROMOTION_ONLY":
                return ""
            if action not in {"KEEP", "TRIM_DIRECTORY"}:
                raise ValueError(
                    f"planned article support {support.support_id!r} has no projected citable text"
                )
            return text
        return sanitize_writer_source_text(support.text.strip() or support.source_text.strip())

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
                fact_text = projected_fact(support)
                if not fact_text and material_projection is None:
                    raise ValueError(f"planned article support {support_id!r} has no citable text")
                if not fact_text:
                    continue
                owner_support_ids.append(support_id)

    composition_membership: dict[str, tuple[str, str]] = {}
    if composition_plan is not None:
        for group in composition_plan.groups:
            for member in group.members:
                for support_id in member.support_ids:
                    if support_id in composition_membership:
                        raise ValueError(
                            f"article support {support_id!r} has multiple composition memberships"
                        )
                    composition_membership[support_id] = (
                        member.story_id,
                        group.group_id,
                    )
        expected_ids = {
            support_id
            for support_ids in planned_supports_by_owner.values()
            for support_id in support_ids
        }
        if set(composition_membership) != expected_ids:
            missing = sorted(expected_ids - set(composition_membership))
            extra = sorted(set(composition_membership) - expected_ids)
            raise ValueError(
                "article composition support membership differs from the projected writer set "
                f"(missing={missing!r}, extra={extra!r})"
            )
        for support_id, owner in (
            (support_id, owner)
            for owner, support_ids in planned_supports_by_owner.items()
            for support_id in support_ids
        ):
            member_owner, _group_id = composition_membership[support_id]
            if member_owner != owner:
                raise ValueError(
                    f"article support {support_id!r} is assigned to composition Story "
                    f"{member_owner!r}, expected {owner!r}"
                )

    evidence_records: list[dict[str, object]] = []
    packet_story_ids: list[str] = []
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
        selected_ids = planned_supports_by_owner.get(item.story_id, [])
        selected_supports = [support_by_id[sid] for sid in selected_ids]
        if selected_supports:
            packets_with_citable_support += 1
            citable_support_count += len(selected_supports)

        composition_group = group_by_story_id.get(item.story_id)
        packet_story_ids.append(item.story_id)

        # Collapse only fully equivalent records within one canonical Story.
        # Keep every support ID and its own provenance in the combined record.
        records_by_key: dict[str, dict[str, object]] = {}
        for support in selected_supports:
            fact_text = projected_fact(support)
            source_text = (
                ""
                if material_projection is not None
                else sanitize_writer_source_text(support.source_text.strip())
            )
            if not fact_text:
                fact_text, source_text = source_text, ""
            if " ".join(fact_text.split()) == " ".join(source_text.split()):
                source_text = ""
            framing = _support_framing(support)
            temporal_fields: dict[str, str] = {}
            for field_name, value in (
                ("observed_at", support.observed_at),
                ("effective_from", support.effective_from),
                ("effective_until", support.effective_until),
            ):
                formatted = format_article_context_time(value, context.edition_timezone)
                if formatted is not None:
                    temporal_fields[field_name] = formatted
            parent_context = sanitize_writer_source_text(support.reply_parent_context_text.strip())
            group_id = composition_group.group_id if composition_group is not None else ""
            line_id = composition_group.narrative_line_id if composition_group is not None else ""
            base_record: dict[str, object] = {
                "story_id": item.story_id,
                "group_id": group_id,
                "narrative_line_id": line_id,
                "publication_use": support.publication_use,
                "support_kind": support.support_kind,
                "evidence_kind": support.evidence_kind,
                "source_roles": list(support.source_roles),
                "framing": framing,
                "temporal_role": support.temporal_role,
                "times": temporal_fields,
                "navigation": list(_support_topic_hint_lines(support)),
                "fact": fact_text,
                "primary_source": source_text or None,
                "reply_parent_context": parent_context or None,
            }
            key = json.dumps(base_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            record = records_by_key.get(key)
            if record is None:
                record = {
                    "record_type": "support",
                    **base_record,
                    "support_ids": [],
                    "provenance_by_support_id": [],
                }
                records_by_key[key] = record
            support_ids = record["support_ids"]
            provenance_rows = record["provenance_by_support_id"]
            if not isinstance(support_ids, list) or not isinstance(provenance_rows, list):
                raise TypeError("article evidence record lost mutable aggregation fields")
            support_ids.append(support.support_id)
            provenance_rows.append(
                {
                    "support_id": support.support_id,
                    "source_refs": list(support.source_refs),
                    "fragment_ids": list(support.fragment_ids),
                    "source_item_ids": list(support.source_item_ids),
                }
            )
        evidence_records.extend(records_by_key.values())

    if composition_plan is not None:
        composition_order: dict[str, int] = {}
        groups_by_id = {group.group_id: group for group in composition_plan.groups}
        for narrative_line in composition_plan.narrative_lines:
            for group_id in narrative_line.group_ids:
                resolved_group = groups_by_id.get(group_id)
                if resolved_group is None:
                    continue
                for member in resolved_group.members:
                    if (
                        member.story_id in packet_story_ids
                        and member.story_id not in composition_order
                    ):
                        composition_order[member.story_id] = len(composition_order)
        evidence_records.sort(
            key=lambda record: composition_order.get(
                str(record["story_id"]), len(composition_order)
            )
        )

    return (
        evidence_records,
        ArticleWriterMaterializationStats(
            coverage_story_count=len(coverage_plan.stories),
            story_packet_count=len(packet_story_ids),
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
    evidence_records: Sequence[dict[str, object]],
) -> str:
    """Fit the complete evidence inventory or fail without a partial dossier."""
    record_lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        for record in evidence_records
    ]
    evidence_block = "\n".join((_ARTICLE_EVIDENCE_BEGIN, *record_lines, _ARTICLE_EVIDENCE_END))
    rendered = "\n\n".join(part for part in (prefix, evidence_block) if part).strip()
    if len(rendered) > ARTICLE_WRITER_CONTEXT_MAX_CHARS:
        raise ValueError(
            "article writer evidence dossier exceeds writer context budget "
            f"({len(rendered)}/{ARTICLE_WRITER_CONTEXT_MAX_CHARS} characters)"
        )
    return rendered


def _render_packet_quote_allowlist(
    context: ArticleEditorialContext,
    exposed_support_ids: set[str],
    material_projection: ArticleMaterialProjection | None,
) -> tuple[tuple[str, ...], str]:
    """Render whole quote candidates from evidence exposed in the inventory."""
    from src.publication.article_quote_allowlist import build_article_quote_allowlist

    candidates = build_article_quote_allowlist(
        context,
        excluded_support_ids=set(context.support_by_id) - exposed_support_ids,
        excluded_story_ids=(
            material_projection.suppressed_story_ids if material_projection is not None else ()
        ),
        candidate_text_by_support_id=(
            material_projection.text_by_support_id if material_projection is not None else None
        ),
    )
    lines = [
        _ARTICLE_QUOTE_BEGIN,
        "Each following JSON string is one exact primary-source phrase; escapes encode the original text.",
    ]
    selected: list[str] = []
    budget = _QUOTE_ALLOWLIST_MAX_CHARS - sum(len(line) for line in lines) - len(_ARTICLE_QUOTE_END)
    for candidate in candidates:
        line = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).replace(
            "<", "\\u003c"
        )
        if len(line) + 1 > budget:
            break
        lines.append(line)
        selected.append(candidate)
        budget -= len(line) + 1
    if not selected:
        lines.append('"(none; use indirect speech only)"')
    lines.append(_ARTICLE_QUOTE_END)
    return tuple(selected), "\n".join(lines)


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
    blocks.append(
        "REPLY-PARENT CONTEXT RULE: Separately labeled reply-parent context may identify only "
        "the subject or location of its linked reply. It is not an answer or a service-status fact; "
        "ground availability and other claims only in the reply's cited fact/source."
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
        if material_projection is not None:
            from src.publication.article_geography import (
                build_article_story_geography_map,
                resolve_article_place_resolver,
            )

            geography_by_story = build_article_story_geography_map(
                context=context,
                coverage_plan=coverage_plan,
                material_projection=material_projection,
                resolver=resolve_article_place_resolver(context),
            )
            geography_lines = [
                "STORY GEOGRAPHY INDEX",
                "Only list areas and places supported by projected, citable material; an unresolved location is intentionally absent.",
            ]
            for story in coverage_plan.stories:
                geography = geography_by_story.get(story.story_id)
                focus = geography.focus if geography is not None else None
                if focus:
                    geography_lines.append(f"- {story.story_id}: {focus}")
            if len(geography_lines) > 2:
                blocks.append("\n".join(geography_lines))

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
    if coverage_plan is not None and materialization_mode == "packetized":
        expected_ids = expected_article_writer_support_ids(
            context,
            coverage_plan,
            material_projection=material_projection,
            composition_plan=composition_plan,
        )
        evidence_records, stats = _render_article_story_packets(
            context, coverage_plan, material_projection, composition_plan
        )
        exposed_support_id_values: list[str] = []
        for record in evidence_records:
            support_ids = record.get("support_ids")
            if not isinstance(support_ids, list):
                raise ValueError("article writer evidence record has invalid support IDs")
            exposed_support_id_values.extend(
                support_id for support_id in support_ids if isinstance(support_id, str)
            )
        exposed_ids = tuple(exposed_support_id_values)
        if len(exposed_ids) != len(set(exposed_ids)) or set(exposed_ids) != set(expected_ids):
            raise ValueError(
                "article writer inventory does not match expected eligible support IDs "
                f"(expected={len(expected_ids)}, exposed={len(exposed_ids)})"
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
        _quote_allowlist, quote_block = _render_packet_quote_allowlist(
            context, set(exposed_ids), material_projection
        )
        prefix = "\n\n".join(
            part
            for part in ("\n\n".join(blocks).strip(), stats.to_prompt_block(), quote_block)
            if part
        ).strip()
        rendered = _fit_story_packets(prefix, evidence_records)
        return rendered, stats

    from src.publication.article_quote_allowlist import build_article_quote_allowlist

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

    # Several evidence rows often carry the same fact and source text (for
    # example, one fact linked to multiple fragments).  They all remain
    # available in ArticleEditorialContext for traceability, but repeating
    # their prose in the LLM prompt needlessly multiplies token usage.
    grouped_supports: list[list[ArticleSupport]] = []
    groups_by_key: dict[tuple[str, ...], list[ArticleSupport]] = {}
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
            _compact_text(sanitize_writer_source_text(sup.reply_parent_context_text), 240),
            *_support_topic_group_key(sup),
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
        topic_hint_lines = _group_topic_hint_lines(group)
        lines.extend(topic_hint_lines)

        if sup.observed_at:
            lines.append(f"observed_at={sup.observed_at.isoformat()}")
        if sup.effective_from:
            lines.append(f"effective_from={sup.effective_from.isoformat()}")
        if sup.effective_until:
            lines.append(f"effective_until={sup.effective_until.isoformat()}")
        lines.append(f"fact={_compact_text(fact_text, _SUPPORT_FACT_MAX_CHARS)}")
        if source_text:
            lines.append(f"source={_compact_text(source_text, _SUPPORT_SOURCE_MAX_CHARS)}")
        parent_context = _compact_text(
            sanitize_writer_source_text(sup.reply_parent_context_text), 240
        )
        if parent_context:
            lines.append(
                "reply_parent_context (subject/place only; not a status or answer)="
                f"{parent_context}"
            )
        support_blocks.append("\n".join(lines))

        compact_lines = [
            f"[SUPPORT {support_ids}]",
            f"kind={sup.support_kind} publication_use={sup.publication_use}",
            f"evidence_kind={sup.evidence_kind} source_roles={roles}",
            f"framing={_support_framing(sup)}",
        ]
        compact_lines.extend(topic_hint_lines)
        compact_lines.append(f"fact={_compact_text(fact_text, _SUPPORT_COMPACT_FACT_MAX_CHARS)}")
        if parent_context:
            compact_lines.append(
                "reply_parent_context (subject/place only; not a status or answer)="
                + parent_context
            )
        compact_support_blocks.append("\n".join(compact_lines))

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
    *,
    coverage_plan: ArticleCoveragePlan | None = None,
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

    story_geographies = {}
    if coverage_plan is not None:
        from src.publication.article_geography import (
            build_article_story_geography_map,
            resolve_article_place_resolver,
        )

        story_geographies = build_article_story_geography_map(
            context=context,
            coverage_plan=coverage_plan,
            material_projection=material_projection,
            resolver=resolve_article_place_resolver(context),
        )

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
        "Story geography labels are organizational aids; shared area membership does not establish proximity or distance.",
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
                *(
                    (
                        "STORY GEOGRAPHY: "
                        + "; ".join(
                            f"{story_id}: {story_geographies[story_id].focus or 'not resolved'}"
                            for story_id in line.story_ids
                        ),
                    )
                    if story_geographies
                    else ()
                ),
                *(
                    (f"GEOGRAPHIC FOCUS: {line.geographic_area_name}",)
                    if line.geographic_area_name
                    else ()
                ),
                *(
                    (f"NAMED PLACES IN THIS AREA: {', '.join(line.geographic_place_names)}",)
                    if line.geographic_place_names
                    else ()
                ),
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
        evidence_lines = [
            f"[SUPPORT {support_id}] story={story_id} kind={support.evidence_kind} "
            f"framing={_support_framing(support)}",
            f"cited_by={', '.join(dict.fromkeys(references))}",
            " ".join(temporal_fields),
        ]
        evidence_lines.extend(_support_topic_hint_lines(support))
        evidence_lines.append(f"fact={fact}")
        if support.reply_parent_context_text:
            evidence_lines.append(
                "reply_parent_context (subject/place only; not a status or answer)="
                + _compact_text(sanitize_writer_source_text(support.reply_parent_context_text), 240)
            )
        evidence_blocks.append("\n".join(evidence_lines))
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
