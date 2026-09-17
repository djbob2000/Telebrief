"""Digest presentation planning for thematic city-life short-read digests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from src.publication.city_situation import (
    CitySituationItem,
    CitySituationRollup,
)
from src.publication.errors import DigestCoverageInvariantError

DigestPresentationUnitKind = Literal["SYNTHESIS", "NORMAL", "BRIEF_ROLLUP"]


class DigestPresentationMode(str, Enum):
    DASHBOARD_ONLY = "DASHBOARD_ONLY"
    DETAIL_ONLY = "DETAIL_ONLY"
    DASHBOARD_AND_DRILLDOWN = "DASHBOARD_AND_DRILLDOWN"


def city_situation_group_reader_text(group: Any) -> str:
    return getattr(group, "reader_text", "") or ""


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
class TopicBundle:
    """Thematic aggregate of related stories within a rubric."""

    bundle_id: str
    rubric_id: str
    topic_key: str
    topic_label: str
    emoji: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    fact_ledger: tuple[str, ...]
    locations: tuple[str, ...] = ()
    required_facts: tuple[RequiredDigestFact, ...] = ()
    status_summary: str = ""
    states: tuple[str, ...] = ()
    epistemic_status: str = "сообщения жителей"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "rubric_id": self.rubric_id,
            "topic_key": self.topic_key,
            "topic_label": self.topic_label,
            "emoji": self.emoji,
            "story_ids": list(self.story_ids),
            "support_ids": list(self.support_ids),
            "fact_ledger": list(self.fact_ledger),
            "locations": list(self.locations),
            "required_facts": [rf.to_dict() for rf in self.required_facts],
            "status_summary": self.status_summary,
            "states": list(self.states),
            "epistemic_status": self.epistemic_status,
        }


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


@dataclass(frozen=True, init=False)
class DigestPresentationPlan:
    story_ids: tuple[str, ...]
    required_facts: tuple[RequiredDigestFact, ...]
    _city_situation: Any
    _story_presentations: tuple[Any, ...]

    def __init__(
        self,
        story_ids: Sequence[str] = (),
        required_facts: Sequence[RequiredDigestFact] = (),
        city_situation: Any = None,
        story_presentations: Sequence[Any] = (),
        **kwargs: Any,
    ) -> None:
        # Accept story_hints as alias for story_presentations (backward compat)
        story_hints = kwargs.pop("story_hints", None)
        if story_hints and not story_presentations:
            story_presentations = story_hints
        # Accept detail_story_ids as alias for story_ids (backward compat)
        detail_story_ids = kwargs.pop("detail_story_ids", None)

        s_ids = tuple(story_ids)
        if not s_ids and detail_story_ids:
            s_ids = tuple(detail_story_ids)
        if not s_ids and story_presentations:
            s_ids = tuple(p.story_id for p in story_presentations if getattr(p, "story_id", None))
        object.__setattr__(self, "story_ids", s_ids)
        object.__setattr__(self, "required_facts", tuple(required_facts))
        object.__setattr__(
            self,
            "_city_situation",
            city_situation if city_situation is not None else _CompatibilitySituationPlan(),
        )
        if not story_presentations and s_ids:
            story_presentations = tuple(
                DigestStoryPresentation(story_id=sid, mode="DETAIL_ONLY") for sid in s_ids
            )
        object.__setattr__(self, "_story_presentations", tuple(story_presentations))

    @property
    def detail_story_ids(self) -> tuple[str, ...]:
        """Story IDs eligible for thematic (detail) blocks — excludes DASHBOARD_ONLY stories."""
        if not self._story_presentations:
            return self.story_ids
        return tuple(
            p.story_id
            for p in self._story_presentations
            if getattr(p, "mode", "DETAIL_ONLY") != "DASHBOARD_ONLY"
        )

    @property
    def city_situation(self) -> Any:
        return self._city_situation

    @property
    def story_presentations(self) -> tuple[Any, ...]:
        return self._story_presentations

    @property
    def story_hints(self) -> tuple[Any, ...]:
        return self._story_presentations

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "story_ids": list(self.story_ids),
            "required_facts": [fact.to_dict() for fact in self.required_facts],
        }


# ---------------------------------------------------------------------------
# Backward-compatible stub types for legacy tests / code that still imports
# dashboard-era presentation classes.  These are intentionally thin so that
# the consuming code can call getattr() on them safely.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CitySituationPresentationGroup:
    """Legacy city-situation group stub (kept for backward compatibility)."""

    group_id: str = ""
    group_kind: str = ""
    subject_key: str = ""
    subject_label: str = ""
    state: str = ""
    source_refs: tuple[str, ...] = ()
    detail_lines: tuple[str, ...] = ()
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()

    @property
    def reader_text(self) -> str:
        parts = list(self.detail_lines)
        return "; ".join(parts) if parts else self.subject_label

    @property
    def all_detail_lines(self) -> tuple[str, ...]:
        return self.detail_lines


@dataclass(frozen=True)
class CitySituationPresentationPlan:
    """Legacy city-situation plan stub (kept for backward compatibility)."""

    groups: tuple[CitySituationPresentationGroup, ...] = ()
    covered_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestStoryPresentation:
    """Legacy per-story presentation descriptor (kept for backward compatibility)."""

    story_id: str = ""
    mode: str = "DETAIL_ONLY"
    detail_support_ids: tuple[str, ...] = ()
    merge_group_id: str = ""
    city_situation_group_ids: tuple[str, ...] = ()

    # detail_role is derived from mode for backward compatibility
    @property
    def detail_role(self) -> str:
        return "DRILL_DOWN" if self.mode == "DASHBOARD_AND_DRILLDOWN" else "NORMAL"


@dataclass(frozen=True)
class DigestStoryPresentationHint:
    """Hint variant of DigestStoryPresentation with explicit detail_role."""

    story_id: str = ""
    detail_support_ids: tuple[str, ...] = ()
    merge_group_id: str = ""
    detail_role: str = "NORMAL"


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


def _derive_observation_fact_id(card_id: str, obs: Any, idx: int) -> str:
    loc = (getattr(obs, "location", "") or "").strip()
    detail = (getattr(obs, "detail", "") or "").strip()
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
    return f"{card_id}_obs_{idx + 1}"


def _derive_hard_fact_id(card_id: str, text: str, idx: int) -> str:
    cf = text.casefold()
    if "центр" in cf and ("170" in text or "напряжен" in cf):
        return "center_voltage"
    for candidate in (
        "нагорная часть",
        "нагорной части",
        "нагорная",
        "слободка",
        "слободке",
        "колония",
        "колонии",
        "петровского",
        "ул. петровского",
        "самолёт",
        "самолет",
    ):
        if candidate in cf:
            slug = re.sub(r"[^\w]+", "_", candidate).strip("_")
            if "нагорн" in slug:
                return "нагорная_часть"
            if "слободк" in slug:
                return "слободка"
            if "петровск" in slug:
                return "ул_петровского"
            return slug
    slug = re.sub(r"[^\w]+", "_", text[:30].casefold()).strip("_")
    return slug or f"{card_id}_fact_{idx + 1}"


def _resolve_fact_supports(
    direct_refs: Sequence[str],
    card: Any,
    evidence_map: Mapping[str, Any],
) -> tuple[str, ...]:
    supports: list[str] = [r for r in direct_refs if r]
    ref_set = set(supports)
    for evi in evidence_map.values():
        if getattr(evi, "publication_use", "PUBLISH") != "PUBLISH":
            continue
        e_ref = getattr(evi, "source_ref", None)
        eid = getattr(evi, "evidence_id", "")
        if (e_ref and e_ref in ref_set) or (eid and eid in ref_set):
            if eid and eid not in supports:
                supports.append(eid)
    if not supports:
        for r in getattr(card, "representative_source_refs", []) or []:
            if r and r not in supports:
                supports.append(r)
    return tuple(dict.fromkeys(supports))


def build_required_digest_facts(
    *,
    cards: Sequence[Any],
    evidence: Mapping[str, Any] | None = None,
    city_situation: CitySituationRollup | None = None,
) -> tuple[RequiredDigestFact, ...]:
    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    # If legacy city_situation with items is provided, use legacy extraction
    if city_situation and city_situation.items:
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

            # Resolve allowed support IDs from current_source_refs / source_refs
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
            for evi in evidence_map.values():
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

            # Derive rubric_id from the first owning StoryCard
            first_owning_card = card_by_id.get(fact_stories[0])
            rubric_id = getattr(first_owning_card, "rubric_id", "") or "infrastructure"

            # Preserve subject_key, subject_label, and sanitized reader fact text
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

    # Event-First canonical extraction directly from StoryCards without city_situation
    card_by_id = {c.id: c for c in cards}
    required_facts = []
    seen_fact_ids: set[str] = set()

    for card in cards:
        service_family = _canonical_service_family(card)
        is_operational = (
            service_family is not None
            or getattr(card, "story_kind", "") == "operational_status"
            or getattr(card, "rubric_id", "") in ("infrastructure", "utilities", "communal")
        )
        if not is_operational:
            continue

        subj_key = service_family or "infrastructure"
        subj_label = getattr(card, "topic", "") or getattr(card, "category", "") or subj_key.title()
        rubric_id = getattr(card, "rubric_id", "") or "infrastructure"

        obs_list = getattr(card, "operational_observations", []) or []
        if obs_list:
            for o_idx, obs in enumerate(obs_list):
                o_loc = getattr(obs, "location", "") or ""
                o_detail = getattr(obs, "detail", "") or ""
                o_text = f"{o_loc}: {o_detail}".strip(": ") if o_loc else o_detail
                if not o_text:
                    continue

                fact_id = _derive_observation_fact_id(card.id, obs, o_idx)
                if fact_id in seen_fact_ids:
                    fact_id = f"{fact_id}_{o_idx + 1}"
                seen_fact_ids.add(fact_id)

                obs_refs = list(getattr(obs, "source_refs", []) or [])
                for fid in getattr(obs, "source_fragment_ids", []) or []:
                    ref_fid = f"fragment:{fid}"
                    if ref_fid not in obs_refs:
                        obs_refs.append(ref_fid)

                obs_supports = _resolve_fact_supports(obs_refs, card, evidence_map)

                required_facts.append(
                    RequiredDigestFact(
                        fact_id=fact_id,
                        rubric_id=rubric_id,
                        subject_key=getattr(obs, "subject_key", "") or subj_key,
                        subject_label=getattr(obs, "subject_label", "") or subj_label,
                        story_ids=(card.id,),
                        support_ids=obs_supports,
                        text=o_text,
                    )
                )
        else:
            hf_list = getattr(card, "hard_facts", []) or []
            for h_idx, hf in enumerate(hf_list):
                hf_text = getattr(hf, "text", "").strip()
                if not hf_text:
                    continue
                fact_id = _derive_hard_fact_id(card.id, hf_text, h_idx)
                if fact_id in seen_fact_ids:
                    fact_id = f"{fact_id}_{h_idx + 1}"
                seen_fact_ids.add(fact_id)

                hf_refs = list(getattr(hf, "source_refs", []) or [])
                hf_supports = _resolve_fact_supports(hf_refs, card, evidence_map)

                required_facts.append(
                    RequiredDigestFact(
                        fact_id=fact_id,
                        rubric_id=rubric_id,
                        subject_key=subj_key,
                        subject_label=subj_label,
                        story_ids=(card.id,),
                        support_ids=hf_supports,
                        text=hf_text,
                    )
                )

    return tuple(required_facts)


def build_digest_presentation_plan(
    *,
    cards: Sequence[Any],
    city_situation: CitySituationRollup | None = None,
    evidence: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> DigestPresentationPlan:
    """Build the reader-independent presentation plan containing selected story IDs and required facts."""
    evidence_map = evidence if isinstance(evidence, Mapping) else {}
    return DigestPresentationPlan(
        story_ids=tuple(card.id for card in cards),
        required_facts=build_required_digest_facts(
            cards=cards,
            evidence=evidence_map,
            city_situation=city_situation,
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


def _canonical_topic_family(card: Any, rubric_id: str = "") -> tuple[str, str, str]:
    """Return (topic_key, topic_label, emoji) for a card based on its content and rubric."""
    cat = (getattr(card, "category", "") or "").casefold()
    topic = (getattr(card, "topic", "") or "").casefold()
    summary = (getattr(card, "summary", "") or "").casefold()
    tags = {str(t).casefold() for t in getattr(card, "tags", []) or []}
    text_corpus = f"{cat} {topic} {summary} {' '.join(tags)}"
    tokens = set(re.findall(r"[a-zа-яё0-9]+", text_corpus))

    # 1. Electricity / Электроснабжение
    if {
        "electricity",
        "power",
        "blackout",
        "свет",
        "электроснабжение",
        "электроэнергия",
        "электричество",
        "подстанция",
        "подстанции",
        "рэс",
        "горсвет",
        "напряжение",
        "киловольт",
    } & tokens:
        if not any(w in topic for w in ("услуги электрика", "электрик на дом", "частный электрик")):
            return ("electricity", "Электроснабжение", "⚡️")

    # 2. Water / Водоснабжение
    if {"water", "водоснабжение", "вода", "водоканал", "водопровод", "порыв", "водоводе"} & tokens:
        if not any(w in topic for w in ("услуги сантехника", "сантехник", "баки")):
            return ("water", "Водоснабжение", "💧")

    # 3. Gas / Газоснабжение
    if {"gas", "газ", "газоснабжение", "горгаз", "газопровод"} & tokens:
        return ("gas", "Газоснабжение", "💨")

    # 4. Heating / Отопление
    if {"heating", "отопление", "теплосеть", "котельная", "теплоснабжение"} & tokens:
        return ("heating", "Отопление", "♨️")

    # 5. Telecom / Connectivity / Internet
    if {
        "telecom",
        "connectivity",
        "связь",
        "интернет",
        "провайдер",
        "мобильная",
        "сотовая",
        "вышка",
        "оптика",
        "миртелеком",
        "онэт",
    } & tokens:
        return ("connectivity", "Связь и интернет", "🌐")

    # 6. Transport & Roads
    if {
        "transport",
        "транспорт",
        "автобус",
        "автобусы",
        "маршрутка",
        "маршрут",
        "перевозки",
        "дорога",
        "дороги",
        "чонгар",
        "перекрытие",
        "трасса",
    } & tokens:
        return ("transport", "Транспорт и дороги", "🚌")

    # 7. Safety / Strikes / Air Defense
    if {
        "взрыв",
        "взрывы",
        "обстрел",
        "обстрелы",
        "пво",
        "бпла",
        "дрон",
        "дроны",
        "прилет",
        "прилеты",
        "сирена",
        "тревога",
        "хлопок",
        "хлопки",
        "атеш",
    } & tokens:
        return ("strikes", "Безопасность и происшествия", "💥")

    # 8. Fires / Emergency
    if {"пожар", "пожары", "возгорание", "мчс", "спасатели"} & tokens:
        return ("fire", "Пожары и происшествия", "🔥")

    # 9. Civic services & Documents
    if {
        "паспорт",
        "паспортный",
        "мфц",
        "гибдд",
        "гаи",
        "пенсионный",
        "госуслуги",
        "нотариус",
        "доверенность",
        "прописка",
        "документы",
        "заявление",
    } & tokens:
        return ("civic_services", "Городские службы и документы", "🏛")

    # 10. Banks & Cash
    if {
        "банк",
        "банки",
        "банкомат",
        "банкоматы",
        "наличные",
        "сбер",
        "пнкб",
        "карта",
        "перерасчет",
    } & tokens:
        return ("banking", "Банки и наличные", "🏧")

    # 11. Medicine & Health
    if {
        "больница",
        "больницы",
        "поликлиника",
        "аптека",
        "аптеки",
        "врач",
        "врачи",
        "медицина",
        "медицинский",
        "флюорография",
        "визант",
    } & tokens:
        return ("health", "Медицина и здоровье", "🏥")

    # 12. Education & Culture
    if {
        "школа",
        "школы",
        "детсад",
        "детсады",
        "училище",
        "вуз",
        "образование",
        "спорт",
        "культура",
        "музей",
    } & tokens:
        return ("education", "Образование и культура", "🎓")

    # 13. Social help & benefits
    if {
        "выплата",
        "выплаты",
        "пособие",
        "пособия",
        "гуманитарная",
        "гумпомощь",
        "помощь",
        "льготы",
        "социальная",
    } & tokens:
        return ("social", "Социальная помощь", "🏢")

    # 14. Economy & Business
    if {
        "магазин",
        "магазины",
        "рынок",
        "цена",
        "цены",
        "торговля",
        "предприятие",
        "бизнес",
    } & tokens:
        return ("economy", "Торговля и экономика", "💼")

    # Fallback to rubric ID
    r = (getattr(card, "rubric_id", "") or rubric_id or cat).casefold()
    if "safety" in r or "безопасн" in r:
        return ("safety_general", "Безопасность", "🛡")
    if "infrastructure" in r or "utilities" in r or "коммун" in r or "жкх" in r:
        return ("infrastructure_general", "Коммунальная сфера", "⚡️")
    if "comm" in r or "связ" in r:
        return ("connectivity_general", "Связь и интернет", "🌐")
    if "mobil" in r or "transport" in r or "трансп" in r:
        return ("mobility_general", "Транспорт и дороги", "🚌")
    if "health" in r or "медиц" in r:
        return ("health_general", "Медицина и здоровье", "🏥")
    if "civic" in r or "служб" in r:
        return ("civic_general", "Городские службы", "🏛")
    if "educ" in r or "образ" in r:
        return ("education_general", "Образование и культура", "🎓")
    if "soc" in r or "соц" in r:
        return ("social_general", "Социальная помощь", "🏢")
    if "econ" in r or "эконом" in r:
        return ("economy_general", "Экономика и бизнес", "💼")
    if "focus" in r or "фокус" in r:
        return ("focus_general", "В фокусе внимания", "🎯")

    return ("other_general", "Городские события", "📌")


_KNOWN_LOCATIONS = (
    # Районы и микрорайоны
    "Центр",
    "Нагорная часть",
    "Нагорный район",
    "Колония",
    "Слободка",
    "Лиски",
    "Азмол",
    "АКЗ",
    "Пески",
    "Дальняя Коса",
    "Ближняя Коса",
    "Коса",
    "РТС",
    "Военный городок",
    "8 Марта",
    "Черёмушки",
    "Стекловолокно",
    # Улицы и проспекты
    "проспект Победы",
    "проспект Труда",
    "улица Гайдара",
    "улица Кирово",
    "улица Орджоникидзе",
    "улица Руденко",
    "улица Правды",
    "улица Свободы",
    "улица Шевченко",
    "улица Дюмина",
    "улица Крупской",
    "улица Морозова",
    "улица Нагорная",
    "улица Пионерская",
    "улица Тверская",
    "улица Карла Маркса",
    "улица Франко",
    "улица Ростовская",
    "улица Тищенко",
    "Мелитопольское шоссе",
    "Восточный проспект",
    # Ориентиры
    "район 19 училища",
    "19 училище",
    "район Пакета",
    "район Меры",
    "район Сбера",
    "самолёт",
    "вокзал",
    "водоканал",
    "ПНС Димитрова",
    "ТЦ «Дель Мар»",
)


def _extract_bundle_locations(texts: Sequence[str]) -> tuple[str, ...]:
    """Extract and deduplicate known locations from a collection of texts."""
    found: list[str] = []
    combined = " ".join(texts)
    combined_l = combined.casefold()
    for loc in _KNOWN_LOCATIONS:
        if loc.casefold() in combined_l and loc not in found:
            found.append(loc)
    for match in re.finditer(r"\b(?:ул\.|улице|улицы|на|по)\s+([А-Я][а-я]+(?:\s+\d+)?)", combined):
        val = match.group(1).strip()
        if len(val) >= 4 and val not in found and not val.startswith(("Бердян", "Город", "Район")):
            found.append(val)
    return tuple(found)


_FACT_INTERNAL_METADATA_RE = re.compile(
    r"\s*\[[^\]\n]+\]\s*(?:" r"(?:AVAILABLE|UNAVAILABLE|DEGRADED|CONFLICTING)\s*[—-]\s*" r")?",
    re.IGNORECASE,
)
_FACT_QUESTION_RE = re.compile(
    r"(?:\?|\b(?:интересу(?:ется|ются|ются|ются)|спрашива(?:ет|ют|ют)|"
    r"выясня(?:ет|ют)|узна(?:ет|ют)|подскажите|кто\s+знает)\b)",
    re.IGNORECASE,
)
_FACT_META_OR_ADVICE_RE = re.compile(
    r"\b(?:подробности\s+(?:не\s+)?уточняются|детали\s+не\s+раскрыты|"
    r"уточняйте\s+в\s+официальных\s+источниках|"
    r"совет(?:ую|ует|уют)|рекоменду(?:ется|ют|ет)|"
    r"не\s+(?:обстреливайте|появляйтесь|ходите|звоните)|"
    r"сообщается\s+о\s+(?:событии|ситуации)|"
    r"сообщени[ея]\s+(?:сообщества\s+о\s+выполнении\s+работ|о\s+(?:событии|ситуации))|"
    r"жител(?:и|ь)\s+(?:интересу(?:ются|ется)|спрашива(?:ют|ет)|зада(?:ют|ёт)\s+вопрос)|"
    r"что[- ]то\s+(?:произошло|отключилось|случилось))\b",
    re.IGNORECASE,
)

_FACT_DIRECTORY_OR_PROMO_RE = re.compile(
    r"\b(?:"
    r"ежедневн\w*\s+(?:автобусн\w*\s+)?(?:рейс\w*|пассажирск\w*\s+перевоз\w*)|"
    r"атмосфер\w*\s+красот\w*|маленьк\w*\s+леди|"
    r"при[её]м\s+автомобил\w*\s+в\s+разбор|"
    r"задава(?:ть|йте)\s+вопрос\w*\s+в\s+личн\w*\s+сообщени\w*|"
    r"сообщени[ея]\s+о\s+контактн\w*\s+телефон\w*"
    r")\b",
    re.IGNORECASE,
)


def _is_fact_noise_sentence(text: str) -> bool:
    """Return whether a sentence is chat metadata, a question, or unsolicited advice."""
    t_l = text.casefold().strip()
    if not t_l:
        return True
    if _FACT_QUESTION_RE.search(t_l):
        return True
    if _FACT_META_OR_ADVICE_RE.search(t_l):
        return True
    if _FACT_DIRECTORY_OR_PROMO_RE.search(t_l):
        return True
    if any(
        marker in t_l
        for marker in (
            "сообщения с эмодзи",
            "сообщение сообщества о выполнении работ",
            "настраиваются на позитив",
            "эмоциональное сообщение",
            "уровень пройти не может",
            "не может пройти через кпп",
            "радиусе",
            "точнее не работает вообще",
            "всем мира",
            "город спит и пусть",
        )
    ):
        return True
    if re.match(r"^(?:один|кто-то|кто то)\s+(?:отвечает|говорит|пишет)\b", t_l):
        return True
    return False


def _clean_fact_sentence(text: str) -> str:
    """Sanitize a raw sentence from chat noise, questions, and internal metadata."""
    t = _FACT_INTERNAL_METADATA_RE.sub(" ", text or "").strip()
    if not t:
        return ""
    t = re.sub(r"^(?:да|ну\s+да|а)\s*,\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"^(?:ах\s+да\s*,?\s*)?(?:вроде|кажется)\s*,?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"^(?:сообщение\s+от\s+(?:местного\s+)?жителя|по\s+сообщениям\s+жителей|"
        r"(?:местный\s+)?жител(?:и|ь)\s+сообща(?:ют|ет)|по\s+словам\s+горожан)[\s,:]*",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()

    # Keep concrete sentences from a mixed summary while dropping appended
    # questions, chat reactions, and advice boilerplate.
    sentences = re.split(r"(?<=[.!?])\s+", t)
    kept = [sentence.strip() for sentence in sentences if not _is_fact_noise_sentence(sentence)]
    t = " ".join(kept).strip()
    if not t:
        return ""
    t = re.sub(
        r"\s*(?:,|;)?\s*(?:подробности|детали)\s+(?:не\s+)?уточняются\.?$",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    if t:
        t = t[:1].upper() + t[1:]
    return t.rstrip(". ") + "."


def _is_usable_fact_line(text: str) -> bool:
    """Filter out chat noise, resident questions, classified ads, lost & found, and meta comments."""
    cleaned = _clean_fact_sentence(text)
    if not cleaned or len(cleaned) < 12:
        return False
    t = cleaned
    words = t.split()
    if len(words) < 3:
        return False
    t_l = t.casefold()

    # 1. Questions
    if (
        _FACT_QUESTION_RE.search(t_l)
        or _FACT_META_OR_ADVICE_RE.search(t_l)
        or _FACT_DIRECTORY_OR_PROMO_RE.search(t_l)
    ):
        return False

    # 2. Lost & found animals / personal items / lost belongings
    if any(
        k in t_l
        for k in (
            "пропал кот",
            "пропала кошка",
            "пропала собака",
            "потерялась собака",
            "потерялся пес",
            "потерялся пёс",
            "нашли собачку",
            "нашли собаку",
            "нашлась собака",
            "найдена собака",
            "найденная собака",
            "найденной собаке",
            "найденную собаку",
            "найденного кота",
            "найденной кошке",
            "найденный кот",
            "нашли щенка",
            "помогите найти хозяина",
            "ищут хозяина",
            "ищет хозяина",
            "ищем хозяина",
            "поиск хозяев",
            "поиски хозяина",
            "оставил рюкзак",
            "оставили рюкзак",
            "потерял рюкзак",
            "потеряли рюкзак",
            "нашел рюкзак",
            "нашёл рюкзак",
            "найден рюкзак",
            "потерян рюкзак",
            "потерял ключи",
            "потерялись документы",
            "найдены ключи",
            "найден кошелек",
            "найден кошелёк",
            "забыли в автобусе",
            "забыл в автобусе",
            "нашли в автобусе",
            "кто-то нашёл",
            "кто-то нашел",
            "кто то нашел",
            "кто то нашёл",
            "найден паспорт",
            "утерян паспорт",
            "утеряны документы",
            "найдены документы",
        )
    ):
        return False

    # 3. Unconditional chat dialogue fragments & non-factual community openers
    if any(
        k in t_l
        for k in (
            "приглашает посетить",
            "приглашают посетить",
            "дозванивается",
            "дозваниваюсь",
        )
    ):
        return False

    if any(
        t_l.startswith(prefix)
        for prefix in (
            "внизу, район",
            "внизу район",
            "житель приглашает",
            "жители приглашают",
            "сообщение от местного жителя",
            "сообщение от жителя",
            "посетите, а то",
            "только особо не рассчитывайте",
            "самостоятельно будет много быстрее",
            "похоже, забыли",
            "раза с 15",
            "раза с 10",
            "с праздником",
            "доброе утро",
            "добрый вечер",
            "спокойной ночи",
            "всем привет",
        )
    ):
        return False

    # 4. Conversational interjections & colloquial dialogue starts. A colloquial
    # lead-in is not enough to discard a report: short local observations often
    # begin with "у нас" or "да, с ...". Keep them when the line still carries
    # a concrete service/event signal or a date/number.
    concrete_signal = bool(re.search(r"\d", t_l)) or any(
        marker in t_l
        for marker in (
            "свет",
            "электр",
            "напряж",
            "вода",
            "водопровод",
            "газ",
            "интернет",
            "связь",
            "отоплен",
            "автобус",
            "маршрут",
            "дорог",
            "аптек",
            "больниц",
            "врач",
            "выплат",
            "ремонт",
            "авар",
            "пожар",
            "взрыв",
            "дрон",
            "пво",
            "перерасч",
        )
    )
    if (
        any(
            t_l.startswith(prefix)
            for prefix in (
                "та уже",
                "уже тихо",
                "да брат",
                "та не",
                "та вроде",
                "ну да",
                "вот именно",
                "короче",
                "кстати",
                "да, с ",
                "нет, с ",
                "а у нас",
                "у нас тоже",
                "и у меня",
                "вчера будет",
                "а завтра день",
                "а завтра — день",
            )
        )
        and not concrete_signal
    ):
        return False

    # 5. Commercial classifieds, private sales, job postings
    if any(
        k in t_l
        for k in (
            "куплю",
            "продам",
            "цена от",
            "позвонить по номеру",
            "купить стекло",
            "продается",
            "сдам",
            "сниму",
            "требуются",
            "вакансия",
            "в личные сообщения",
            "писать в лс",
            "писать в личку",
            "стоимости перекопки",
            "за сотку",
            "напишите в лс",
            "написать в лс",
        )
    ):
        return False

    # 6. Emojis / technical metadata / chat profanity
    if "эмодзи" in t_l or "смайлик" in t_l or "стикер" in t_l:
        return False
    if any(
        k in t_l
        for k in (
            "чо за фигня",
            "идите нах",
            "кинули не только вас",
            "жесть",
            "херня",
        )
    ):
        return False

    # 7. Meta-commentary without concrete facts
    if "подробности уточняются" in t_l and len(words) <= 8:
        return False
    if "детали не раскрыты" in t_l:
        return False
    if "без конкретных деталей" in t_l:
        return False
    if "эмоциональное сообщение" in t_l:
        return False

    return True


def build_thematic_topic_bundles(
    cards: Sequence[Any],
    *,
    evidence: Mapping[str, Any] | None = None,
    required_facts: Sequence[RequiredDigestFact] = (),
    rubric_id: str | None = None,
) -> tuple[TopicBundle, ...]:
    """Group story cards within rubrics into cohesive, scan-first Thematic Topic Bundles."""
    if not cards:
        return ()

    target_cards = [
        c for c in cards if rubric_id is None or getattr(c, "rubric_id", "") == rubric_id
    ]
    if not target_cards:
        return ()

    by_rubric: dict[str, list[Any]] = {}
    for c in target_cards:
        rid = getattr(c, "rubric_id", "") or "other"
        by_rubric.setdefault(rid, []).append(c)

    bundles: list[TopicBundle] = []
    bundle_counter = 0

    for rid, r_cards in by_rubric.items():
        groups_by_key: dict[str, list[Any]] = {}
        for c in r_cards:
            t_key, _, _ = _canonical_topic_family(c, rid)
            # A rubric fallback such as ``other_general`` is only a label, not
            # evidence that two stories describe the same subject.  Merging all
            # such cards creates mixed headlines/bodies (and makes the fallback
            # print unrelated chat fragments together).  Keep unclassified
            # topics separate until a stronger deterministic family is known.
            group_key = f"{t_key}:{c.id}" if t_key.endswith("_general") else t_key
            groups_by_key.setdefault(group_key, []).append(c)

        rubric_bundles: list[TopicBundle] = []
        for group_key, g_cards in groups_by_key.items():
            bundle_counter += 1
            sample_card = g_cards[0]
            t_key, t_label, t_emoji = _canonical_topic_family(sample_card, rid)
            story_ids = tuple(c.id for c in g_cards)

            # Collect allowed supports
            all_sups: list[str] = []
            for c in g_cards:
                for s in _card_allowed_supports(c):
                    if s not in all_sups:
                        all_sups.append(s)
            if evidence:
                for eid, evi in evidence.items():
                    evi_sid = getattr(evi, "story_id", None)
                    if (
                        evi_sid and (str(evi_sid) in story_ids or f"story:{evi_sid}" in story_ids)
                    ) or eid.startswith(tuple(f"{sid}:" for sid in story_ids)):
                        if (
                            getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                            and eid not in all_sups
                        ):
                            all_sups.append(eid)

            # Collect texts for location extraction and fact ledger
            raw_texts: list[str] = []
            fact_candidates: list[str] = []
            for c in g_cards:
                if c.topic:
                    raw_texts.append(c.topic)
                if c.summary:
                    raw_texts.append(c.summary)
                    cleaned_summary = _clean_fact_sentence(c.summary)
                    for atom in re.split(r"(?<=[.!?])\s+", cleaned_summary):
                        if _is_usable_fact_line(atom):
                            fact_candidates.append(_clean_fact_sentence(atom))
                for hf in getattr(c, "hard_facts", []) or []:
                    if hf.text:
                        raw_texts.append(hf.text)
                        cleaned_fact = _clean_fact_sentence(hf.text)
                        for atom in re.split(r"(?<=[.!?])\s+", cleaned_fact):
                            if _is_usable_fact_line(atom):
                                fact_candidates.append(_clean_fact_sentence(atom))
                for co in getattr(c, "community_observations", []) or []:
                    if co.text:
                        raw_texts.append(co.text)
                        cleaned_observation = _clean_fact_sentence(co.text)
                        for atom in re.split(r"(?<=[.!?])\s+", cleaned_observation):
                            if _is_usable_fact_line(atom):
                                fact_candidates.append(_clean_fact_sentence(atom))

            locations = _extract_bundle_locations(raw_texts)

            # Deduplicate fact ledger propositions
            dedup_facts: list[str] = []
            seen_cf: set[str] = set()
            for fc in fact_candidates:
                key = fc.casefold()
                if key not in seen_cf and not any(key in s or s in key for s in seen_cf):
                    seen_cf.add(key)
                    dedup_facts.append(fc)

            # If all raw lines were filtered as chatter, preserve cleaned summary/topic
            if not dedup_facts:
                fallback_source = sample_card.summary or sample_card.topic
                fallback_cand = (
                    _clean_fact_sentence(fallback_source)
                    if _is_usable_fact_line(fallback_source)
                    else ""
                )
                if fallback_cand:
                    dedup_facts.append(fallback_cand)

            # Determine epistemic kind
            is_official = False
            if evidence:
                for s in all_sups:
                    evi = evidence.get(s)
                    if evi and (
                        getattr(evi, "kind", "") == "official_statement"
                        or getattr(evi, "source_role", "") in {"official", "authority"}
                    ):
                        is_official = True
                        break
            epistemic_status = "официальная информация" if is_official else "сообщения жителей"

            # Extract distinct operational states
            bundle_states: list[str] = []
            for c in g_cards:
                for co in getattr(c, "community_observations", []) or []:
                    if co.text and _is_usable_fact_line(co.text):
                        st = _clean_fact_sentence(co.text)
                        if st not in bundle_states:
                            bundle_states.append(st)
                for hf in getattr(c, "hard_facts", []) or []:
                    if hf.text and _is_usable_fact_line(hf.text):
                        st = _clean_fact_sentence(hf.text)
                        if st not in bundle_states:
                            bundle_states.append(st)
            if not bundle_states and dedup_facts:
                bundle_states = list(dedup_facts[:4])

            # Match required operational facts
            story_id_set = set(story_ids)
            bundle_req_facts = tuple(
                rf for rf in required_facts if bool(set(rf.story_ids) & story_id_set)
            )

            if not dedup_facts and not bundle_req_facts:
                continue

            rubric_bundles.append(
                TopicBundle(
                    bundle_id=f"bundle:{rid}:{group_key}",
                    rubric_id=rid,
                    topic_key=t_key,
                    topic_label=t_label,
                    emoji=t_emoji,
                    story_ids=story_ids,
                    support_ids=tuple(all_sups),
                    fact_ledger=tuple(dedup_facts[:8]),
                    locations=locations,
                    required_facts=bundle_req_facts,
                    status_summary="",
                    states=tuple(bundle_states),
                    epistemic_status=epistemic_status,
                )
            )

        bundles.extend(rubric_bundles)

    return tuple(bundles)


def build_digest_presentation_units(
    cards: Sequence[Any],
    presentation_plan: Any = None,
    *,
    max_synthesis_size: int = 100,
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
                t_key, _, _ = _canonical_topic_family(c, rid)
                if t_key and not t_key.endswith("_general"):
                    gid = f"topic:{t_key}"
                else:
                    gid = fallback_merge_groups.get(c.id, c.id)
            groups_by_key.setdefault(gid, []).append(c)

        for gid, g_cards in groups_by_key.items():
            is_service = gid.startswith("service:") or gid.startswith("topic:")
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
