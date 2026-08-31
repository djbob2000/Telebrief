"""Digest presentation planning for layered city-life short-read digests."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
    city_situation_icon,
    city_situation_severity,
)

DigestDetailRole = Literal["SUPPRESS", "DRILL_DOWN", "NORMAL"]
DigestPresentationMode = Literal[
    "DASHBOARD_ONLY",
    "DETAIL_ONLY",
    "DASHBOARD_AND_DRILLDOWN",
]


@dataclass(frozen=True)
class CitySituationPresentationGroup:
    group_id: str
    group_kind: str  # "subject_status" | "available_services"
    subject_key: str
    subject_label: str
    state: str
    source_refs: tuple[str, ...]
    detail_lines: tuple[str, ...]
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CitySituationPresentationPlan:
    groups: tuple[CitySituationPresentationGroup, ...]
    covered_source_refs: tuple[str, ...]


def _norm_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _detail_line(item: CitySituationItem) -> str:
    detail = item.detail.strip()
    location = item.location.strip()
    if location and location.casefold() not in detail.casefold():
        return f"{location}: {detail}" if detail else location
    return detail or item.subject_label or item.subject_key


def _positive_detail_line(item: CitySituationItem) -> str:
    label = item.subject_label.strip() or item.subject_key.strip()
    detail = item.detail.strip()
    if detail and label.casefold() in detail.casefold():
        return detail
    if detail:
        return f"{label}: {detail}"
    return label


_POSITIVE_STATES = frozenset({"AVAILABLE", "RESOLVED"})


def _presentation_state(items: Sequence[CitySituationItem]) -> str:
    states = {item.state.upper() for item in items}
    has_positive = bool(states & _POSITIVE_STATES)
    has_non_positive = bool(states - _POSITIVE_STATES)
    if has_positive and has_non_positive:
        return "CONFLICTING"
    return min(items, key=lambda item: city_situation_severity(item.state)).state


def _select_group_details(
    items: Sequence[CitySituationItem],
    *,
    limit: int,
) -> tuple[str, ...]:
    positive = [item for item in items if item.state.upper() in _POSITIVE_STATES]
    non_positive = [item for item in items if item.state.upper() not in _POSITIVE_STATES]
    ordered: list[CitySituationItem] = []
    if positive and non_positive:
        ordered.extend([non_positive[0], positive[0]])
    for item in items:
        if item not in ordered:
            ordered.append(item)
    lines: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        line = _detail_line(item)
        key = line.casefold()
        if line and key not in seen:
            seen.add(key)
            lines.append(line)
        if len(lines) >= limit:
            break
    return tuple(lines)


def plan_city_situation_presentation(
    rollup: CitySituationRollup | None,
    *,
    max_items: int = 7,
    max_details_per_item: int = 2,
    max_positive_items: int = 2,
) -> CitySituationPresentationPlan:
    """Consolidate and cap operational observations into a structured dashboard plan."""
    if not rollup or not rollup.items:
        return CitySituationPresentationPlan(groups=(), covered_source_refs=())

    # Group all items by (subject_key, dimension)
    grouped: dict[tuple[str, str], list[CitySituationItem]] = {}
    for item in rollup.items:
        key = (_norm_key(item.subject_key), _norm_key(item.dimension))
        grouped.setdefault(key, []).append(item)

    candidate_groups: list[
        tuple[
            CitySituationPresentationGroup,
            int,  # worst severity
            dt.datetime,  # latest ts
            int,  # observation count
        ]
    ] = []

    for (norm_subj, norm_dim), group_items in grouped.items():
        pres_state = _presentation_state(group_items)

        first_item = group_items[0]
        subject_label = next(
            (it.subject_label for it in group_items if it.subject_label),
            first_item.subject_key,
        )
        worst_sev = city_situation_severity(pres_state)

        # Merge source refs preserving order / uniqueness
        seen_refs: set[str] = set()
        merged_refs: list[str] = []
        for it in group_items:
            for r in it.source_refs:
                if r and r not in seen_refs:
                    seen_refs.add(r)
                    merged_refs.append(r)

        detail_lines = _select_group_details(group_items, limit=max_details_per_item)
        latest_ts = max(it.last_observed_at for it in group_items)
        obs_count = sum(it.observation_count for it in group_items)

        group_id = f"situation:{norm_subj}:{norm_dim}"
        presentation_group = CitySituationPresentationGroup(
            group_id=group_id,
            group_kind="subject_status",
            subject_key=first_item.subject_key,
            subject_label=subject_label,
            state=pres_state,
            source_refs=tuple(merged_refs),
            detail_lines=detail_lines,
        )
        candidate_groups.append((presentation_group, worst_sev, latest_ts, obs_count))

    positive = [row for row in candidate_groups if row[0].state.upper() in _POSITIVE_STATES]
    non_positive = [row for row in candidate_groups if row[0].state.upper() not in _POSITIVE_STATES]

    positive.sort(
        key=lambda entry: (
            entry[1],
            -entry[2].timestamp(),
            -entry[3],
            entry[0].subject_label.casefold(),
        )
    )
    non_positive.sort(
        key=lambda entry: (
            entry[1],
            -entry[2].timestamp(),
            -entry[3],
            entry[0].subject_label.casefold(),
        )
    )

    reserve_positive = 1 if positive and non_positive and max_items >= 2 else 0
    negative_limit = max_items - reserve_positive

    selected_groups: list[CitySituationPresentationGroup] = [
        row[0] for row in non_positive[:negative_limit]
    ]
    remaining = max_items - len(selected_groups)

    selected_groups.extend(row[0] for row in positive[: min(max_positive_items, remaining)])

    # covered_source_refs = union of source refs of selected groups only
    seen_covered: set[str] = set()
    covered_refs: list[str] = []
    for g in selected_groups:
        for r in g.source_refs:
            if r and r not in seen_covered:
                seen_covered.add(r)
                covered_refs.append(r)

    return CitySituationPresentationPlan(
        groups=tuple(selected_groups),
        covered_source_refs=tuple(covered_refs),
    )


def render_city_situation_presentation(
    plan: CitySituationPresentationPlan | None,
    *,
    use_emojis: bool = True,
) -> str:
    if not plan or not plan.groups:
        return ""
    lines = ["*🏙 Городская обстановка*" if use_emojis else "*Городская обстановка*"]
    for group in plan.groups:
        icon = city_situation_icon(group.state) if use_emojis else ""
        prefix = f"{icon} " if icon else ""
        body = "; ".join(group.detail_lines)
        lines.append(f"• {prefix}**{group.subject_label}**: {body}")
    return "\n".join(lines)


@dataclass(frozen=True)
class DigestStoryPresentation:
    story_id: str
    mode: DigestPresentationMode = "DETAIL_ONLY"
    city_situation_group_ids: tuple[str, ...] = ()
    detail_support_ids: tuple[str, ...] = ()
    merge_group_id: str = ""

    def __init__(
        self,
        story_id: str,
        mode: DigestPresentationMode | None = None,
        city_situation_group_ids: tuple[str, ...] = (),
        detail_support_ids: tuple[str, ...] = (),
        merge_group_id: str = "",
        *,
        detail_role: str | None = None,
    ) -> None:
        if mode is None:
            if detail_role == "SUPPRESS":
                mode = "DASHBOARD_ONLY"
            elif detail_role == "DRILL_DOWN":
                mode = "DASHBOARD_AND_DRILLDOWN"
            else:
                mode = "DETAIL_ONLY"
        object.__setattr__(self, "story_id", str(story_id))
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "city_situation_group_ids", tuple(city_situation_group_ids))
        object.__setattr__(self, "detail_support_ids", tuple(detail_support_ids))
        object.__setattr__(self, "merge_group_id", str(merge_group_id or story_id))

    @property
    def detail_role(self) -> DigestDetailRole:
        if self.mode == "DASHBOARD_ONLY":
            return "SUPPRESS"
        if self.mode == "DASHBOARD_AND_DRILLDOWN":
            return "DRILL_DOWN"
        return "NORMAL"


DigestStoryPresentationHint = DigestStoryPresentation


@dataclass(frozen=True)
class DigestPresentationPlan:
    city_situation: CitySituationPresentationPlan
    story_presentations: tuple[DigestStoryPresentation, ...]

    def __init__(
        self,
        city_situation: CitySituationPresentationPlan,
        story_presentations: tuple[DigestStoryPresentation, ...] | None = None,
        *,
        detail_story_ids: tuple[str, ...] | None = None,
        story_hints: tuple[DigestStoryPresentation, ...] | None = None,
    ) -> None:
        if story_presentations is not None:
            object.__setattr__(self, "story_presentations", tuple(story_presentations))
        elif story_hints is not None:
            object.__setattr__(self, "story_presentations", tuple(story_hints))
        else:
            object.__setattr__(self, "story_presentations", ())
        object.__setattr__(self, "city_situation", city_situation)

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.story_presentations)

    @property
    def detail_story_ids(self) -> tuple[str, ...]:
        detail_modes = {"DETAIL_ONLY", "DASHBOARD_AND_DRILLDOWN"}
        return tuple(
            item.story_id for item in self.story_presentations if item.mode in detail_modes
        )

    @property
    def story_hints(self) -> tuple[DigestStoryPresentation, ...]:
        return self.story_presentations


def score_digest_detail_evidence(evi: Any) -> int:
    """Score evidence for microdetail richness (concrete numbers, dates, times, amounts, quotes)."""
    from src.publication.article_claims import extract_concrete_claims

    text = " ".join(
        part for part in (getattr(evi, "text", ""), getattr(evi, "source_text", "")) if part
    ).strip()
    if not text:
        return 0
    score = 0
    if extract_concrete_claims(text):
        score += 3
    if getattr(evi, "kind", "") in {"community_report", "service_access", "official_statement"}:
        score += 2
    src_text = getattr(evi, "source_text", "") or ""
    if len(src_text.split()) >= 8:
        score += 1
    if any(mark in src_text for mark in ("«", "»", '"')):
        score += 1
    return score


_GENERIC_STOP_TAGS = {
    "город",
    "города",
    "городской",
    "городские",
    "житель",
    "жители",
    "жителей",
    "новость",
    "новости",
    "информация",
    "информации",
    "местный",
    "местные",
    "общество",
    "происшествия",
    "события",
    "news",
    "city",
    "resident",
    "local",
    "info",
}


def _card_meaningful_tags(card: Any) -> set[str]:
    tags: set[str] = set()
    raw_tags = getattr(card, "tags", []) or []
    for t in raw_tags:
        norm = " ".join(str(t).casefold().split())
        if norm and norm not in _GENERIC_STOP_TAGS and len(norm) > 2:
            tags.add(norm)
    return tags


def _compute_merge_groups(cards: Sequence[Any]) -> dict[str, str]:
    """Group cards in the same rubric that share at least one non-generic normalized tag."""
    # Group by rubric_id
    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or ""
        by_rubric.setdefault(rid, []).append(c)

    merge_group_by_id: dict[str, str] = {}
    for _rid, r_cards in by_rubric.items():
        n = len(r_cards)
        # Adjacency list
        adj: dict[int, set[int]] = {i: set() for i in range(n)}
        for i in range(n):
            tags_i = _card_meaningful_tags(r_cards[i])
            if not tags_i:
                continue
            for j in range(i + 1, n):
                tags_j = _card_meaningful_tags(r_cards[j])
                if tags_i & tags_j:
                    adj[i].add(j)
                    adj[j].add(i)

        visited: set[int] = set()
        for i in range(n):
            if i in visited:
                continue
            component: list[int] = []
            queue = [i]
            visited.add(i)
            while queue:
                curr = queue.pop()
                component.append(curr)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            if len(component) > 1:
                comp_cards = [r_cards[idx] for idx in component]
                gid = f"merge:{min(c.id for c in comp_cards)}"
                for c in comp_cards:
                    merge_group_by_id[c.id] = gid
            else:
                card = r_cards[i]
                merge_group_by_id[card.id] = card.id

    return merge_group_by_id


def _dashboard_supports_for_items(
    items: Sequence[CitySituationItem],
    evidence: Mapping[str, Any],
) -> tuple[Any, ...]:
    current_refs = {
        ref
        for item in items
        for ref in (getattr(item, "current_source_refs", ()) or item.source_refs)
        if ref
    }
    return tuple(
        evi
        for evi in evidence.values()
        if getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
        and getattr(evi, "kind", "") in {"service_access", "established_fact", "official_statement"}
        and getattr(evi, "source_ref", None) in current_refs
    )


def _matches_card(card_id: str, evi: Any, eid: str) -> bool:
    evi_sid = getattr(evi, "story_id", None)
    if str(evi_sid) == card_id or f"story:{evi_sid}" == card_id:
        return True
    if eid.startswith(f"{card_id}:"):
        return True
    num_part = card_id.split(":", 1)[1] if card_id.startswith("story:") else None
    if num_part and num_part.isdigit() and evi_sid is not None:
        try:
            if int(evi_sid) == int(num_part):
                return True
        except (ValueError, TypeError):
            pass
    return False


def build_digest_presentation_plan(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None,
    evidence: Any = None,
    max_city_situation_items: int = 7,
    max_details_per_item: int = 2,
    max_positive_items: int = 2,
    max_city_situation_details: int | None = None,
    max_city_situation_positive_items: int | None = None,
) -> DigestPresentationPlan:
    """Build the comprehensive presentation plan for a digest run."""
    from dataclasses import replace

    if max_city_situation_details is not None:
        max_details_per_item = max_city_situation_details
    if max_city_situation_positive_items is not None:
        max_positive_items = max_city_situation_positive_items

    city_plan = plan_city_situation_presentation(
        city_situation,
        max_items=max_city_situation_items,
        max_details_per_item=max_details_per_item,
        max_positive_items=max_positive_items,
    )

    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    # Group rollup items by group_id
    items_by_group_id: dict[str, list[CitySituationItem]] = {}
    for item in city_situation.items if city_situation else ():
        group_id = f"situation:{_norm_key(item.subject_key)}:{_norm_key(item.dimension)}"
        items_by_group_id.setdefault(group_id, []).append(item)

    enriched_groups: list[CitySituationPresentationGroup] = []
    for group in city_plan.groups:
        dashboard_evidence = _dashboard_supports_for_items(
            items_by_group_id.get(group.group_id, []),
            evidence_map,
        )
        cited_support_ids = tuple(
            dict.fromkeys(
                getattr(evi, "evidence_id", "")
                for evi in dashboard_evidence
                if getattr(evi, "evidence_id", "")
            )
        )
        covered_story_ids_set: list[str] = []
        for evi in dashboard_evidence:
            eid = getattr(evi, "evidence_id", "")
            matched = False
            for card in cards:
                if _matches_card(card.id, evi, eid):
                    if card.id not in covered_story_ids_set:
                        covered_story_ids_set.append(card.id)
                    matched = True
            if not matched and getattr(evi, "story_id", None) is not None:
                st_str = (
                    f"story:{evi.story_id}"
                    if not str(evi.story_id).startswith("story:")
                    else str(evi.story_id)
                )
                if st_str not in covered_story_ids_set:
                    covered_story_ids_set.append(st_str)

        enriched_groups.append(
            replace(
                group,
                covered_story_ids=tuple(covered_story_ids_set),
                cited_support_ids=cited_support_ids,
            )
        )

    city_plan = CitySituationPresentationPlan(
        groups=tuple(enriched_groups),
        covered_source_refs=city_plan.covered_source_refs,
    )

    dashboard_groups_by_story: dict[str, list[str]] = {}
    dashboard_supports_by_story: dict[str, set[str]] = {}
    for group in city_plan.groups:
        for sid in group.covered_story_ids:
            dashboard_groups_by_story.setdefault(sid, []).append(group.group_id)
        for supp_id in group.cited_support_ids:
            evi = evidence_map.get(supp_id)
            if evi is not None:
                for card in cards:
                    if _matches_card(card.id, evi, supp_id):
                        dashboard_supports_by_story.setdefault(card.id, set()).add(supp_id)

    card_modes: dict[str, DigestPresentationMode] = {}
    card_detail_supports: dict[str, tuple[str, ...]] = {}
    card_group_ids: dict[str, tuple[str, ...]] = {}

    for card in cards:
        sid = card.id
        group_ids = tuple(dashboard_groups_by_story.get(sid, ()))
        card_group_ids[sid] = group_ids

        if not evidence_map:
            all_refs_fn = getattr(card, "all_source_refs", None)
            if callable(all_refs_fn):
                card_refs = {ref for ref in all_refs_fn() if ref}
            else:
                card_refs = {ref for ref in getattr(card, "representative_source_refs", []) if ref}
            is_operational = getattr(card, "story_kind", "") == "operational_status"
            covered_refs = set(city_plan.covered_source_refs)
            overlaps_dashboard = (
                bool(card_refs)
                and bool(card_refs & covered_refs)
                and (is_operational or (card_refs <= covered_refs))
            )
            if overlaps_dashboard:
                card_modes[sid] = "DASHBOARD_ONLY"
                card_detail_supports[sid] = ()
            else:
                card_modes[sid] = "DETAIL_ONLY"
                card_detail_supports[sid] = ()
            continue

        candidate_evi: list[Any] = []
        for eid, evi in evidence_map.items():
            if _matches_card(sid, evi, eid):
                if (
                    getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                    and getattr(evi, "kind", "") != "resident_question"
                ):
                    candidate_evi.append(evi)

        dash_supp_ids = dashboard_supports_by_story.get(sid, set())

        non_dash_evi = [
            evi for evi in candidate_evi if getattr(evi, "evidence_id", "") not in dash_supp_ids
        ]
        scored_non_dash = [
            (score_digest_detail_evidence(evi), getattr(evi, "evidence_id", ""))
            for evi in non_dash_evi
            if getattr(evi, "evidence_id", "")
        ]
        scored_non_dash.sort(key=lambda x: (-x[0], x[1]))

        if not dash_supp_ids:
            card_modes[sid] = "DETAIL_ONLY"
            pos_sups = [eid for score, eid in scored_non_dash if score > 0][:2]
            if pos_sups:
                card_detail_supports[sid] = tuple(pos_sups)
            elif scored_non_dash:
                card_detail_supports[sid] = (scored_non_dash[0][1],)
            else:
                card_detail_supports[sid] = ()
        else:
            drilldown_sups = [eid for score, eid in scored_non_dash if score > 0][:2]
            if drilldown_sups:
                card_modes[sid] = "DASHBOARD_AND_DRILLDOWN"
                card_detail_supports[sid] = tuple(drilldown_sups)
            else:
                card_modes[sid] = "DASHBOARD_ONLY"
                card_detail_supports[sid] = ()

    detail_cards = [
        card for card in cards if card_modes[card.id] in {"DETAIL_ONLY", "DASHBOARD_AND_DRILLDOWN"}
    ]
    merge_groups = _compute_merge_groups(detail_cards)

    story_presentations: list[DigestStoryPresentation] = []
    for card in cards:
        sid = card.id
        story_presentations.append(
            DigestStoryPresentation(
                story_id=sid,
                mode=card_modes[sid],
                city_situation_group_ids=card_group_ids[sid],
                detail_support_ids=card_detail_supports[sid],
                merge_group_id=merge_groups.get(sid, sid),
            )
        )

    return DigestPresentationPlan(
        city_situation=city_plan,
        story_presentations=tuple(story_presentations),
    )
