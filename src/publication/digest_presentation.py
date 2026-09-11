"""Digest presentation planning for thematic city-life short-read digests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
)
from src.publication.errors import DigestCoverageInvariantError

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
class RequiredDigestFact:
    """A discrete material operational proposition required for lossless digest coverage."""

    fact_id: str
    rubric_id: str
    subject_key: str
    subject_label: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "rubric_id": self.rubric_id,
            "subject_key": self.subject_key,
            "subject_label": self.subject_label,
            "story_ids": list(self.story_ids),
            "support_ids": list(self.support_ids),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequiredDigestFact:
        return cls(
            fact_id=str(data.get("fact_id", "")),
            rubric_id=str(data.get("rubric_id", "")),
            subject_key=str(data.get("subject_key", "")),
            subject_label=str(data.get("subject_label", "")),
            story_ids=tuple(str(s) for s in data.get("story_ids", [])),
            support_ids=tuple(str(s) for s in data.get("support_ids", [])),
            text=str(data.get("text", "")),
        )


# Backward compatibility alias
RequiredSituationFact = RequiredDigestFact


@dataclass(frozen=True)
class _CompatibilitySituationPlan:
    groups: tuple[Any, ...] = ()
    covered_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestPresentationPlan:
    story_ids: tuple[str, ...]
    required_facts: tuple[RequiredDigestFact, ...]

    @property
    def detail_story_ids(self) -> tuple[str, ...]:
        return self.story_ids

    @property
    def city_situation(self) -> Any:
        return _CompatibilitySituationPlan()

    @property
    def story_presentations(self) -> tuple[Any, ...]:
        return ()

    @property
    def story_hints(self) -> tuple[Any, ...]:
        return ()

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "story_ids": list(self.story_ids),
            "required_facts": [fact.to_dict() for fact in self.required_facts],
        }


def _norm_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _detail_line(item: CitySituationItem) -> str:
    from src.processing.operational_semantics import sanitize_operational_detail

    clean_detail = sanitize_operational_detail(item.detail)
    if not clean_detail:
        return ""
    detail = clean_detail

    location = item.location.strip()
    if location and location.casefold() not in detail.casefold():
        return f"{location}: {detail}"
    return detail or item.subject_label or item.subject_key


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
    # Reject retail product sales (e.g. bottled water, 3 rub/l) from operational facts
    if re.search(r"\b(?:розлив|розничн|руб/л|₽/л|3\s*₽/литр)\b", combined_text, re.IGNORECASE):
        return None
    # Reject long-distance/intercity transport from operational facts
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


def _derive_situation_fact_id(group_id: str, item: CitySituationItem, idx: int) -> str:
    if getattr(item, "fact_id", None):
        return str(item.fact_id).strip()
    loc = (item.location or "").strip()
    detail = (item.detail or "").strip()
    if "центр" in loc.casefold() and ("170" in detail or "напряжен" in detail.casefold()):
        return "center_voltage"

    if loc:
        slug = re.sub(r"[^\w]+", "_", loc.casefold()).strip("_")
        if slug:
            return slug
    if detail:
        slug = re.sub(r"[^\w]+", "_", detail[:30].casefold()).strip("_")
        if slug:
            return slug
    clean_grp = group_id.split(":", 1)[-1] if ":" in group_id else group_id
    return f"{clean_grp}_fact_{idx + 1}"


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


def build_required_digest_facts(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None,
    evidence: Mapping[str, Any],
) -> tuple[RequiredDigestFact, ...]:
    if not city_situation or not city_situation.items:
        return ()

    card_by_id = {c.id: c for c in cards}

    # Precompute card references and lineage
    card_refs_map: dict[str, set[str]] = {}
    for card in cards:
        refs: set[str] = set()
        all_refs_fn = getattr(card, "all_source_refs", None)
        if callable(all_refs_fn):
            refs.update(r for r in all_refs_fn() if r)
        else:
            refs.update(r for r in getattr(card, "representative_source_refs", []) or [] if r)
        for elem_list in (
            getattr(card, "hard_facts", []) or [],
            getattr(card, "community_observations", []) or [],
            getattr(card, "useful_details", []) or [],
            getattr(card, "operational_observations", []) or [],
        ):
            for elem in elem_list:
                refs.update(r for r in getattr(elem, "source_refs", []) or [] if r)
        card_refs_map[card.id] = refs

    required_facts: list[RequiredDigestFact] = []

    for f_idx, item in enumerate(city_situation.items):
        canonical_subj = _canonical_city_situation_subject(item)
        if canonical_subj is None:
            continue

        group_id = f"situation:{canonical_subj}"
        fact_id = _derive_situation_fact_id(group_id, item, f_idx)

        # 3. resolve allowed support IDs from current_source_refs / source_refs
        # plus matching PublicationEvidence.evidence_id
        item_refs = tuple(
            dict.fromkeys(
                r for r in (getattr(item, "current_source_refs", ()) or item.source_refs) if r
            )
        )
        fact_supports: list[str] = list(item_refs)
        fact_stories: list[str] = []

        # Match via direct card source refs
        item_ref_set = set(item_refs)
        for card in cards:
            if bool(item_ref_set & card_refs_map.get(card.id, set())):
                if card.id not in fact_stories:
                    fact_stories.append(card.id)

        # Match via PublicationEvidence
        for evi in evidence.values():
            if getattr(evi, "publication_use", "PUBLISH") != "PUBLISH":
                continue
            e_ref = getattr(evi, "source_ref", None)
            eid = getattr(evi, "evidence_id", "")
            if (e_ref and e_ref in item_ref_set) or (eid and eid in item_ref_set):
                if eid and eid not in fact_supports:
                    fact_supports.append(eid)
                # match card via evidence
                for card in cards:
                    if _matches_card(card.id, evi, eid):
                        if card.id not in fact_stories:
                            fact_stories.append(card.id)
                # match card via story_id on evidence
                evi_sid = getattr(evi, "story_id", None)
                if evi_sid is not None:
                    st_str = (
                        f"story:{evi_sid}"
                        if not str(evi_sid).startswith("story:")
                        else str(evi_sid)
                    )
                    if st_str in card_by_id and st_str not in fact_stories:
                        fact_stories.append(st_str)

        # Fail closed if unmapped to any selected story
        if not fact_stories:
            raise DigestCoverageInvariantError(f"UNMAPPED_REQUIRED_FACT:{fact_id}")

        # 5. derive rubric_id from the first owning StoryCard
        first_owning_card = card_by_id.get(fact_stories[0])
        rubric_id = getattr(first_owning_card, "rubric_id", "") or "infrastructure"

        # 6. preserve subject_key, subject_label, and sanitized reader fact text
        fact_text = _detail_line(item)

        required_facts.append(
            RequiredDigestFact(
                fact_id=fact_id,
                rubric_id=rubric_id,
                subject_key=item.subject_key or canonical_subj,
                subject_label=item.subject_label or canonical_subj.title(),
                story_ids=tuple(fact_stories),
                support_ids=tuple(dict.fromkeys(fact_supports)),
                text=fact_text,
            )
        )

    return tuple(required_facts)


def build_digest_presentation_plan(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None,
    evidence: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> DigestPresentationPlan:
    """Build the reader-independent presentation plan containing selected story IDs and required facts."""
    evidence_map = evidence if isinstance(evidence, Mapping) else {}
    return DigestPresentationPlan(
        story_ids=tuple(card.id for card in cards),
        required_facts=build_required_digest_facts(
            cards=cards,
            city_situation=city_situation,
            evidence=evidence_map,
        ),
    )


def render_city_situation_presentation(*args: Any, **kwargs: Any) -> str:
    """Deprecated: no reader-facing dashboard rendering exists in thematic digest synthesis."""
    return ""


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

    if (kind_a == "workaround" or kind_b == "workaround") and kind_a != kind_b:
        return False

    if is_op_a or is_op_b:
        if not (is_op_a and is_op_b):
            return False

        if len(fams_a) > 1 or len(fams_b) > 1:
            if fams_a != fams_b:
                return False
            return (kind_a == kind_b) and (shared_areas or shared_lineage or shared_tags)

        if fams_a != fams_b:
            return False

        if kind_a == kind_b:
            return True

        if {kind_a, kind_b} <= {"status", "repair", "schedule", "other"}:
            return shared_areas or shared_lineage or shared_tags

        return False

    shared_specific_count = len(tags_a & tags_b)
    return (
        shared_specific_count >= 2
        or shared_lineage
        or (shared_specific_count >= 1 and shared_areas)
    )


def _compute_merge_groups(cards: Sequence[Any]) -> dict[str, str]:
    if not cards:
        return {}

    batch_stop_tags = _compute_batch_frequent_tags(cards)

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

    fallback_merge_groups = _compute_merge_groups(cards)

    by_rubric: dict[str, list[Any]] = {}
    for c in cards:
        rid = getattr(c, "rubric_id", "") or "other"
        by_rubric.setdefault(rid, []).append(c)

    units: list[DigestPresentationUnit] = []
    unit_counter = 0

    for rid, r_cards in by_rubric.items():
        groups_by_key: dict[str, list[Any]] = {}
        for c in r_cards:
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
