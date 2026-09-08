"""Story-thread clustering, trajectory classification, and milestone extraction for longitudinal articles."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.editorial_models import StoryCard
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleProminence,
    ArticleStoryAssignment,
    ArticleStoryCoverage,
    ArticleThematicSection,
)

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_STOP_WORDS = {
    "в",
    "и",
    "на",
    "с",
    "по",
    "к",
    "для",
    "о",
    "об",
    "от",
    "до",
    "из",
    "за",
    "при",
    "что",
    "как",
    "не",
    "то",
    "но",
    "а",
    "же",
    "ли",
    "бы",
    "город",
    "города",
    "жители",
    "жителей",
    "горожане",
    "сообщают",
    "сообщение",
    "день",
    "дня",
}


class TrajectoryKind(str, Enum):
    """Temporal trajectory of a multi-day narrative thread."""

    CHRONIC_EVOLVING = "CHRONIC_EVOLVING"  # Active across 3+ distinct days
    ACUTE_PIVOTAL = "ACUTE_PIVOTAL"  # High burst of activity in 1-2 days
    BACKGROUND_LOCAL = "BACKGROUND_LOCAL"  # Everyday civic life, sporadic


class ThreadEditorialWeight(str, Enum):
    """Editorial prominence weighting for longitudinal synthesis."""

    LEAD_THREAD = "LEAD_THREAD"  # Multi-paragraph treatment (maps to DEVELOP)
    WEAVE_THREAD = "WEAVE_THREAD"  # Substantive chapter section (maps to WEAVE)
    BRIEF_THREAD = "BRIEF_THREAD"  # Compact milestone mention (maps to BRIEF)


@dataclass(frozen=True)
class StoryMilestone:
    """A dated factual milestone along a story thread trajectory."""

    date: dt.date
    date_str: str  # Format: "DD.MM"
    fact: str
    support_id: str
    source_type: str = ""


@dataclass(frozen=True)
class StoryThread:
    """A thematic and temporal progression of related events over days or weeks."""

    id: str
    title: str
    rubric: str
    story_ids: tuple[str, ...]
    trajectory: TrajectoryKind
    weight: ThreadEditorialWeight
    milestones: tuple[StoryMilestone, ...] = ()
    support_ids: tuple[str, ...] = ()
    summary: str = ""


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in (
        "оснабжение",
        "оканал",
        "оканала",
        "оканалу",
        "опровод",
        "опровода",
        "ского",
        "скому",
        "ской",
        "ских",
        "ская",
        "ское",
        "ными",
        "ным",
        "ных",
        "ная",
        "ное",
        "ной",
        "ные",
        "ний",
        "няя",
        "нее",
        "ние",
        "ния",
        "ний",
        "ями",
        "ами",
        "ям",
        "ам",
        "ах",
        "ях",
        "ов",
        "ев",
        "ей",
        "ой",
        "ом",
        "ем",
        "ы",
        "и",
        "а",
        "я",
        "у",
        "ю",
        "е",
        "о",
    ):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break
    return w


def _tokenize(text: str) -> set[str]:
    raw_tokens = {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 3}
    filtered = raw_tokens - _STOP_WORDS
    stems = {_stem(t) for t in filtered if len(_stem(t)) >= 3}
    # Also include 4-char prefixes for compound root matching (e.g. водо...)
    prefixes = {t[:4] for t in filtered if len(t) >= 4}
    return filtered | stems | prefixes


def _cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


def classify_thread_trajectory(
    dates: Sequence[dt.datetime | dt.date],
    total_observations: int = 1,
) -> TrajectoryKind:
    """Classify thread trajectory as CHRONIC_EVOLVING, ACUTE_PIVOTAL, or BACKGROUND_LOCAL."""
    if not dates:
        return TrajectoryKind.BACKGROUND_LOCAL

    distinct_days = {d.date() if isinstance(d, dt.datetime) else d for d in dates}

    if len(distinct_days) >= 3:
        return TrajectoryKind.CHRONIC_EVOLVING

    if total_observations >= 3 or len(dates) >= 3:
        return TrajectoryKind.ACUTE_PIVOTAL

    return TrajectoryKind.BACKGROUND_LOCAL


def _parse_ts(raw: str | dt.datetime | dt.date) -> dt.datetime:
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.timezone.utc)
    if isinstance(raw, dt.date):
        return dt.datetime(raw.year, raw.month, raw.day, tzinfo=dt.timezone.utc)
    # Parse ISO string
    cleaned = str(raw).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(cleaned)


def build_milestone_timeline(
    story_id: str,
    facts: Sequence[tuple[str | dt.datetime, str, str]],
) -> tuple[StoryMilestone, ...]:
    """Extract and sort chronological milestones for a story or thread."""
    raw_milestones: list[tuple[dt.datetime, StoryMilestone]] = []

    for item in facts:
        raw_time, fact_text, sup_id = item[0], item[1], item[2]
        try:
            ts = _parse_ts(raw_time)
        except Exception:
            ts = dt.datetime.now(dt.timezone.utc)

        m = StoryMilestone(
            date=ts.date(),
            date_str=ts.strftime("%d.%m"),
            fact=fact_text.strip(),
            support_id=sup_id,
        )
        raw_milestones.append((ts, m))

    raw_milestones.sort(key=lambda x: x[0])
    return tuple(m for _, m in raw_milestones)


def cluster_stories_into_threads(
    cards: Sequence[StoryCard],
    story_dates: Mapping[str, Sequence[dt.datetime | dt.date]] | None = None,
    story_embeddings: Mapping[str, Sequence[float]] | None = None,
    story_support_ids: Mapping[str, Sequence[str]] | None = None,
    min_similarity: float = 0.70,
) -> list[StoryThread]:
    """Cluster multi-day StoryCards into coherent StoryThread narratives."""
    if not cards:
        return []

    story_dates = story_dates or {}
    story_embeddings = story_embeddings or {}
    story_support_ids = story_support_ids or {}

    # Disjoint-set forest for clustering
    parent: dict[str, str] = {c.id: c.id for c in cards}

    def find(x: str) -> str:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    card_tokens = {c.id: _tokenize(f"{c.topic} {c.summary}") for c in cards}

    # Pairwise linkage based on rubric + entity/token overlap OR embedding similarity
    card_list = list(cards)
    for i in range(len(card_list)):
        c1 = card_list[i]
        tokens1 = card_tokens[c1.id]
        emb1 = story_embeddings.get(c1.id)

        for j in range(i + 1, len(card_list)):
            c2 = card_list[j]
            tokens2 = card_tokens[c2.id]
            emb2 = story_embeddings.get(c2.id)

            # 1. Embedding vector check
            if emb1 and emb2:
                sim = _cosine_similarity(emb1, emb2)
                if sim >= min_similarity:
                    union(c1.id, c2.id)
                    continue

            # 2. Shared rubric and high token overlap (Jaccard >= 0.35 or intersection >= 2 entities)
            rub1 = getattr(c1, "rubric_id", "") or getattr(c1, "rubric", "")
            rub2 = getattr(c2, "rubric_id", "") or getattr(c2, "rubric", "")
            if rub1 and rub2 and rub1 == rub2:
                common_tokens = tokens1 & tokens2
                if len(common_tokens) >= 2:
                    union(c1.id, c2.id)
                elif tokens1 and tokens2:
                    jaccard = len(common_tokens) / len(tokens1 | tokens2)
                    if jaccard >= 0.35:
                        union(c1.id, c2.id)

    # Group cards by cluster root
    groups: dict[str, list[StoryCard]] = defaultdict(list)
    for c in cards:
        groups[find(c.id)].append(c)

    def _card_source_count(c: StoryCard) -> int:
        if hasattr(c, "source_count"):
            return int(c.source_count)
        refs = getattr(c, "representative_source_refs", [])
        return len(refs) if refs else 1

    threads: list[StoryThread] = []
    thread_idx = 1

    for _root_id, group_cards in groups.items():
        # Sort group cards by source count / prominence descending
        group_cards.sort(key=_card_source_count, reverse=True)
        primary_card = group_cards[0]

        all_story_ids = tuple(c.id for c in group_cards)
        all_dates: list[dt.datetime | dt.date] = []
        all_supports: list[str] = []

        for sid in all_story_ids:
            all_dates.extend(story_dates.get(sid, []))
            all_supports.extend(story_support_ids.get(sid, []))

        total_obs = max(len(all_dates), sum(_card_source_count(c) for c in group_cards))
        trajectory = classify_thread_trajectory(all_dates, total_observations=total_obs)

        # Determine editorial weight
        if trajectory == TrajectoryKind.CHRONIC_EVOLVING or total_obs >= 5:
            weight = ThreadEditorialWeight.LEAD_THREAD
        elif trajectory == TrajectoryKind.ACUTE_PIVOTAL or len(group_cards) > 1:
            weight = ThreadEditorialWeight.WEAVE_THREAD
        else:
            weight = ThreadEditorialWeight.BRIEF_THREAD

        primary_rubric = (
            getattr(primary_card, "rubric_id", "")
            or getattr(primary_card, "rubric", "")
            or "Городская жизнь"
        )
        thread = StoryThread(
            id=f"thread:{thread_idx}",
            title=primary_card.topic,
            rubric=primary_rubric,
            story_ids=all_story_ids,
            trajectory=trajectory,
            weight=weight,
            milestones=(),
            support_ids=tuple(dict.fromkeys(all_supports)),
            summary=primary_card.summary,
        )
        threads.append(thread)
        thread_idx += 1

    return threads


@dataclass(frozen=True)
class AnchorPublicationSummary:
    """Summary of an existing publication within the reporting window."""

    publication_id: int
    publication_type: str
    title: str
    created_at: dt.datetime
    lead: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_CHAPTER_SPECS = [
    (
        "chapter_infra",
        "Инфраструктура и жизнеобеспечение",
        {
            "жкх",
            "вода",
            "водоснабжение",
            "электричество",
            "свет",
            "газ",
            "отопление",
            "коммуналка",
            "авария",
            "ремонт",
            "инфраструктура",
            "водоканал",
            "сети",
            "водовод",
        },
        "Хроника коммунальных ремонтов, стабильности подачи ресурсов и аварийных работ за период.",
    ),
    (
        "chapter_transit",
        "Городской транспорт и логистика",
        {
            "транспорт",
            "автобус",
            "маршрут",
            "дорога",
            "дороги",
            "логистика",
            "проезд",
            "перевозки",
            "рейс",
            "сообщение",
            "маршрутка",
        },
        "Состояние маршрутной сети, графики движения и ключевые изменения в сообщении.",
    ),
    (
        "chapter_market",
        "Потребительский рынок и цены",
        {
            "рынок",
            "цены",
            "магазин",
            "продукты",
            "банк",
            "банки",
            "деньги",
            "наличные",
            "выплаты",
            "пенсии",
            "торговля",
            "товары",
        },
        "Динамика цен, доступность основных товаров и работа финансовых сервисов.",
    ),
    (
        "chapter_civic",
        "Социальная жизнь и городская среда",
        {
            "социальная",
            "спорт",
            "культура",
            "школа",
            "образование",
            "медицина",
            "больница",
            "дети",
            "общество",
            "благоустройство",
            "городская среда",
        },
        "События городской жизни, социальные инициативы и городская атмосфера.",
    ),
]


def _match_chapter_for_thread(thread: StoryThread) -> str:
    tokens = _tokenize(f"{thread.rubric} {thread.title} {thread.summary}")
    best_chap = "chapter_civic"
    best_score = -1

    for chap_id, _, keywords, _ in _CHAPTER_SPECS:
        rub_lower = thread.rubric.lower()
        if any(k in rub_lower for k in keywords):
            return chap_id
        overlap = len(tokens & keywords)
        if overlap > best_score:
            best_score = overlap
            best_chap = chap_id

    return best_chap


def build_longitudinal_coverage_plan(
    threads: Sequence[StoryThread],
    cards_by_id: dict[str, StoryCard] | None = None,
    anchor_pubs: Sequence[Any] = (),
) -> ArticleCoveragePlan:
    """Group story threads into thematic chapters and build a zero-loss coverage plan."""
    cards_by_id = cards_by_id or {}
    threads_by_chap: dict[str, list[StoryThread]] = defaultdict(list)

    for t in threads:
        chap_id = _match_chapter_for_thread(t)
        threads_by_chap[chap_id].append(t)

    sections: list[ArticleThematicSection] = []
    story_coverages: list[ArticleStoryCoverage] = []
    global_rank = 1

    spec_dict = {spec[0]: spec for spec in _CHAPTER_SPECS}

    # Iterate through defined chapters in canonical order, plus any dynamic chapters
    active_chapter_ids = [s[0] for s in _CHAPTER_SPECS if s[0] in threads_by_chap]
    for cid in threads_by_chap:
        if cid not in active_chapter_ids:
            active_chapter_ids.append(cid)

    for chap_id in active_chapter_ids:
        chap_threads = threads_by_chap[chap_id]
        if not chap_threads:
            continue

        spec = spec_dict.get(chap_id)
        sec_title = spec[1] if spec else "Городская жизнь"
        narrative_intent = spec[3] if spec else "События и изменения городской среды за период."

        # Sort threads inside chapter: LEAD first, then WEAVE, then BRIEF
        weight_order = {
            ThreadEditorialWeight.LEAD_THREAD: 0,
            ThreadEditorialWeight.WEAVE_THREAD: 1,
            ThreadEditorialWeight.BRIEF_THREAD: 2,
        }
        chap_threads.sort(key=lambda t: weight_order.get(t.weight, 1))
        lead_thread = chap_threads[0]
        lead_story_id = lead_thread.story_ids[0] if lead_thread.story_ids else lead_thread.id

        assignments: list[ArticleStoryAssignment] = []

        for thread in chap_threads:
            depth: ArticleProminence = (
                "DEVELOP"
                if thread.weight == ThreadEditorialWeight.LEAD_THREAD
                else "WEAVE"
                if thread.weight == ThreadEditorialWeight.WEAVE_THREAD
                else "BRIEF"
            )

            # Every story in the thread is mapped to an assignment and a story coverage
            for sid in thread.story_ids:
                card = cards_by_id.get(sid)
                topic = card.topic if card else thread.title
                sups = tuple(thread.support_ids) if thread.support_ids else (f"{sid}:ev:1",)

                assignment = ArticleStoryAssignment(
                    story_id=sid,
                    section_id=chap_id,
                    depth=depth,
                    rank=global_rank,
                    primary_evidence_ids=sups,
                    concrete_details=(),
                )
                assignments.append(assignment)

                story_cov = ArticleStoryCoverage(
                    story_id=sid,
                    topic=topic,
                    rank=global_rank,
                    prominence=depth,
                    support_ids=sups,
                    detail_support_ids=(),
                )
                story_coverages.append(story_cov)
                global_rank += 1

        sec = ArticleThematicSection(
            section_id=chap_id,
            title=sec_title,
            lead_story_id=lead_story_id,
            story_assignments=tuple(assignments),
            narrative_intent=narrative_intent,
        )
        sections.append(sec)

    return ArticleCoveragePlan(
        stories=tuple(story_coverages),
        sections=tuple(sections),
    )


def extract_story_thread_maps(
    support_index: Sequence[Any],
) -> tuple[dict[str, list[dt.datetime | dt.date]], dict[str, list[str]]]:
    """Extract story dates and support IDs mappings from article support index."""
    story_dates_map: dict[str, list[dt.datetime | dt.date]] = defaultdict(list)
    story_sups_map: dict[str, list[str]] = defaultdict(list)
    for sup in support_index:
        sid = getattr(sup, "story_id", "") or ""
        if not sid:
            m = re.search(r"story:\d+", getattr(sup, "support_id", ""))
            if m:
                sid = m.group(0)
        if sid:
            sup_id = getattr(sup, "support_id", "")
            if sup_id:
                story_sups_map[sid].append(sup_id)
            observed = getattr(sup, "observed_at", None)
            if observed:
                story_dates_map[sid].append(observed)
    return story_dates_map, story_sups_map
