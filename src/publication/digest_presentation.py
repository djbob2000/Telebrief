"""Digest presentation planning for layered city-life short-read digests."""

from __future__ import annotations

import datetime as dt
import re
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
DigestPresentationUnitKind = Literal["SYNTHESIS", "NORMAL", "BRIEF_ROLLUP"]


@dataclass(frozen=True)
class DigestPresentationUnit:
    """Deterministic presentation compression unit grouping related stories into scan-first items."""

    unit_id: str
    rubric_id: str
    kind: DigestPresentationUnitKind
    story_ids: tuple[str, ...]
    support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...]
    min_rank: int
    compression_key: str


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
    from src.processing.operational_semantics import sanitize_operational_detail

    clean_detail = sanitize_operational_detail(item.detail)
    detail = clean_detail if clean_detail else item.detail.strip()
    if not clean_detail and re.search(r"(\?|спрашива|интересу)", item.detail, flags=re.IGNORECASE):
        return ""

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

_CITY_SITUATION_SUBJECT_ALIASES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "water",
        frozenset(
            {
                "water",
                "water_supply",
                "water_network",
                "vodosnabzhenie",
                "voda",
                "водоснабжение",
                "вода",
            }
        ),
    ),
    (
        "electricity",
        frozenset(
            {
                "electricity",
                "power",
                "power_supply",
                "electricity_supply",
                "electrosupply",
                "электроснабжение",
                "электричество",
                "свет",
            }
        ),
    ),
    (
        "gas",
        frozenset(
            {
                "gas",
                "gas_supply",
                "газоснабжение",
                "газ",
            }
        ),
    ),
    (
        "heating",
        frozenset(
            {
                "heating",
                "heat_supply",
                "district_heating",
                "отопление",
                "теплоснабжение",
            }
        ),
    ),
    (
        "connectivity",
        frozenset(
            {
                "connectivity",
                "mobile_connection",
                "mobile_network",
                "telecom",
                "mobile_internet",
                "связь",
                "интернет",
                "мобильная_связь",
            }
        ),
    ),
    (
        "urban_transport",
        frozenset(
            {
                "urban_transport",
                "city_transport",
                "городской_транспорт",
                "городской_автобус",
                "городские_автобусы",
                "трамвай",
                "трамваи",
                "троллейбус",
                "троллейбусы",
                "маршрутка",
                "городская_маршрутка",
            }
        ),
    ),
)


def _canonical_city_situation_subject(item: CitySituationItem) -> str | None:
    combined_text = f"{item.subject_label} {item.detail}"
    # Reject retail product sales (e.g. bottled water, 3 rub/l) from City Situation dashboard
    if re.search(r"\b(?:розлив|розничн|руб/л|₽/л|3\s*₽/литр)\b", combined_text, re.IGNORECASE):
        return None
    # Reject long-distance/intercity transport from City Situation dashboard (placed in narrative mobility)
    if re.search(
        r"\b(?:междугородн|межгород|ростов|тбилиси|москва|симферополь|донецк|луганск|таганрог)\b",
        combined_text,
        re.IGNORECASE,
    ):
        return None

    key = _norm_key(item.subject_key).replace(" ", "_")
    label = _norm_key(item.subject_label).replace(" ", "_")
    for root, aliases in _CITY_SITUATION_SUBJECT_ALIASES:
        if key in aliases or label in aliases:
            return root
    return None


def _city_situation_group_id(item: CitySituationItem) -> str | None:
    root = _canonical_city_situation_subject(item)
    return f"situation:{root}" if root is not None else None


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

    # Group all items by canonical subject
    grouped: dict[str, list[CitySituationItem]] = {}
    for item in rollup.items:
        key = _canonical_city_situation_subject(item)
        if key is None:
            continue
        grouped.setdefault(key, []).append(item)

    candidate_groups: list[
        tuple[
            CitySituationPresentationGroup,
            int,  # worst severity
            dt.datetime,  # latest ts
            int,  # observation count
        ]
    ] = []

    for canonical_subj, group_items in grouped.items():
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

        group_id = f"situation:{canonical_subj}"
        presentation_group = CitySituationPresentationGroup(
            group_id=group_id,
            group_kind="subject_status",
            subject_key=canonical_subj,
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


def city_situation_group_reader_text(group: CitySituationPresentationGroup) -> str:
    body = "; ".join(line.strip() for line in group.detail_lines if line.strip())
    return f"{group.subject_label}: {body}" if body else group.subject_label


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
        body = "; ".join(line.strip() for line in group.detail_lines if line.strip())
        if body:
            lines.append(f"• {prefix}**{group.subject_label}**: {body}")
        else:
            lines.append(f"• {prefix}**{group.subject_label}**")
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

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "story_ids": list(self.story_ids),
            "stories": [
                {
                    "story_id": p.story_id,
                    "mode": p.mode,
                    "city_situation_group_ids": list(p.city_situation_group_ids),
                    "detail_support_ids": list(p.detail_support_ids),
                    "merge_group_id": p.merge_group_id,
                }
                for p in self.story_presentations
            ],
            "city_situation_groups": [
                {
                    "group_id": g.group_id,
                    "covered_story_ids": list(g.covered_story_ids),
                    "cited_support_ids": list(g.cited_support_ids),
                }
                for g in (self.city_situation.groups if self.city_situation else ())
            ],
        }


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


_DETAIL_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_DETAIL_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "have",
        "has",
        "had",
        "been",
        "still",
        "also",
        "were",
        "will",
        "would",
        "there",
        "their",
        "they",
        "about",
        "which",
        "city",
        "resident",
        "residents",
        "report",
        "reports",
        "reported",
        "message",
        "messages",
        "город",
        "города",
        "городе",
        "житель",
        "жители",
        "жителей",
        "жителям",
        "сообщают",
        "сообщает",
        "сообщению",
        "сообщения",
        "также",
        "тоже",
        "было",
        "были",
        "будет",
        "будут",
        "есть",
        "нет",
        "информация",
        "информации",
    }
)


def _digest_detail_tokens(text: str) -> set[str]:
    from src.publication.article_claims import normalize_support_text

    normalized = normalize_support_text(text)
    return {
        token
        for token in _DETAIL_TOKEN_RE.findall(normalized)
        if len(token) >= 4 and token not in _DETAIL_STOPWORDS
    }


def _is_material_digest_detail(evi: Any, dashboard_texts: Sequence[str]) -> bool:
    from src.publication.article_claims import extract_concrete_claims, normalize_support_text

    text = " ".join(
        part for part in (getattr(evi, "text", ""), getattr(evi, "source_text", "")) if part
    ).strip()
    if not text:
        return False

    dashboard_text = " ".join(dashboard_texts)
    dashboard_normalized = normalize_support_text(dashboard_text)

    concrete_claims = [
        claim
        for claim in extract_concrete_claims(text)
        if claim.kind != "phone" and normalize_support_text(claim.raw) not in dashboard_normalized
    ]
    if concrete_claims:
        return True

    detail_tokens = _digest_detail_tokens(text)
    dashboard_tokens = _digest_detail_tokens(dashboard_text)
    novel_tokens = detail_tokens - dashboard_tokens
    return len(novel_tokens) >= 3


_GENERIC_STOP_TAGS = frozenset(
    {
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
        "сообщество",
        "news",
        "city",
        "resident",
        "local",
        "info",
        # Administrative & utility generic terms
        "жкх",
        "коммуналка",
        "коммунальные_услуги",
        "коммунальные",
        "коммунальный",
        "услуги",
        "служба",
        "службы",
        "благоустройство",
        "отключение",
        "отключения",
        "авария",
        "перебои",
        "график",
        "жалоба",
        "жалобы",
        "жалоба_жителей",
        "жалобы_жителей",
        "обращение",
        "заявка",
        "заявки",
        "сервис",
        "телефон",
        "номер",
        "адрес",
        "ремонт",
        "работы",
        "ситуация",
        "состояние",
        # City and service family terms (must never act as specific merge tags)
        "бердянск",
        "бердянске",
        "бердянска",
        "бердянский",
        "водоснабжение",
        "электроснабжение",
        "электричество",
        "свет",
        "вода",
        "газ",
        "отопление",
        "связь",
        "интернет",
        "транспорт",
        "инфраструктура",
    }
)


_EDITION_LEVEL_AREAS = frozenset(
    {
        "бердянск",
        "бердянский",
        "бердянске",
        "бердянска",
        "город",
        "городской",
        "в городе",
        "центр города",
        "район города",
    }
)


def _compute_batch_frequent_tags(cards: Sequence[Any], threshold: float = 0.30) -> set[str]:
    """Detect dataset-wide / city-wide tags dynamically without hardcoding city names."""
    from collections import Counter

    if len(cards) < 8:
        return set()
    tag_rubrics: dict[str, set[str]] = {}
    tag_counts: Counter[str] = Counter()
    for c in cards:
        rid = getattr(c, "rubric_id", "") or ""
        seen_in_card: set[str] = set()
        for t in getattr(c, "tags", []) or []:
            norm = " ".join(str(t).casefold().split())
            if norm and norm not in seen_in_card:
                seen_in_card.add(norm)
                tag_counts[norm] += 1
                tag_rubrics.setdefault(norm, set()).add(rid)

    cutoff = max(5, int(len(cards) * threshold))
    # A generic dataset-wide tag appears in at least 3 distinct rubrics AND exceeds frequency threshold
    return {
        tag
        for tag, count in tag_counts.items()
        if count >= cutoff and len(tag_rubrics.get(tag, set())) >= 3
    }


def _card_specific_tags(card: Any, batch_stop_tags: set[str] | None = None) -> set[str]:
    tags: set[str] = set()
    raw_tags = getattr(card, "tags", []) or []
    for t in raw_tags:
        norm = " ".join(str(t).casefold().split())
        if (
            norm
            and norm not in _GENERIC_STOP_TAGS
            and (batch_stop_tags is None or norm not in batch_stop_tags)
            and len(norm) > 2
        ):
            tags.add(norm)
    return tags


def _card_areas(card: Any) -> set[str]:
    areas: set[str] = set()
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            for a in getattr(elem, "areas", []) or []:
                norm = " ".join(str(a).casefold().split())
                if norm and norm not in _EDITION_LEVEL_AREAS:
                    areas.add(norm)
    return areas


def _card_source_lineage(card: Any) -> set[str]:
    refs: set[str] = set()
    all_refs_fn = getattr(card, "all_source_refs", None)
    raw_refs: list[Any] = []
    if callable(all_refs_fn):
        raw_refs.extend(all_refs_fn())
    else:
        raw_refs.extend(getattr(card, "representative_source_refs", []) or [])
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            raw_refs.extend(getattr(elem, "source_refs", []) or [])

    for r in raw_refs:
        s = str(r).strip()
        # Require specific numeric ID or post reference, not coarse channel name
        if s and any(c.isdigit() for c in s):
            refs.add(s)
    return refs


def _card_service_families(card: Any) -> frozenset[str]:
    from src.domain.service_taxonomy import detect_service_families

    text_parts = [
        getattr(card, "topic", "") or "",
        getattr(card, "summary", "") or "",
        " ".join(getattr(card, "tags", []) or []),
    ]
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            text_parts.append(getattr(elem, "text", "") or "")
    return detect_service_families(" ".join(text_parts))


def _detect_presentation_kind(card: Any) -> str:
    """Detect presentation kind: status, schedule, repair, damage, workaround, official_position, incident, other."""
    text_parts = [
        getattr(card, "topic", "") or "",
        getattr(card, "summary", "") or "",
        " ".join(getattr(card, "tags", []) or []),
    ]
    for elem_list in (
        getattr(card, "hard_facts", []) or [],
        getattr(card, "community_observations", []) or [],
        getattr(card, "useful_details", []) or [],
    ):
        for elem in elem_list:
            text_parts.append(getattr(elem, "text", "") or "")
    text = " ".join(text_parts).casefold()

    if re.search(
        r"\b(?:накопительн\w* бак|генератор|аккумулятор|павербанк|своими силами|частник|установка бак|альтернативн)\b",
        text,
    ):
        return "workaround"
    if re.search(r"\b(?:график|расписани|режим работы|по часам|веерн)\b", text):
        return "schedule"
    if re.search(
        r"\b(?:ремонт|восстановлен|бригад|аварийн\w* работ|водоканал проводит|чинят|устраняют)\b",
        text,
    ):
        return "repair"
    if re.search(
        r"\b(?:прилет|обстрел|поврежден|разрушен|взрыв|осколк|порыв|прорыв трубы|обрыв)\b",
        text,
    ):
        return "damage"
    if re.search(
        r"\b(?:заявил|сообщил|пообещал|администрация|власти|мэр|глава|официальн)\b",
        text,
    ):
        return "official_position"
    if re.search(r"\b(?:дтп|пожар|чп|несчастный случай)\b", text):
        return "incident"
    if re.search(
        r"\b(?:нет света|нет воды|света нет|воды нет|дали свет|дали воду|включили|отключили|появился|пропал|отсутствует|давление|есть вода|вода есть|свет есть)\b",
        text,
    ):
        return "status"
    return "other"


def _are_cards_merge_compatible(
    card_a: Any,
    card_b: Any,
    batch_stop_tags: set[str] | None = None,
) -> bool:
    """Determine if two cards in the same rubric are pairwise compatible for synthesis merging."""
    fams_a = _card_service_families(card_a)
    fams_b = _card_service_families(card_b)
    kind_a = _detect_presentation_kind(card_a)
    kind_b = _detect_presentation_kind(card_b)

    is_op_a = bool(fams_a) or getattr(card_a, "story_kind", "") == "operational_status"
    is_op_b = bool(fams_b) or getattr(card_b, "story_kind", "") == "operational_status"

    shared_lineage = bool(_card_source_lineage(card_a) & _card_source_lineage(card_b))
    areas_a = _card_areas(card_a)
    areas_b = _card_areas(card_b)
    shared_areas = bool(areas_a & areas_b)
    tags_a = _card_specific_tags(card_a, batch_stop_tags)
    tags_b = _card_specific_tags(card_b, batch_stop_tags)
    shared_tags = bool(tags_a & tags_b)

    # Workarounds (coping, private plumber ads) cannot merge with operational statuses
    if (kind_a == "workaround" or kind_b == "workaround") and kind_a != kind_b:
        return False

    if is_op_a or is_op_b:
        if not (is_op_a and is_op_b):
            return False

        # Multi-family stories rule:
        # Cannot bridge different families. Must have IDENTICAL family set.
        if len(fams_a) > 1 or len(fams_b) > 1:
            if fams_a != fams_b:
                return False
            # Multi-family stories with same families require shared specific area or lineage or tags
            return (kind_a == kind_b) and (shared_areas or shared_lineage or shared_tags)

        # Mono-family stories rule:
        if fams_a != fams_b:
            return False

        # If both are same kind (e.g. status + status):
        if kind_a == kind_b:
            return True

        # Cross-kind merge (e.g. status + repair or status + schedule):
        # Requires shared specific micro-area or shared entity relation (shared specific tag or lineage)
        if {kind_a, kind_b} <= {"status", "repair", "schedule", "other"}:
            return shared_areas or shared_lineage or shared_tags

        return False

    # Non-operational stories:
    # >= 2 shared specific tags OR shared strong evidence/source lineage OR (1 shared tag AND shared area)
    shared_specific_count = len(tags_a & tags_b)
    return (
        shared_specific_count >= 2
        or shared_lineage
        or (shared_specific_count >= 1 and shared_areas)
    )


def _compute_merge_groups(cards: Sequence[Any]) -> dict[str, str]:
    """Group cards in the same rubric using complete-link (clique) clustering with max group size 6."""
    if not cards:
        return {}

    batch_stop_tags = _compute_batch_frequent_tags(cards)

    # Group by rubric_id
    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or ""
        by_rubric.setdefault(rid, []).append(c)

    merge_group_by_id: dict[str, str] = {}
    for _rid, r_cards in by_rubric.items():
        groups: list[list[Any]] = []
        for card in r_cards:
            placed = False
            for g in groups:
                # Complete-link: must be compatible with EVERY card in the group
                # and group size is capped at 6
                if len(g) < 6 and all(
                    _are_cards_merge_compatible(card, member, batch_stop_tags) for member in g
                ):
                    g.append(card)
                    placed = True
                    break
            if not placed:
                groups.append([card])

        for g in groups:
            if len(g) > 1:
                gid = f"merge:{min(c.id for c in g)}"
                for c in g:
                    merge_group_by_id[c.id] = gid
            else:
                c = g[0]
                merge_group_by_id[c.id] = c.id

    return merge_group_by_id


def _card_allowed_supports(card: Any) -> tuple[str, ...]:
    sups: list[str] = []
    if getattr(card, "summary", ""):
        sups.append(f"{card.id}:summary")
    for hf in getattr(card, "hard_facts", []) or []:
        for r in getattr(hf, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for co in getattr(card, "community_observations", []) or []:
        for r in getattr(co, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for ud in getattr(card, "useful_details", []) or []:
        for r in getattr(ud, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    for obs in getattr(card, "operational_observations", []) or []:
        for r in getattr(obs, "source_refs", []) or []:
            if r not in sups:
                sups.append(r)
    return tuple(sups)


def _canonical_service_family(card: Any) -> str | None:
    cat = (getattr(card, "category", "") or "").casefold()
    topic = (getattr(card, "topic", "") or "").casefold()
    tags = {str(t).casefold() for t in getattr(card, "tags", []) or []}
    tokens = set(re.findall(r"[a-zа-яё0-9]+", f"{cat} {topic} {' '.join(tags)}"))

    if {
        "electricity",
        "power",
        "blackout",
        "свет",
        "электроснабжение",
        "электроэнергия",
        "подстанция",
        "рэс",
    } & tokens:
        if not any(w in topic for w in ("услуги электрика", "электрик на дом", "частный электрик")):
            return "electricity"
    if {"water", "водоснабжение", "вода", "водоканал", "порыв"} & tokens:
        if not any(w in topic for w in ("услуги сантехника", "сантехник", "баки")):
            return "water"
    if {"gas", "газ", "газоснабжение", "горгаз"} & tokens:
        return "gas"
    if {"heating", "отопление", "теплосеть"} & tokens:
        return "heating"
    if {"telecom", "connectivity", "связь", "интернет", "провайдер", "мобильная связь"} & tokens:
        return "connectivity"
    if {"transport", "транспорт", "автобус", "маршрутка", "перевозки"} & tokens:
        return "transport"
    return None


def build_digest_presentation_units(
    cards: Sequence[Any],
    presentation_plan: Any = None,
    *,
    max_synthesis_size: int = 24,
    max_normal_size: int = 8,
    max_brief_size: int = 6,
) -> tuple[DigestPresentationUnit, ...]:
    """Partition all detail story cards into deterministic presentation compression units."""
    if not cards:
        return ()

    presentations_by_id = {}
    if presentation_plan is not None and getattr(presentation_plan, "story_presentations", None):
        presentations_by_id = {p.story_id: p for p in presentation_plan.story_presentations}
    elif presentation_plan is not None and getattr(presentation_plan, "story_hints", None):
        presentations_by_id = {h.story_id: h for h in presentation_plan.story_hints}

    fallback_merge_groups: dict[str, str] = {}
    if not presentations_by_id and cards:
        fallback_merge_groups = _compute_merge_groups(cards)

    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or "other"
        by_rubric.setdefault(rid, []).append(c)

    units: list[DigestPresentationUnit] = []
    unit_counter = 0

    for rid, r_cards in by_rubric.items():
        # Partition cards by group key preserving first appearance order
        groups_by_key: dict[str, list[Any]] = {}
        for c in r_cards:
            if c.id in presentations_by_id:
                gid = presentations_by_id[c.id].merge_group_id
            else:
                fam = _canonical_service_family(c)
                if fam:
                    gid = f"service:{fam}"
                else:
                    gid = fallback_merge_groups.get(c.id, c.id)
            groups_by_key.setdefault(gid, []).append(c)

        for gid, g_cards in groups_by_key.items():
            is_service = gid.startswith("service:")
            max_size = max_synthesis_size if is_service else 6
            kind: DigestPresentationUnitKind = "SYNTHESIS" if is_service else "NORMAL"

            for i in range(0, len(g_cards), max_size):
                chunk = g_cards[i : i + max_size]
                unit_counter += 1
                sups_by_story = tuple((c.id, _card_allowed_supports(c)) for c in chunk)
                units.append(
                    DigestPresentationUnit(
                        unit_id=f"unit:{rid}:{unit_counter}",
                        rubric_id=rid,
                        kind=kind,
                        story_ids=tuple(c.id for c in chunk),
                        support_ids_by_story=sups_by_story,
                        min_rank=1,
                        compression_key=f"{rid}:{gid}",
                    )
                )

    return tuple(units)


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
        group_id = _city_situation_group_id(item)
        if group_id is not None:
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
            story_dashboard_texts = [
                city_situation_group_reader_text(group)
                for group in city_plan.groups
                if sid in group.covered_story_ids
            ]
            material_non_dash = [
                evi
                for evi in non_dash_evi
                if _is_material_digest_detail(evi, story_dashboard_texts)
            ]
            scored_material_non_dash = [
                (score_digest_detail_evidence(evi), getattr(evi, "evidence_id", ""))
                for evi in material_non_dash
                if getattr(evi, "evidence_id", "")
            ]
            scored_material_non_dash.sort(key=lambda x: (-x[0], x[1]))
            drilldown_sups = [eid for _, eid in scored_material_non_dash][:2]
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
