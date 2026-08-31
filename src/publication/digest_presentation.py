"""Digest presentation planning for layered city-life short-read digests."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
    city_situation_severity,
)


@dataclass(frozen=True)
class CitySituationPresentationGroup:
    group_id: str
    group_kind: str  # "subject_status" | "available_services"
    subject_key: str
    subject_label: str
    state: str
    source_refs: tuple[str, ...]
    detail_lines: tuple[str, ...]


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


def plan_city_situation_presentation(
    rollup: CitySituationRollup | None,
    *,
    max_items: int = 7,
    max_details_per_item: int = 2,
) -> CitySituationPresentationPlan:
    """Consolidate and cap operational observations into a structured dashboard plan."""
    if not rollup or not rollup.items:
        return CitySituationPresentationPlan(groups=(), covered_source_refs=())

    non_positive_items: list[CitySituationItem] = []
    positive_items: list[CitySituationItem] = []

    for item in rollup.items:
        st_upper = item.state.upper()
        if st_upper in ("AVAILABLE", "RESOLVED"):
            positive_items.append(item)
        else:
            non_positive_items.append(item)

    # Group non-positive items by (subject_key, dimension)
    grouped_non_positive: dict[tuple[str, str], list[CitySituationItem]] = {}
    for item in non_positive_items:
        key = (_norm_key(item.subject_key), _norm_key(item.dimension))
        grouped_non_positive.setdefault(key, []).append(item)

    candidate_groups: list[
        tuple[
            CitySituationPresentationGroup,
            int,  # worst severity
            dt.datetime,  # latest ts
            int,  # observation count
        ]
    ] = []

    for (norm_subj, norm_dim), group_items in grouped_non_positive.items():
        first_item = group_items[0]
        subject_label = next(
            (it.subject_label for it in group_items if it.subject_label),
            first_item.subject_key,
        )

        # Worst state (minimum severity integer)
        worst_item = min(
            group_items,
            key=lambda it: city_situation_severity(it.state),
        )
        worst_state = worst_item.state
        worst_sev = city_situation_severity(worst_state)

        # Merge source refs preserving order / uniqueness
        seen_refs: set[str] = set()
        merged_refs: list[str] = []
        for it in group_items:
            for r in it.source_refs:
                if r and r not in seen_refs:
                    seen_refs.add(r)
                    merged_refs.append(r)

        # Detail lines
        seen_details: set[str] = set()
        detail_lines: list[str] = []
        for it in group_items:
            line = _detail_line(it)
            norm_l = line.casefold()
            if line and norm_l not in seen_details:
                seen_details.add(norm_l)
                detail_lines.append(line)
                if len(detail_lines) >= max_details_per_item:
                    break

        latest_ts = max(it.last_observed_at for it in group_items)
        obs_count = sum(it.observation_count for it in group_items)

        group_id = f"situation:{norm_subj}:{norm_dim}"
        presentation_group = CitySituationPresentationGroup(
            group_id=group_id,
            group_kind="subject_status",
            subject_key=first_item.subject_key,
            subject_label=subject_label,
            state=worst_state,
            source_refs=tuple(merged_refs),
            detail_lines=tuple(detail_lines),
        )
        candidate_groups.append((presentation_group, worst_sev, latest_ts, obs_count))

    # Sort non-positive candidate groups
    candidate_groups.sort(
        key=lambda entry: (
            entry[1],  # severity (1=most severe)
            -entry[2].timestamp(),  # latest observation
            -entry[3],  # observation count
            entry[0].subject_label.casefold(),
        )
    )

    # Prepare positive available_services bundle if positive items exist
    available_group: CitySituationPresentationGroup | None = None
    if positive_items:
        seen_pos_refs: set[str] = set()
        merged_pos_refs: list[str] = []
        for it in positive_items:
            for r in it.source_refs:
                if r and r not in seen_pos_refs:
                    seen_pos_refs.add(r)
                    merged_pos_refs.append(r)

        seen_pos_details: set[str] = set()
        pos_detail_lines: list[str] = []
        for it in positive_items:
            line = _positive_detail_line(it)
            norm_l = line.casefold()
            if line and norm_l not in seen_pos_details:
                seen_pos_details.add(norm_l)
                pos_detail_lines.append(line)
                if len(pos_detail_lines) >= max(max_details_per_item, 4):
                    break

        available_group = CitySituationPresentationGroup(
            group_id="situation:available_services",
            group_kind="available_services",
            subject_key="available_services",
            subject_label="Работающие службы и доступные сервисы",
            state="AVAILABLE",
            source_refs=tuple(merged_pos_refs),
            detail_lines=tuple(pos_detail_lines),
        )

    # Rank and select final groups up to max_items
    selected_groups: list[CitySituationPresentationGroup] = []

    if available_group and max_items >= 2 and candidate_groups:
        # Reserve 1 slot for positive bundle, remaining max_items - 1 for non-positive
        non_pos_limit = max_items - 1
        for grp_tuple in candidate_groups[:non_pos_limit]:
            selected_groups.append(grp_tuple[0])
        selected_groups.append(available_group)
    elif available_group and not candidate_groups:
        selected_groups.append(available_group)
    else:
        for grp_tuple in candidate_groups[:max_items]:
            selected_groups.append(grp_tuple[0])
        if available_group and len(selected_groups) < max_items:
            selected_groups.append(available_group)

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


@dataclass(frozen=True)
class DigestStoryPresentationHint:
    story_id: str
    detail_support_ids: tuple[str, ...]
    merge_group_id: str


@dataclass(frozen=True)
class DigestPresentationPlan:
    city_situation: CitySituationPresentationPlan
    detail_story_ids: tuple[str, ...]
    story_hints: tuple[DigestStoryPresentationHint, ...]


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


def _card_is_consumed_by_dashboard(
    card: Any,
    covered_refs: set[str],
) -> bool:
    if getattr(card, "story_kind", "") != "operational_status":
        return False
    all_refs_fn = getattr(card, "all_source_refs", None)
    if callable(all_refs_fn):
        refs = {ref for ref in all_refs_fn() if ref}
    else:
        refs = {ref for ref in getattr(card, "representative_source_refs", []) if ref}
    return bool(refs) and refs <= covered_refs


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


def build_digest_presentation_plan(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None,
    evidence: Any = None,
    max_city_situation_items: int = 7,
    max_city_situation_details: int = 2,
) -> DigestPresentationPlan:
    """Build the comprehensive presentation plan for a digest run."""
    city_plan = plan_city_situation_presentation(
        city_situation,
        max_items=max_city_situation_items,
        max_details_per_item=max_city_situation_details,
    )
    covered_refs = set(city_plan.covered_source_refs)
    detail_cards = [
        card for card in cards if not _card_is_consumed_by_dashboard(card, covered_refs)
    ]
    detail_story_ids = tuple(card.id for card in detail_cards)

    merge_groups = _compute_merge_groups(detail_cards)

    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    story_hints: list[DigestStoryPresentationHint] = []
    for card in detail_cards:
        sid = card.id
        merge_gid = merge_groups.get(sid, sid)

        # Extract numeric story id if applicable
        num_sid: int | None = None
        if sid.startswith("story:"):
            num_part = sid.split(":", 1)[1]
            if num_part.isdigit():
                num_sid = int(num_part)

        # Find matching evidence
        candidate_evi: list[Any] = []
        for eid, evi in evidence_map.items():
            evi_sid = getattr(evi, "story_id", None)
            if (
                (evi_sid is not None and num_sid is not None and evi_sid == num_sid)
                or eid.startswith(f"{sid}:")
                or getattr(evi, "story_id", None) == sid
            ):
                if (
                    getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                    and getattr(evi, "kind", "") != "resident_question"
                ):
                    candidate_evi.append(evi)

        # Score candidate evidence
        scored_evi = [
            (score_digest_detail_evidence(evi), getattr(evi, "evidence_id", ""))
            for evi in candidate_evi
        ]
        scored_evi.sort(key=lambda x: (-x[0], x[1]))

        positive_supports = [eid for score, eid in scored_evi if score > 0 and eid][:2]
        if positive_supports:
            detail_support_ids = tuple(positive_supports)
        elif scored_evi and scored_evi[0][1]:
            detail_support_ids = (scored_evi[0][1],)
        else:
            detail_support_ids = ()

        story_hints.append(
            DigestStoryPresentationHint(
                story_id=sid,
                detail_support_ids=detail_support_ids,
                merge_group_id=merge_gid,
            )
        )

    return DigestPresentationPlan(
        city_situation=city_plan,
        detail_story_ids=detail_story_ids,
        story_hints=tuple(story_hints),
    )
