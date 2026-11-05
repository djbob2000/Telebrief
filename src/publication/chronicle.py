"""Storyline chronicle: the permanent editorial memory behind period reviews.

A daily digest and a daily long read cover one reporting window. A monthly,
quarterly or yearly review needs the storylines that ran through the whole
period, without loading thousands of Stories into one writer call.

The chronicle is built by background maintenance, outside publication runs:

* every Story with a current revision belongs to one theme (power, water,
  transport, ...) and is linked once, in the week it first appeared, to one
  storyline of that theme; Stories that join no named storyline are linked to
  the theme's catch-all storyline;
* a weekly entry summarises one storyline for a calendar week clipped to its
  month; the model organises a theme's week into storylines, and every
  statement it writes must be grounded in the fragments it cites;
* month, quarter, half-year and year entries roll up from their child entries
  and may cite only the refs of those children.

Entries are navigation and editorial memory, never citable evidence: a review
publishes only facts grounded in the PUBLISH supports of the Stories its
entries reference, under the unchanged Evidence Boundary (AGENTS.md §0.12).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from src.domain.service_taxonomy import detect_service_families
from src.publication.grounding import clean_text, grounded_claim, parse_refs

logger = logging.getLogger(__name__)

CHRONICLE_VERSION = "chronicle-v1"
PERIOD_KINDS = ("week", "month", "quarter", "half_year", "year")

# A week is closed this long after its end, once processing has caught up.
WEEK_GRACE = dt.timedelta(hours=4)
# A processing backlog delays a week at most this long past its end.
MAX_BACKLOG_WAIT = dt.timedelta(hours=48)
RUNNING_STORYLINE_DAYS = 35
MAX_RUNNING_STORYLINES = 60
MAX_INPUT_CHARS = 100_000
# A week chunk is smaller: the answer restates its reports as storyline facts.
WEEK_INPUT_CHARS = 45_000
MIN_MODEL_STORIES = 3
MAX_REPORTS_PER_STORY = 6
PAIR_SIMILARITY = 0.82
WEEK_FACTS = 8
MAX_DETAILS = 2
REF_TEXT_CHARS = 400
ROLLUP_FACTS = {"month": 12, "quarter": 12, "half_year": 12, "year": 14}
ROLLUP_STORYLINES_PER_THEME = {"month": 20, "quarter": 20, "half_year": 20, "year": 24}
ROLLUP_OTHER_FACTS = {"month": 12, "quarter": 12, "half_year": 12, "year": 14}
LATE_LINK_DAYS = 60
MODEL_MAX_TOKENS = 16_000

THEME_LABELS: dict[str, str] = {
    "safety": "Безопасность",
    "power": "Электроснабжение",
    "water": "Водоснабжение",
    "gas": "Газоснабжение",
    "heating": "Отопление",
    "telecom": "Связь и интернет",
    "transport": "Транспорт",
    "health": "Здоровье и медицина",
    "civic_services": "Банки, документы и госуслуги",
    "municipal": "Городское хозяйство",
    "economy": "Цены, торговля и работа",
    "education_culture": "Образование и культура",
    "society": "Общество",
    "other": "Городская жизнь",
}

# Service family -> theme, in priority order when a Story names several.
_FAMILY_THEMES: tuple[tuple[str, str], ...] = (
    ("safety", "safety"),
    ("power", "power"),
    ("water", "water"),
    ("gas", "gas"),
    ("heating", "heating"),
    ("telecom", "telecom"),
    ("urban_transport", "transport"),
    ("transport", "transport"),
    ("health", "health"),
    ("banking", "civic_services"),
    ("civic_services", "civic_services"),
    ("municipal", "municipal"),
    ("lift", "municipal"),
    ("economy", "economy"),
    ("logistics", "economy"),
    ("education_culture", "education_culture"),
    ("social", "society"),
)


def story_theme(headline: str, summary: str) -> str:
    """Deterministic theme of a Story from its headline and summary."""
    families = detect_service_families(f"{headline} {summary}")
    for family, theme in _FAMILY_THEMES:
        if family in families:
            return theme
    return "other"


# ---------------------------------------------------------------------------
# Periods


@dataclass(frozen=True, order=True)
class ChroniclePeriod:
    """A calendar period in edition local dates; ``end`` is exclusive."""

    kind: str
    start: dt.date
    end: dt.date

    @property
    def last_day(self) -> dt.date:
        return self.end - dt.timedelta(days=1)

    def contains(self, day: dt.date) -> bool:
        return self.start <= day < self.end


def _add_months(day: dt.date, months: int) -> dt.date:
    years, month = divmod(day.month - 1 + months, 12)
    return dt.date(day.year + years, month + 1, 1)


def period_for(kind: str, day: dt.date) -> ChroniclePeriod:
    """The month, quarter, half-year or year containing ``day``."""
    if kind == "month":
        start = day.replace(day=1)
        return ChroniclePeriod(kind, start, _add_months(start, 1))
    if kind == "quarter":
        start = dt.date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
        return ChroniclePeriod(kind, start, _add_months(start, 3))
    if kind == "half_year":
        start = dt.date(day.year, 1 if day.month <= 6 else 7, 1)
        return ChroniclePeriod(kind, start, _add_months(start, 6))
    if kind == "year":
        return ChroniclePeriod(kind, dt.date(day.year, 1, 1), dt.date(day.year + 1, 1, 1))
    raise ValueError(f"unsupported chronicle period kind: {kind!r}")


def week_segments(start: dt.date, end: dt.date) -> list[ChroniclePeriod]:
    """Monday-to-Sunday weeks between two dates, split at month boundaries."""
    segments: list[ChroniclePeriod] = []
    current = start
    while current < end:
        next_monday = current + dt.timedelta(days=7 - current.weekday())
        segment_end = min(next_monday, _add_months(current.replace(day=1), 1), end)
        segments.append(ChroniclePeriod("week", current, segment_end))
        current = segment_end
    return segments


def child_periods(period: ChroniclePeriod) -> list[ChroniclePeriod]:
    """The shorter chronicle periods a period rolls up from."""
    if period.kind == "month":
        return week_segments(period.start, period.end)
    child_kind, months = {
        "quarter": ("month", 1),
        "half_year": ("quarter", 3),
        "year": ("quarter", 3),
    }[period.kind]
    children = []
    current = period.start
    while current < period.end:
        children.append(period_for(child_kind, current))
        current = _add_months(current, months)
    return children


def periods_between(kind: str, first: dt.date, last: dt.date) -> list[ChroniclePeriod]:
    """Every period of ``kind`` that overlaps [first, last]."""
    if kind == "week":
        return week_segments(first.replace(day=1), last + dt.timedelta(days=1))
    periods: list[ChroniclePeriod] = []
    current = period_for(kind, first)
    while current.start <= last:
        periods.append(current)
        current = period_for(kind, current.end)
    return periods


def period_bounds(period: ChroniclePeriod, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """UTC instants of a period's local start and end."""
    start = dt.datetime.combine(period.start, dt.time(0), tzinfo=tz)
    end = dt.datetime.combine(period.end, dt.time(0), tzinfo=tz)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def _zone(name: str | None) -> dt.tzinfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - an invalid profile timezone falls back to UTC
        return dt.timezone.utc


# ---------------------------------------------------------------------------
# Input


@dataclass(frozen=True)
class ChronicleReport:
    """One citable PUBLISH source fragment of a Story."""

    ref: str
    story_id: int
    observed_at: dt.datetime
    source_id: int | None
    evidence_text: str
    source_text: str

    def check_texts(self) -> tuple[str, ...]:
        return tuple(text for text in (self.evidence_text, self.source_text) if text)


@dataclass(frozen=True)
class ChronicleStory:
    story_id: int
    theme: str
    headline: str
    first_seen_at: dt.datetime
    reports: tuple[ChronicleReport, ...]

    @property
    def source_ids(self) -> set[int]:
        return {r.source_id for r in self.reports if r.source_id is not None}


_STORY_SQL = """
SELECT s.id, sc.first_seen_at,
       COALESCE(NULLIF(sr.event_payload->>'headline', ''), sr.event_payload->>'topic'),
       sr.event_payload->>'digest_summary',
       (SELECT jsonb_agg(jsonb_build_object('text', e->>'text', 'ids', e->'source_fragment_ids'))
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(sr.event_payload->'evidence_items') = 'array'
                 THEN sr.event_payload->'evidence_items' ELSE '[]'::jsonb END) AS e
        WHERE e->>'publication_use' = 'PUBLISH')
FROM stories s
JOIN story_cluster_state sc ON sc.story_id = s.id
JOIN story_revisions sr ON sr.id = s.current_revision_id
WHERE s.edition_id = %(edition_id)s
  AND sc.first_seen_at >= %(start)s AND sc.first_seen_at < %(end)s
  AND sr.event_payload IS NOT NULL
  AND COALESCE(sr.event_payload->>'publishability', 'news') IN ('news', 'brief')
  AND (
      SELECT d.scope_class
      FROM story_edition_scope_decisions d
      WHERE d.story_id = s.id AND d.edition_id = %(edition_id)s
      ORDER BY d.created_at DESC, d.id DESC
      LIMIT 1
  ) IN ('LOCAL', 'DIRECT_IMPACT')
  AND (
      NOT %(unlinked_only)s
      OR NOT EXISTS (SELECT 1 FROM story_storyline_links l WHERE l.story_id = s.id)
  )
  AND (%(story_ids)s::bigint[] IS NULL OR s.id = ANY(%(story_ids)s::bigint[]))
ORDER BY sc.first_seen_at, s.id
"""


async def load_chronicle_stories(
    conn: Any,
    *,
    edition_id: int,
    start: dt.datetime,
    end: dt.datetime,
    unlinked_only: bool = True,
    story_ids: Sequence[int] | None = None,
) -> list[ChronicleStory]:
    """In-scope Stories first seen in [start, end) with their PUBLISH evidence."""
    cursor = await conn.execute(
        _STORY_SQL,
        {
            "edition_id": edition_id,
            "start": start,
            "end": end,
            "unlinked_only": unlinked_only,
            "story_ids": list(story_ids) if story_ids is not None else None,
        },
    )
    rows = await cursor.fetchall()
    pending: list[tuple[int, dt.datetime, str, str, list[tuple[str, int]]]] = []
    fragment_ids: set[int] = set()
    for story_id, first_seen_at, raw_headline, raw_summary, items in rows:
        headline = clean_text(raw_headline, 200)
        summary = clean_text(raw_summary, 400)
        evidence: list[tuple[str, int]] = []
        for item in items or ():
            if not isinstance(item, Mapping):
                continue
            text = clean_text(item.get("text"), REF_TEXT_CHARS)
            ids = item.get("ids")
            for fragment_id in ids if isinstance(ids, list) else ():
                if isinstance(fragment_id, int) and text:
                    evidence.append((text, fragment_id))
                    fragment_ids.add(fragment_id)
        if evidence:
            pending.append((int(story_id), first_seen_at, headline, summary, evidence))
    if not fragment_ids:
        return []
    cursor = await conn.execute(
        """
        SELECT f.id, f.text_content,
               COALESCE(si.published_at, si.first_collected_at, f.created_at), si.source_id
        FROM source_fragments f
        JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
        JOIN source_items si ON si.id = sir.source_item_id
        WHERE f.id = ANY(%s)
        """,
        (sorted(fragment_ids),),
    )
    fragments = {
        int(fid): (clean_text(text, REF_TEXT_CHARS), observed, source_id)
        for fid, text, observed, source_id in await cursor.fetchall()
    }
    stories: list[ChronicleStory] = []
    for story_id, first_seen_at, headline, summary, evidence in pending:
        reports: list[ChronicleReport] = []
        seen: set[str] = set()
        for text, fragment_id in evidence:
            source = fragments.get(fragment_id)
            ref = f"fragment:{fragment_id}"
            if source is None or source[1] is None or ref in seen:
                continue
            seen.add(ref)
            reports.append(
                ChronicleReport(
                    ref=ref,
                    story_id=story_id,
                    observed_at=source[1],
                    source_id=int(source[2]) if source[2] is not None else None,
                    evidence_text=text,
                    source_text=source[0],
                )
            )
        if reports:
            reports.sort(key=lambda r: (r.observed_at, r.ref))
            stories.append(
                ChronicleStory(
                    story_id=story_id,
                    theme=story_theme(headline, summary),
                    headline=headline or reports[0].evidence_text[:160],
                    first_seen_at=first_seen_at,
                    reports=tuple(reports),
                )
            )
    return stories


async def similar_story_pairs(
    conn: Any, story_ids: Sequence[int], *, threshold: float = PAIR_SIMILARITY
) -> list[tuple[int, int]]:
    """Pairs of Stories whose semantic vectors are at least ``threshold`` similar."""
    if len(story_ids) < 2:
        return []
    cursor = await conn.execute(
        """
        WITH v AS (
            SELECT s.id, COALESCE(ssv.embedding, sc.centroid::halfvec) AS e
            FROM unnest(%s::bigint[]) AS s(id)
            LEFT JOIN story_semantic_vectors ssv ON ssv.story_id = s.id
            LEFT JOIN story_cluster_state sc ON sc.story_id = s.id
        )
        SELECT a.id, b.id
        FROM v a
        JOIN v b ON a.id < b.id
        WHERE a.e IS NOT NULL AND b.e IS NOT NULL
          AND vector_dims(a.e) = vector_dims(b.e)
          AND (a.e <=> b.e) <= %s
        """,
        (list(story_ids), 1.0 - threshold),
    )
    return [(int(a), int(b)) for a, b in await cursor.fetchall()]


def order_by_similarity(
    stories: Sequence[ChronicleStory], pairs: Iterable[tuple[int, int]]
) -> list[ChronicleStory]:
    """Keep similar Stories adjacent so one model call sees a whole group."""
    parent = {s.story_id: s.story_id for s in stories}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[ChronicleStory]] = defaultdict(list)
    for story in stories:
        groups[find(story.story_id)].append(story)
    ordered = sorted(groups.values(), key=lambda g: (g[0].first_seen_at, g[0].story_id))
    return [story for group in ordered for story in group]


# ---------------------------------------------------------------------------
# Weekly entries


WEEK_SYSTEM_PROMPT = """You keep the weekly chronicle of a local newsroom.

You receive one theme of city life, one calendar week, the theme's running
storylines and this week's community and official reports grouped by Story. A
storyline is one continuing situation, place or subject: outages in one
district, a damaged cable and its repair, a bus route, a school reopening,
prices at the market. Organise the week's Stories into storylines and write a
short chronicle entry for each.

Return JSON:
{"storylines":[{
  "storyline":"an id from running_storylines, or \\"new\\"",
  "title":"short Russian label of the continuing storyline, e.g. «Отключения света на АКЗ»",
  "areas":["at most 6 districts or places named by its reports"],
  "story_ids":[ids of this week's Stories that belong to it],
  "summary":"2–4 Russian sentences: how the storyline developed this week, attributed",
  "summary_refs":["fragment:ID"],
  "facts":[{"date":"YYYY-MM-DD","text":"one attributed Russian sentence","refs":["fragment:ID"]}],
  "details":[{"text":"one vivid concrete detail or a short exact quote","refs":["fragment:ID"]}]
}]}

Rules:
- Use only the supplied reports. Every text field cites the fragment refs that
  state it in its refs list, never inside the text, and every cited ref belongs
  to one of the entry's story_ids.
- Put each Story in at most one storyline. Continue a running storyline when the
  reports clearly continue it; otherwise start a new one. Leave out a minor
  one-off Story that fits no storyline: it stays in the theme's other news.
- At most 8 facts per storyline, in date order. A fact is one concrete
  development: what happened, where, when, the reported state. Merge repeated
  reports of the same state into one fact that cites all of them.
- At most 2 details: a concrete resident action, interval, amount, place or
  workaround, or a short quote copied exactly from a report inside «».
- Preserve epistemic status: «по словам жителей», «по сообщению водоканала». A
  question, sarcasm or a guess is not a fact.
- Do not invent dates, durations, numbers, causes, repair plans or city-wide
  scope. Each report shows the day it was posted, and you may name that day.
  Write any other date, number or duration only when a cited report states it.
- Write all text in Russian.
"""


@dataclass
class EntryDraft:
    """A chronicle entry before it is stored."""

    storyline_key: str
    theme: str
    title: str
    areas: list[str] = field(default_factory=list)
    story_ids: list[int] = field(default_factory=list)
    trajectory: str = ""
    trajectory_refs: list[str] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    method: str = "model"
    child_count: int = 0


def _story_row(story: ChronicleStory, tz: dt.tzinfo) -> dict[str, Any]:
    by_source: dict[int | None, ChronicleReport] = {}
    for report in story.reports:
        by_source.setdefault(report.source_id, report)
    chosen = list(by_source.values())
    chosen += [r for r in story.reports if r not in chosen]
    return {
        "story_id": story.story_id,
        "headline": story.headline,
        "reports": [
            {
                "ref": r.ref,
                "date": r.observed_at.astimezone(tz).date().isoformat(),
                "text": r.evidence_text,
            }
            for r in sorted(chosen[:MAX_REPORTS_PER_STORY], key=lambda r: r.observed_at)
        ],
    }


def chunk_stories(
    stories: Sequence[ChronicleStory], tz: dt.tzinfo, *, budget: int = WEEK_INPUT_CHARS
) -> list[list[ChronicleStory]]:
    """Split a theme's ordered Stories into prompt-sized chunks."""
    chunks: list[list[ChronicleStory]] = []
    current: list[ChronicleStory] = []
    used = 0
    for story in stories:
        size = len(json.dumps(_story_row(story, tz), ensure_ascii=False)) + 2
        if current and used + size > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(story)
        used += size
    if current:
        chunks.append(current)
    return chunks


def build_week_prompt(
    *,
    edition_name: str,
    theme: str,
    period: ChroniclePeriod,
    running: Sequence[Mapping[str, Any]],
    stories: Sequence[ChronicleStory],
    tz: dt.tzinfo,
) -> str:
    return json.dumps(
        {
            "edition": edition_name,
            "theme": THEME_LABELS.get(theme, theme),
            "week": {
                "first_day": period.start.isoformat(),
                "last_day": period.last_day.isoformat(),
            },
            "running_storylines": list(running),
            "stories": [_story_row(story, tz) for story in stories],
        },
        ensure_ascii=False,
        indent=1,
    )


_INLINE_REFS_RE = re.compile(r"\s*[\(\[]\s*(?:fragment:\d+[\s,;]*)+[\)\]]|\s*\bfragment:\d+\b")
_INLINE_REF_RE = re.compile(r"fragment:\d+")


def split_inline_refs(value: Any) -> tuple[str, list[str]]:
    """Move ``fragment:<id>`` markers a model wrote inside prose into a ref list."""
    text = str(value or "")
    refs = _INLINE_REF_RE.findall(text)
    return _INLINE_REFS_RE.sub("", text), refs


def clip_sentences(value: Any, limit: int) -> str:
    """Collapse whitespace and cut at the last sentence or clause end within ``limit``."""
    text = clean_text(value, 100_000)
    if len(text) <= limit:
        return text
    head = text[:limit]
    for marks in (".!?", ";"):
        cut = max(head.rfind(mark) for mark in marks)
        if cut >= limit // 2:
            return head[: cut + 1].strip()
    return head.rsplit(" ", 1)[0].rstrip(",:—- ")


def _own_refs(refs: Iterable[str], own: Mapping[str, Any] | set[str]) -> list[str]:
    return [ref for ref in dict.fromkeys(refs) if ref in own]


def _local_date(value: dt.datetime, tz: dt.tzinfo) -> str:
    return value.astimezone(tz).date().isoformat()


_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def posted_day_text(iso_day: str) -> str:
    """The day a report was posted, as text a grounded statement may name."""
    try:
        day = dt.date.fromisoformat(str(iso_day)[:10])
    except ValueError:
        return ""
    return (
        f"{day.day} {_MONTHS_GENITIVE[day.month - 1]} {day.year} года "
        f"{day.day:02d}.{day.month:02d}.{day.year}"
    )


def _fact_date(raw: Any, allowed: Sequence[str]) -> str:
    text = str(raw or "").strip()[:10]
    return text if text in allowed else min(allowed)


def deterministic_facts(
    stories: Sequence[ChronicleStory], tz: dt.tzinfo, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """One fact per Story: its earliest PUBLISH evidence, heaviest Stories first."""
    ranked = sorted(stories, key=lambda s: (-len(s.source_ids), s.first_seen_at, s.story_id))
    if limit is not None:
        ranked = ranked[:limit]
    facts: list[dict[str, Any]] = [
        {
            "date": _local_date(story.reports[0].observed_at, tz),
            "text": story.reports[0].evidence_text,
            "refs": [story.reports[0].ref],
            "story_ids": [story.story_id],
            "sources": max(1, len(story.source_ids)),
        }
        for story in ranked
    ]
    facts.sort(key=lambda f: (f["date"], f["refs"][0]))
    return facts


def validate_week_storylines(
    parsed: Any,
    *,
    theme: str,
    stories: Sequence[ChronicleStory],
    known_keys: set[str],
    claimed: set[int],
    tz: dt.tzinfo,
    new_key: Any,
    stats: dict[str, int],
) -> list[EntryDraft]:
    """Keep only grounded storyline entries whose Stories and refs come from the input."""
    items = parsed.get("storylines") if isinstance(parsed, Mapping) else None
    if not isinstance(items, list):
        stats["malformed"] = stats.get("malformed", 0) + 1
        return []
    by_id = {story.story_id: story for story in stories}
    drafts: list[EntryDraft] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        story_ids: list[int] = []
        for raw in item.get("story_ids") or ():
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid in by_id and sid not in claimed and sid not in story_ids:
                story_ids.append(sid)
        if not story_ids:
            stats["empty"] = stats.get("empty", 0) + 1
            continue
        claimed.update(story_ids)
        own = {r.ref: r for sid in story_ids for r in by_id[sid].reports}
        dates = {ref: _local_date(r.observed_at, tz) for ref, r in own.items()}
        texts = {ref: (*r.check_texts(), posted_day_text(dates[ref])) for ref, r in own.items()}
        quotes = {ref: r.source_text for ref, r in own.items()}
        facts: list[dict[str, Any]] = []
        for raw_fact in (item.get("facts") or ())[:WEEK_FACTS]:
            if not isinstance(raw_fact, Mapping):
                continue
            raw_text, inline = split_inline_refs(raw_fact.get("text"))
            text = clip_sentences(raw_text, 600)
            refs = _own_refs([*parse_refs(raw_fact.get("refs")), *inline], own)
            if not grounded_claim(text, refs, texts, quote_texts=quotes):
                stats["facts_dropped"] = stats.get("facts_dropped", 0) + 1
                continue
            facts.append(
                {
                    "date": _fact_date(raw_fact.get("date"), [dates[ref] for ref in refs]),
                    "text": text,
                    "refs": refs,
                    "story_ids": sorted({own[ref].story_id for ref in refs}),
                    "sources": len({own[ref].source_id for ref in refs}),
                }
            )
        details: list[dict[str, Any]] = []
        for raw_detail in (item.get("details") or ())[:MAX_DETAILS]:
            if not isinstance(raw_detail, Mapping):
                continue
            raw_text, inline = split_inline_refs(raw_detail.get("text"))
            text = clip_sentences(raw_text, 400)
            refs = _own_refs([*parse_refs(raw_detail.get("refs")), *inline], own)
            if grounded_claim(text, refs, texts, quote_texts=quotes):
                details.append({"text": text, "refs": refs})
            else:
                stats["details_dropped"] = stats.get("details_dropped", 0) + 1
        raw_summary, inline = split_inline_refs(item.get("summary"))
        summary = clip_sentences(raw_summary, 1200)
        summary_refs = _own_refs([*parse_refs(item.get("summary_refs")), *inline], own)
        if not grounded_claim(summary, summary_refs, texts, quote_texts=quotes):
            if summary:
                stats["summaries_dropped"] = stats.get("summaries_dropped", 0) + 1
            summary, summary_refs = "", []
        method = "model"
        if not facts:
            facts = deterministic_facts([by_id[sid] for sid in story_ids], tz, limit=WEEK_FACTS)
            method = "deterministic"
        facts.sort(key=lambda f: (f["date"], f["refs"][0]))
        key = str(item.get("storyline") or "").strip()
        drafts.append(
            EntryDraft(
                storyline_key=key if key in known_keys else new_key(),
                theme=theme,
                title=clean_text(item.get("title"), 120) or by_id[story_ids[0]].headline[:120],
                areas=[a for a in (clean_text(x, 60) for x in item.get("areas") or ()) if a][:6],
                story_ids=story_ids,
                trajectory=summary,
                trajectory_refs=summary_refs,
                facts=facts,
                details=details,
                method=method,
            )
        )
    return drafts


def merge_drafts(first: EntryDraft, second: EntryDraft) -> EntryDraft:
    """Merge two chunk entries of the same storyline in one week."""
    trajectory = clip_sentences(
        " ".join(t for t in (first.trajectory, second.trajectory) if t), 1600
    )
    facts = sorted(first.facts + second.facts, key=lambda f: (f["date"], f["refs"][0]))
    return EntryDraft(
        storyline_key=first.storyline_key,
        theme=first.theme,
        title=second.title or first.title,
        areas=list(dict.fromkeys(first.areas + second.areas))[:6],
        story_ids=first.story_ids + [s for s in second.story_ids if s not in first.story_ids],
        trajectory=trajectory,
        trajectory_refs=list(dict.fromkeys(first.trajectory_refs + second.trajectory_refs)),
        facts=facts,
        details=(first.details + second.details)[:MAX_DETAILS],
        method="model" if "model" in (first.method, second.method) else "deterministic",
    )


def entry_weight(stories: Sequence[ChronicleStory], tz: dt.tzinfo) -> dict[str, Any]:
    source_ids = sorted({sid for story in stories for sid in story.source_ids})
    days = sorted({_local_date(r.observed_at, tz) for story in stories for r in story.reports})
    return {
        "stories": len(stories),
        "sources": len(source_ids),
        "reports": sum(len(story.reports) for story in stories),
        "days": days,
        "source_ids": source_ids,
    }


def weight_score(weight: Mapping[str, Any]) -> int:
    """Rank storylines by how long they ran and how many sources reported them."""
    return (
        4 * len(weight.get("days") or ())
        + 2 * int(weight.get("sources") or 0)
        + int(weight.get("stories") or 0)
    )


def _ref_texts(
    refs: Iterable[str], reports: Mapping[str, ChronicleReport], tz: dt.tzinfo
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ref in refs:
        report = reports.get(ref)
        if report is not None:
            out[ref] = {
                "evidence": report.evidence_text,
                "source": report.source_text,
                "story_id": report.story_id,
                "date": _local_date(report.observed_at, tz),
            }
    return out


def _cited_refs(draft: EntryDraft) -> list[str]:
    refs = [ref for fact in draft.facts for ref in fact["refs"]]
    refs += [ref for detail in draft.details for ref in detail["refs"]]
    refs += draft.trajectory_refs
    return list(dict.fromkeys(refs))


# ---------------------------------------------------------------------------
# Model calls


async def _call_model(
    provider: Any,
    model: str | None,
    system: str,
    user: str,
    *,
    reasoning_effort: str | None,
    headroom_tokens: int,
) -> Any:
    from src.ai_providers import apply_reasoning_settings
    from src.utils import robust_extract_json

    kwargs: dict[str, Any] = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "reasoning_effort": "none",
        "thinking": False,
        "max_tokens": MODEL_MAX_TOKENS,
    }
    if model:
        kwargs["model"] = model
    apply_reasoning_settings(kwargs, reasoning_effort, headroom_tokens=headroom_tokens)
    return robust_extract_json(await provider.chat_completion(**kwargs))


async def _call_with_retry(log: logging.Logger, label: str, *args: Any, **kwargs: Any) -> Any:
    for attempt in (1, 2):
        try:
            return await _call_model(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the chronicle falls back to deterministic entries
            log.warning(
                "chronicle %s model call %s failed (%s: %s)",
                label,
                attempt,
                type(exc).__name__,
                exc,
            )
    return None


# ---------------------------------------------------------------------------
# Database


async def _edition(conn: Any, edition_id: int) -> tuple[str, dt.tzinfo]:
    cursor = await conn.execute("SELECT name, timezone FROM editions WHERE id = %s", (edition_id,))
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"edition {edition_id} does not exist")
    return str(row[0]), _zone(row[1])


async def completed_periods(conn: Any, edition_id: int) -> dict[tuple[str, dt.date], dict]:
    cursor = await conn.execute(
        """
        SELECT period_kind, period_start, period_end, story_count, coverage_start
        FROM chronicle_periods
        WHERE edition_id = %s AND chronicle_version = %s
        """,
        (edition_id, CHRONICLE_VERSION),
    )
    return {
        (str(kind), start): {"end": end, "story_count": int(count), "coverage_start": coverage}
        for kind, start, end, count, coverage in await cursor.fetchall()
    }


async def load_running_storylines(
    conn: Any, *, edition_id: int, theme: str, since: dt.datetime
) -> list[dict[str, Any]]:
    """The theme's named storylines active since ``since`` with their latest summary."""
    cursor = await conn.execute(
        """
        SELECT sl.id, sl.title, sl.areas, sl.last_seen_at,
               (SELECT ce.trajectory FROM chronicle_entries ce
                WHERE ce.storyline_id = sl.id AND ce.period_kind = 'week'
                  AND ce.chronicle_version = %(version)s
                ORDER BY ce.period_start DESC LIMIT 1)
        FROM storylines sl
        WHERE sl.edition_id = %(edition_id)s AND sl.theme = %(theme)s
          AND sl.kind = 'storyline' AND sl.chronicle_version = %(version)s
          AND sl.last_seen_at >= %(since)s
        ORDER BY sl.last_seen_at DESC, sl.id DESC
        LIMIT %(limit)s
        """,
        {
            "edition_id": edition_id,
            "theme": theme,
            "since": since,
            "version": CHRONICLE_VERSION,
            "limit": MAX_RUNNING_STORYLINES,
        },
    )
    return [
        {
            "storyline": f"sl:{row[0]}",
            "title": row[1],
            "areas": list(row[2] or ()),
            "last_seen": row[3].date().isoformat(),
            "last_summary": clean_text(row[4], 500),
        }
        for row in await cursor.fetchall()
    ]


async def _theme_other_storyline(
    conn: Any, *, edition_id: int, theme: str, first: dt.datetime, last: dt.datetime, count: int
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO storylines
            (edition_id, theme, kind, title, first_seen_at, last_seen_at, story_count,
             chronicle_version)
        VALUES (%s, %s, 'theme_other', %s, %s, %s, %s, %s)
        ON CONFLICT (edition_id, theme) WHERE kind = 'theme_other'
        DO UPDATE SET
            first_seen_at = LEAST(storylines.first_seen_at, EXCLUDED.first_seen_at),
            last_seen_at = GREATEST(storylines.last_seen_at, EXCLUDED.last_seen_at),
            story_count = storylines.story_count + EXCLUDED.story_count,
            updated_at = now()
        RETURNING id
        """,
        (
            edition_id,
            theme,
            f"{THEME_LABELS.get(theme, theme)}: другие сообщения",
            first,
            last,
            count,
            CHRONICLE_VERSION,
        ),
    )
    row = await cursor.fetchone()
    return int(row[0])


async def _save_entry(
    conn: Any,
    *,
    edition_id: int,
    storyline_id: int,
    period: ChroniclePeriod,
    draft: EntryDraft,
    weight: Mapping[str, Any],
    ref_texts: Mapping[str, Any],
    model: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO chronicle_entries
            (edition_id, storyline_id, period_kind, period_start, period_end,
             chronicle_version, method, model, title, trajectory, facts, details,
             weight, ref_texts, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                %s::jsonb, %s::jsonb, %s::jsonb)
        ON CONFLICT (edition_id, storyline_id, period_kind, period_start, chronicle_version)
        DO UPDATE SET
            period_end = EXCLUDED.period_end, method = EXCLUDED.method,
            model = EXCLUDED.model, title = EXCLUDED.title,
            trajectory = EXCLUDED.trajectory, facts = EXCLUDED.facts,
            details = EXCLUDED.details, weight = EXCLUDED.weight,
            ref_texts = EXCLUDED.ref_texts, metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        (
            edition_id,
            storyline_id,
            period.kind,
            period.start,
            period.end,
            CHRONICLE_VERSION,
            draft.method,
            model if draft.method == "model" else None,
            draft.title,
            draft.trajectory,
            json.dumps(draft.facts, ensure_ascii=False),
            json.dumps(draft.details, ensure_ascii=False),
            json.dumps(dict(weight), ensure_ascii=False),
            json.dumps(dict(ref_texts), ensure_ascii=False),
            json.dumps(
                {
                    "theme": draft.theme,
                    "areas": draft.areas,
                    "story_ids": draft.story_ids,
                    "trajectory_refs": draft.trajectory_refs,
                    "child_count": draft.child_count,
                },
                ensure_ascii=False,
            ),
        ),
    )


async def _mark_period(
    conn: Any,
    *,
    edition_id: int,
    period: ChroniclePeriod,
    story_count: int,
    entry_count: int,
    model_calls: int,
    coverage_start: dt.date | None,
    metadata: Mapping[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO chronicle_periods
            (edition_id, period_kind, period_start, period_end, chronicle_version,
             story_count, entry_count, model_calls, coverage_start, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (edition_id, period_kind, period_start, chronicle_version) DO NOTHING
        """,
        (
            edition_id,
            period.kind,
            period.start,
            period.end,
            CHRONICLE_VERSION,
            story_count,
            entry_count,
            model_calls,
            coverage_start,
            json.dumps(dict(metadata), ensure_ascii=False),
        ),
    )


# ---------------------------------------------------------------------------
# Week build


@dataclass
class WeekPlan:
    """The Stories of one week grouped into the model calls that will process them."""

    period: ChroniclePeriod
    stories: list[ChronicleStory]
    chunks: dict[str, list[list[ChronicleStory]]]

    @property
    def model_calls(self) -> int:
        return sum(len(chunks) for chunks in self.chunks.values())

    def prompt_chars(self, tz: dt.tzinfo) -> int:
        return sum(
            len(json.dumps(_story_row(story, tz), ensure_ascii=False))
            for chunks in self.chunks.values()
            for chunk in chunks
            for story in chunk
        )


async def plan_week(
    conn: Any, *, edition_id: int, period: ChroniclePeriod, tz: dt.tzinfo
) -> WeekPlan:
    """Load a week's unlinked Stories and split each theme into model-call chunks."""
    start, end = period_bounds(period, tz)
    stories = await load_chronicle_stories(conn, edition_id=edition_id, start=start, end=end)
    by_theme: dict[str, list[ChronicleStory]] = defaultdict(list)
    for story in stories:
        by_theme[story.theme].append(story)
    chunks: dict[str, list[list[ChronicleStory]]] = {}
    for theme, theme_stories in sorted(by_theme.items()):
        if len(theme_stories) < MIN_MODEL_STORIES:
            continue
        pairs = await similar_story_pairs(conn, [s.story_id for s in theme_stories])
        chunks[theme] = chunk_stories(order_by_similarity(theme_stories, pairs), tz)
    return WeekPlan(period=period, stories=stories, chunks=chunks)


async def build_week(
    *,
    uow: Any,
    provider: Any,
    model: str | None,
    edition_id: int,
    period: ChroniclePeriod,
    reasoning_effort: str | None = "none",
    headroom_tokens: int = 0,
    log: logging.Logger = logger,
) -> dict[str, Any]:
    """Write the week's storyline entries, link its Stories and mark the week complete."""
    async with uow.transaction() as conn:
        edition_name, tz = await _edition(conn, edition_id)
        plan = await plan_week(conn, edition_id=edition_id, period=period, tz=tz)
        start, _end = period_bounds(period, tz)
        running_by_theme = {
            theme: await load_running_storylines(
                conn,
                edition_id=edition_id,
                theme=theme,
                since=start - dt.timedelta(days=RUNNING_STORYLINE_DAYS),
            )
            for theme in plan.chunks
        }
    stats: dict[str, int] = {"model_calls": 0, "fallback_chunks": 0}
    drafts: dict[str, EntryDraft] = {}
    claimed: set[int] = set()
    counter = iter(range(1, 1_000_000))

    def new_key() -> str:
        return f"new:{next(counter)}"

    for theme, chunks in plan.chunks.items():
        running = list(running_by_theme.get(theme, ()))
        for chunk in chunks:
            known = {str(row["storyline"]) for row in running}
            prompt = build_week_prompt(
                edition_name=edition_name,
                theme=theme,
                period=period,
                running=running,
                stories=chunk,
                tz=tz,
            )
            stats["model_calls"] += 1
            parsed = await _call_with_retry(
                log,
                f"week {period.start} {theme}",
                provider,
                model,
                WEEK_SYSTEM_PROMPT,
                prompt,
                reasoning_effort=reasoning_effort,
                headroom_tokens=headroom_tokens,
            )
            if parsed is None:
                stats["fallback_chunks"] += 1
                continue
            for draft in validate_week_storylines(
                parsed,
                theme=theme,
                stories=chunk,
                known_keys=known,
                claimed=claimed,
                tz=tz,
                new_key=new_key,
                stats=stats,
            ):
                key = draft.storyline_key
                drafts[key] = merge_drafts(drafts[key], draft) if key in drafts else draft
                if key not in known:
                    running.insert(
                        0,
                        {
                            "storyline": key,
                            "title": draft.title,
                            "areas": draft.areas,
                            "last_seen": period.start.isoformat(),
                            "last_summary": draft.trajectory[:500],
                        },
                    )
    async with uow.transaction() as conn:
        result = await save_week(
            conn,
            edition_id=edition_id,
            period=period,
            stories=plan.stories,
            drafts=list(drafts.values()),
            tz=tz,
            model=model,
            stats=stats,
        )
    log.info("chronicle week %s: %s", period.start, result)
    return result


async def save_week(
    conn: Any,
    *,
    edition_id: int,
    period: ChroniclePeriod,
    stories: Sequence[ChronicleStory],
    drafts: Sequence[EntryDraft],
    tz: dt.tzinfo,
    model: str | None,
    stats: Mapping[str, int],
) -> dict[str, Any]:
    by_id = {story.story_id: story for story in stories}
    reports = {r.ref: r for story in stories for r in story.reports}
    linked: set[int] = set()
    entries = 0
    for draft in drafts:
        own = [by_id[sid] for sid in draft.story_ids if sid in by_id]
        if not own:
            continue
        first = min(s.first_seen_at for s in own)
        last = max(r.observed_at for s in own for r in s.reports)
        if draft.storyline_key.startswith("sl:"):
            storyline_id = int(draft.storyline_key[3:])
            await conn.execute(
                """
                UPDATE storylines
                SET title = %s, areas = %s::jsonb,
                    last_seen_at = GREATEST(last_seen_at, %s),
                    story_count = story_count + %s, updated_at = now()
                WHERE id = %s
                """,
                (
                    draft.title,
                    json.dumps(draft.areas, ensure_ascii=False),
                    last,
                    len(own),
                    storyline_id,
                ),
            )
        else:
            cursor = await conn.execute(
                """
                INSERT INTO storylines
                    (edition_id, theme, kind, title, areas, first_seen_at, last_seen_at,
                     story_count, chronicle_version)
                VALUES (%s, %s, 'storyline', %s, %s::jsonb, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    edition_id,
                    draft.theme,
                    draft.title,
                    json.dumps(draft.areas, ensure_ascii=False),
                    first,
                    last,
                    len(own),
                    CHRONICLE_VERSION,
                ),
            )
            storyline_id = int((await cursor.fetchone())[0])
        await _link(conn, [s.story_id for s in own], storyline_id, "chronicle_model", period)
        linked.update(s.story_id for s in own)
        await _save_entry(
            conn,
            edition_id=edition_id,
            storyline_id=storyline_id,
            period=period,
            draft=draft,
            weight=entry_weight(own, tz),
            ref_texts=_ref_texts(_cited_refs(draft), reports, tz),
            model=model,
        )
        entries += 1
    others: dict[str, list[ChronicleStory]] = defaultdict(list)
    for story in stories:
        if story.story_id not in linked:
            others[story.theme].append(story)
    for theme, theme_stories in sorted(others.items()):
        entries += await _save_other_week_entry(
            conn,
            edition_id=edition_id,
            period=period,
            theme=theme,
            stories=theme_stories,
            tz=tz,
            new_story_count=len(theme_stories),
        )
    coverage = min((s.first_seen_at for s in stories), default=None)
    await _mark_period(
        conn,
        edition_id=edition_id,
        period=period,
        story_count=len(stories),
        entry_count=entries,
        model_calls=int(stats.get("model_calls", 0)),
        coverage_start=coverage.astimezone(tz).date() if coverage else None,
        metadata=dict(stats),
    )
    return {"stories": len(stories), "entries": entries, **stats}


async def _link(
    conn: Any, story_ids: Sequence[int], storyline_id: int, method: str, period: ChroniclePeriod
) -> None:
    await conn.execute(
        """
        INSERT INTO story_storyline_links (story_id, storyline_id, method, period_start)
        SELECT story_id, %s, %s, %s FROM unnest(%s::bigint[]) AS story_id
        ON CONFLICT (story_id) DO NOTHING
        """,
        (storyline_id, method, period.start, list(story_ids)),
    )


async def _save_other_week_entry(
    conn: Any,
    *,
    edition_id: int,
    period: ChroniclePeriod,
    theme: str,
    stories: Sequence[ChronicleStory],
    tz: dt.tzinfo,
    new_story_count: int,
) -> int:
    """Link Stories to the theme's catch-all storyline and (re)write its week entry."""
    if not stories:
        return 0
    first = min(s.first_seen_at for s in stories)
    last = max(r.observed_at for s in stories for r in s.reports)
    storyline_id = await _theme_other_storyline(
        conn, edition_id=edition_id, theme=theme, first=first, last=last, count=new_story_count
    )
    await _link(conn, [s.story_id for s in stories], storyline_id, "theme_other", period)
    facts = deterministic_facts(stories, tz)
    draft = EntryDraft(
        storyline_key=f"sl:{storyline_id}",
        theme=theme,
        title=f"{THEME_LABELS.get(theme, theme)}: другие сообщения",
        story_ids=[s.story_id for s in stories],
        facts=facts,
        method="deterministic",
    )
    reports = {r.ref: r for story in stories for r in story.reports}
    await _save_entry(
        conn,
        edition_id=edition_id,
        storyline_id=storyline_id,
        period=period,
        draft=draft,
        weight=entry_weight(stories, tz),
        ref_texts=_ref_texts(_cited_refs(draft), reports, tz),
        model=None,
    )
    return 1


async def link_late_stories(
    *, uow: Any, edition_id: int, now: dt.datetime, log: logging.Logger = logger
) -> int:
    """Add Stories analysed after their week closed to the theme's other news."""
    async with uow.transaction() as conn:
        _name, tz = await _edition(conn, edition_id)
        done = await completed_periods(conn, edition_id)
    horizon = (now - dt.timedelta(days=LATE_LINK_DAYS)).astimezone(tz).date()
    total = 0
    for (kind, start), info in sorted(done.items()):
        if kind != "week" or start < horizon:
            continue
        period = ChroniclePeriod("week", start, info["end"])
        begin, end = period_bounds(period, tz)
        async with uow.transaction() as conn:
            late = await load_chronicle_stories(conn, edition_id=edition_id, start=begin, end=end)
            if not late:
                continue
            by_theme: dict[str, list[ChronicleStory]] = defaultdict(list)
            for story in late:
                by_theme[story.theme].append(story)
            for theme, theme_late in by_theme.items():
                cursor = await conn.execute(
                    """
                    SELECT l.story_id
                    FROM story_storyline_links l
                    JOIN storylines sl ON sl.id = l.storyline_id
                    WHERE sl.edition_id = %s AND sl.theme = %s AND sl.kind = 'theme_other'
                      AND l.period_start = %s
                    """,
                    (edition_id, theme, period.start),
                )
                existing_ids = [int(row[0]) for row in await cursor.fetchall()]
                existing = (
                    await load_chronicle_stories(
                        conn,
                        edition_id=edition_id,
                        start=begin,
                        end=end,
                        unlinked_only=False,
                        story_ids=existing_ids,
                    )
                    if existing_ids
                    else []
                )
                await _save_other_week_entry(
                    conn,
                    edition_id=edition_id,
                    period=period,
                    theme=theme,
                    stories=[*existing, *theme_late],
                    tz=tz,
                    new_story_count=len(theme_late),
                )
            await conn.execute(
                """
                UPDATE chronicle_periods
                SET story_count = story_count + %s,
                    metadata = metadata || jsonb_build_object(
                        'late_linked', COALESCE((metadata->>'late_linked')::int, 0) + %s)
                WHERE edition_id = %s AND period_kind = 'week' AND period_start = %s
                  AND chronicle_version = %s
                """,
                (len(late), len(late), edition_id, period.start, CHRONICLE_VERSION),
            )
        total += len(late)
        log.info("chronicle week %s: linked %s late stories", period.start, len(late))
    return total


# ---------------------------------------------------------------------------
# Rollups


ROLLUP_SYSTEM_PROMPT = """You condense a local newsroom's chronicle of one theme.

You receive storylines, each with its chronicle entries for consecutive shorter
periods: summaries and dated facts that cite source fragment refs. Write one
entry per storyline for the whole longer period.

Return JSON:
{"storylines":[{
  "storyline":"the given id",
  "title":"short Russian label of the storyline over the whole period",
  "summary":"3–5 Russian sentences: how the storyline developed over the period — how it began, its turning points, its state at the end — attributed",
  "summary_refs":["fragment:ID"],
  "facts":[{"date":"YYYY-MM-DD","text":"one attributed Russian sentence","refs":["fragment:ID"]}],
  "details":[{"text":"...","refs":["fragment:ID"]}]
}]}

Rules:
- Use only the supplied entries. Cite refs in the refs lists, never inside the
  text, and only refs that the same storyline's supplied facts or details cite.
- At most FACT_CAP facts per storyline, in date order: keep turning points, the
  longest or widest situations and concrete reported states. Merge repeated
  states into one fact that cites all of their refs.
- At most 2 details, chosen from the supplied details.
- Preserve attribution and epistemic status. Do not invent dates, durations,
  numbers, causes or scope. You may name the day a cited fact was reported;
  write any other date or number only when a cited fact states it.
- Write all text in Russian.
"""


@dataclass
class StoredEntry:
    storyline_id: int
    theme: str
    storyline_kind: str
    period: ChroniclePeriod
    title: str
    trajectory: str
    facts: list[dict[str, Any]]
    details: list[dict[str, Any]]
    weight: dict[str, Any]
    ref_texts: dict[str, dict[str, Any]]
    metadata: dict[str, Any]


async def load_entries(
    conn: Any, *, edition_id: int, periods: Sequence[ChroniclePeriod]
) -> list[StoredEntry]:
    if not periods:
        return []
    cursor = await conn.execute(
        """
        SELECT ce.storyline_id, sl.theme, sl.kind, ce.period_kind, ce.period_start,
               ce.period_end, ce.title, ce.trajectory, ce.facts, ce.details, ce.weight,
               ce.ref_texts, ce.metadata
        FROM chronicle_entries ce
        JOIN storylines sl ON sl.id = ce.storyline_id
        WHERE ce.edition_id = %s AND ce.chronicle_version = %s
          AND ce.period_kind = %s AND ce.period_start = ANY(%s::date[])
        ORDER BY ce.period_start, ce.storyline_id
        """,
        (edition_id, CHRONICLE_VERSION, periods[0].kind, [p.start for p in periods]),
    )
    return [
        StoredEntry(
            storyline_id=int(row[0]),
            theme=str(row[1]),
            storyline_kind=str(row[2]),
            period=ChroniclePeriod(str(row[3]), row[4], row[5]),
            title=str(row[6]),
            trajectory=str(row[7] or ""),
            facts=list(row[8] or ()),
            details=list(row[9] or ()),
            weight=dict(row[10] or {}),
            ref_texts=dict(row[11] or {}),
            metadata=dict(row[12] or {}),
        )
        for row in await cursor.fetchall()
    ]


@dataclass
class MergedStoryline:
    storyline_id: int
    theme: str
    kind: str
    title: str
    children: list[StoredEntry]
    facts: list[dict[str, Any]]
    details: list[dict[str, Any]]
    weight: dict[str, Any]
    ref_texts: dict[str, dict[str, Any]]


def merge_children(children: Sequence[StoredEntry], period: ChroniclePeriod) -> MergedStoryline:
    """Pool child entries of one storyline, keeping only facts dated inside the period."""
    start, end = period.start.isoformat(), period.end.isoformat()
    facts = [f for child in children for f in child.facts if start <= str(f.get("date")) < end]
    facts.sort(key=lambda f: (str(f.get("date")), str((f.get("refs") or [""])[0])))
    ref_texts: dict[str, dict[str, Any]] = {}
    for child in children:
        ref_texts.update(child.ref_texts)
    source_ids = sorted({int(s) for c in children for s in c.weight.get("source_ids") or ()})
    days = sorted({d for c in children for d in c.weight.get("days") or () if start <= d < end})
    weight = {
        "stories": sum(int(c.weight.get("stories") or 0) for c in children),
        "sources": len(source_ids),
        "reports": sum(int(c.weight.get("reports") or 0) for c in children),
        "days": days,
        "source_ids": source_ids,
    }
    latest = max(children, key=lambda c: c.period.start)
    return MergedStoryline(
        storyline_id=latest.storyline_id,
        theme=latest.theme,
        kind=latest.storyline_kind,
        title=latest.title,
        children=list(children),
        facts=facts,
        details=[d for child in children for d in child.details],
        weight=weight,
        ref_texts=ref_texts,
    )


def select_facts(facts: Sequence[Mapping[str, Any]], cap: int) -> list[dict[str, Any]]:
    """The ``cap`` best-sourced facts, back in date order."""
    ranked = sorted(
        facts,
        key=lambda f: (-int(f.get("sources") or len(f.get("refs") or ())), str(f.get("date"))),
    )[:cap]
    return sorted((dict(f) for f in ranked), key=lambda f: str(f.get("date")))


def deterministic_rollup(merged: MergedStoryline, cap: int) -> EntryDraft:
    trajectories = [c.trajectory for c in sorted(merged.children, key=lambda c: c.period.start)]
    return EntryDraft(
        storyline_key=f"sl:{merged.storyline_id}",
        theme=merged.theme,
        title=merged.title,
        story_ids=sorted({s for f in merged.facts for s in f.get("story_ids") or ()}),
        trajectory=clip_sentences(" ".join(t for t in trajectories if t), 2000),
        trajectory_refs=list(
            dict.fromkeys(
                r for c in merged.children for r in c.metadata.get("trajectory_refs") or ()
            )
        ),
        facts=select_facts(merged.facts, cap),
        details=merged.details[:MAX_DETAILS],
        method="deterministic",
        child_count=len(merged.children),
    )


def _rollup_row(merged: MergedStoryline, period: ChroniclePeriod) -> dict[str, Any]:
    start, end = period.start.isoformat(), period.end.isoformat()
    return {
        "storyline": f"sl:{merged.storyline_id}",
        "title": merged.title,
        "periods": [
            {
                "first_day": child.period.start.isoformat(),
                "last_day": child.period.last_day.isoformat(),
                "summary": child.trajectory,
                "facts": [
                    {"date": f.get("date"), "text": f.get("text"), "refs": f.get("refs")}
                    for f in child.facts
                    if start <= str(f.get("date")) < end
                ],
                "details": child.details,
            }
            for child in sorted(merged.children, key=lambda c: c.period.start)
        ],
    }


def validate_rollup(
    parsed: Any,
    *,
    batch: Mapping[str, MergedStoryline],
    period: ChroniclePeriod,
    cap: int,
    stats: dict[str, int],
) -> dict[str, EntryDraft]:
    """Keep grounded rollup entries that cite only their own children's refs."""
    items = parsed.get("storylines") if isinstance(parsed, Mapping) else None
    if not isinstance(items, list):
        stats["malformed"] = stats.get("malformed", 0) + 1
        return {}
    start, end = period.start.isoformat(), period.end.isoformat()
    out: dict[str, EntryDraft] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("storyline") or "").strip()
        merged = batch.get(key)
        if merged is None or key in out:
            continue
        own = {ref for f in merged.facts for ref in f.get("refs") or ()}
        own |= {ref for d in merged.details for ref in d.get("refs") or ()}
        own &= set(merged.ref_texts)
        texts = {
            ref: tuple(
                t
                for t in (
                    merged.ref_texts[ref].get("evidence"),
                    merged.ref_texts[ref].get("source"),
                    posted_day_text(str(merged.ref_texts[ref].get("date") or "")),
                )
                if t
            )
            for ref in own
        }
        quotes = {ref: str(merged.ref_texts[ref].get("source") or "") for ref in own}
        facts: list[dict[str, Any]] = []
        for raw in (item.get("facts") or ())[:cap]:
            if not isinstance(raw, Mapping):
                continue
            raw_text, inline = split_inline_refs(raw.get("text"))
            text = clip_sentences(raw_text, 600)
            refs = _own_refs([*parse_refs(raw.get("refs")), *inline], own)
            dates = [
                str(merged.ref_texts[ref].get("date"))
                for ref in refs
                if start <= str(merged.ref_texts[ref].get("date")) < end
            ]
            if not dates or not grounded_claim(text, refs, texts, quote_texts=quotes):
                stats["facts_dropped"] = stats.get("facts_dropped", 0) + 1
                continue
            facts.append(
                {
                    "date": _fact_date(raw.get("date"), dates),
                    "text": text,
                    "refs": refs,
                    "story_ids": sorted(
                        {
                            int(merged.ref_texts[ref]["story_id"])
                            for ref in refs
                            if merged.ref_texts[ref].get("story_id") is not None
                        }
                    ),
                    "sources": len(refs),
                }
            )
        if not facts:
            continue
        details = []
        for raw in (item.get("details") or ())[:MAX_DETAILS]:
            if not isinstance(raw, Mapping):
                continue
            raw_text, inline = split_inline_refs(raw.get("text"))
            text = clip_sentences(raw_text, 400)
            refs = _own_refs([*parse_refs(raw.get("refs")), *inline], own)
            if grounded_claim(text, refs, texts, quote_texts=quotes):
                details.append({"text": text, "refs": refs})
        raw_summary, inline = split_inline_refs(item.get("summary"))
        summary = clip_sentences(raw_summary, 1600)
        summary_refs = _own_refs([*parse_refs(item.get("summary_refs")), *inline], own)
        if not grounded_claim(summary, summary_refs, texts, quote_texts=quotes):
            if summary:
                stats["summaries_dropped"] = stats.get("summaries_dropped", 0) + 1
            summary, summary_refs = "", []
        if not summary:
            summary = deterministic_rollup(merged, cap).trajectory
        facts.sort(key=lambda f: (f["date"], f["refs"][0]))
        out[key] = EntryDraft(
            storyline_key=key,
            theme=merged.theme,
            title=clean_text(item.get("title"), 120) or merged.title,
            story_ids=sorted({s for f in facts for s in f["story_ids"]}),
            trajectory=summary,
            trajectory_refs=summary_refs,
            facts=facts,
            details=details,
            method="model",
            child_count=len(merged.children),
        )
    return out


def _rollup_batches(
    rows: Sequence[tuple[MergedStoryline, dict[str, Any]]], budget: int = MAX_INPUT_CHARS
) -> list[list[tuple[MergedStoryline, dict[str, Any]]]]:
    batches: list[list[tuple[MergedStoryline, dict[str, Any]]]] = []
    current: list[tuple[MergedStoryline, dict[str, Any]]] = []
    used = 0
    for merged, row in rows:
        size = len(json.dumps(row, ensure_ascii=False))
        if current and used + size > budget:
            batches.append(current)
            current, used = [], 0
        current.append((merged, row))
        used += size
    if current:
        batches.append(current)
    return batches


async def build_rollup(
    *,
    uow: Any,
    provider: Any,
    model: str | None,
    edition_id: int,
    period: ChroniclePeriod,
    reasoning_effort: str | None = "none",
    headroom_tokens: int = 0,
    log: logging.Logger = logger,
) -> dict[str, Any] | None:
    """Roll child entries up into one period; None when a child period is not complete."""
    async with uow.transaction() as conn:
        edition_name, tz = await _edition(conn, edition_id)
        done = await completed_periods(conn, edition_id)
        first_day = await first_story_day(conn, edition_id, tz)
        # Child periods that ended before collection began hold no Stories.
        children = [
            child for child in child_periods(period) if first_day is None or child.end > first_day
        ]
        if not children or any((child.kind, child.start) not in done for child in children):
            return None
        entries = await load_entries(conn, edition_id=edition_id, periods=children)
    coverage = min(
        (
            done[(c.kind, c.start)]["coverage_start"]
            for c in children
            if done[(c.kind, c.start)]["coverage_start"] is not None
        ),
        default=None,
    )
    by_storyline: dict[int, list[StoredEntry]] = defaultdict(list)
    for entry in entries:
        by_storyline[entry.storyline_id].append(entry)
    merged_all = [merge_children(group, period) for group in by_storyline.values()]
    merged_all = [m for m in merged_all if m.facts]
    cap = ROLLUP_FACTS[period.kind]
    keep_per_theme = ROLLUP_STORYLINES_PER_THEME[period.kind]
    named: dict[str, list[MergedStoryline]] = defaultdict(list)
    folded: dict[str, list[MergedStoryline]] = defaultdict(list)
    for merged in merged_all:
        (named if merged.kind == "storyline" else folded)[merged.theme].append(merged)
    for theme, group in named.items():
        group.sort(key=lambda m: (-weight_score(m.weight), m.storyline_id))
        folded[theme].extend(group[keep_per_theme:])
        del group[keep_per_theme:]

    stats: dict[str, int] = {"model_calls": 0, "fallback_batches": 0}
    drafts: list[tuple[MergedStoryline, EntryDraft]] = []
    for theme, group in sorted(named.items()):
        needs_model = [m for m in group if len(m.children) > 1 or len(m.facts) > cap]
        for merged in group:
            if merged not in needs_model:
                drafts.append((merged, deterministic_rollup(merged, cap)))
        rows = [(m, _rollup_row(m, period)) for m in needs_model]
        for batch in _rollup_batches(rows):
            prompt = json.dumps(
                {
                    "edition": edition_name,
                    "theme": THEME_LABELS.get(theme, theme),
                    "period": {
                        "first_day": period.start.isoformat(),
                        "last_day": period.last_day.isoformat(),
                    },
                    "storylines": [row for _m, row in batch],
                },
                ensure_ascii=False,
                indent=1,
            )
            stats["model_calls"] += 1
            parsed = await _call_with_retry(
                log,
                f"{period.kind} {period.start} {theme}",
                provider,
                model,
                ROLLUP_SYSTEM_PROMPT.replace("FACT_CAP", str(cap)),
                prompt,
                reasoning_effort=reasoning_effort,
                headroom_tokens=headroom_tokens,
            )
            keyed = {f"sl:{m.storyline_id}": m for m, _row in batch}
            valid = (
                validate_rollup(parsed, batch=keyed, period=period, cap=cap, stats=stats)
                if parsed is not None
                else {}
            )
            if parsed is None:
                stats["fallback_batches"] += 1
            for key, merged in keyed.items():
                drafts.append((merged, valid.get(key) or deterministic_rollup(merged, cap)))

    other_cap = ROLLUP_OTHER_FACTS[period.kind]
    async with uow.transaction() as conn:
        entry_count = 0
        for merged, draft in drafts:
            refs = _cited_refs(draft)
            await _save_entry(
                conn,
                edition_id=edition_id,
                storyline_id=merged.storyline_id,
                period=period,
                draft=draft,
                weight=merged.weight,
                ref_texts={ref: merged.ref_texts[ref] for ref in refs if ref in merged.ref_texts},
                model=model,
            )
            entry_count += 1
        for theme, group in sorted(folded.items()):
            facts = select_facts([f for m in group for f in m.facts], other_cap)
            if not facts:
                continue
            first_fact = min(str(f["date"]) for m in group for f in m.facts)
            last_fact = max(str(f["date"]) for m in group for f in m.facts)
            storyline_id = await _theme_other_storyline(
                conn,
                edition_id=edition_id,
                theme=theme,
                first=dt.datetime.fromisoformat(first_fact).replace(tzinfo=dt.timezone.utc),
                last=dt.datetime.fromisoformat(last_fact).replace(tzinfo=dt.timezone.utc),
                count=0,
            )
            ref_texts: dict[str, dict[str, Any]] = {}
            for m in group:
                ref_texts.update(m.ref_texts)
            source_ids = sorted({s for m in group for s in m.weight.get("source_ids") or ()})
            draft = EntryDraft(
                storyline_key=f"sl:{storyline_id}",
                theme=theme,
                title=f"{THEME_LABELS.get(theme, theme)}: другие сообщения",
                story_ids=sorted({s for f in facts for s in f.get("story_ids") or ()}),
                facts=facts,
                method="deterministic",
                child_count=len(group),
            )
            await _save_entry(
                conn,
                edition_id=edition_id,
                storyline_id=storyline_id,
                period=period,
                draft=draft,
                weight={
                    "stories": sum(int(m.weight.get("stories") or 0) for m in group),
                    "sources": len(source_ids),
                    "reports": sum(int(m.weight.get("reports") or 0) for m in group),
                    "days": sorted({d for m in group for d in m.weight.get("days") or ()}),
                    "source_ids": source_ids,
                    "folded_storylines": len(group),
                },
                ref_texts={ref: ref_texts[ref] for ref in _cited_refs(draft) if ref in ref_texts},
                model=None,
            )
            entry_count += 1
        await _mark_period(
            conn,
            edition_id=edition_id,
            period=period,
            story_count=sum(done[(c.kind, c.start)]["story_count"] for c in children),
            entry_count=entry_count,
            model_calls=stats["model_calls"],
            coverage_start=coverage,
            metadata=dict(stats),
        )
    result = {"entries": entry_count, **stats}
    log.info("chronicle %s %s: %s", period.kind, period.start, result)
    return result


# ---------------------------------------------------------------------------
# Maintenance


async def first_story_day(conn: Any, edition_id: int, tz: dt.tzinfo) -> dt.date | None:
    cursor = await conn.execute(
        """
        SELECT min(sc.first_seen_at)
        FROM story_cluster_state sc
        JOIN stories s ON s.id = sc.story_id
        WHERE s.edition_id = %s AND s.current_revision_id IS NOT NULL
        """,
        (edition_id,),
    )
    row = await cursor.fetchone()
    return row[0].astimezone(tz).date() if row and row[0] else None


async def week_backlog(conn: Any, *, edition_id: int, start: dt.datetime, end: dt.datetime) -> int:
    """Revisions and Stories of the week that processing has not finished yet."""
    cursor = await conn.execute(
        """
        SELECT
            (SELECT count(*)
             FROM story_cluster_state sc
             JOIN stories s ON s.id = sc.story_id
             WHERE s.edition_id = %(edition_id)s AND sc.analysis_dirty
               AND sc.first_seen_at >= %(start)s AND sc.first_seen_at < %(end)s)
          + (SELECT count(*)
             FROM source_item_revisions sir
             JOIN source_items si ON si.id = sir.source_item_id
             JOIN source_editions se
               ON se.source_id = si.source_id AND se.edition_id = %(edition_id)s
             JOIN event_revision_processing_state erps
               ON erps.source_item_revision_id = sir.id
             WHERE erps.status IN ('pending', 'running')
               AND COALESCE(si.published_at, si.first_collected_at, sir.collected_at) >= %(start)s
               AND COALESCE(si.published_at, si.first_collected_at, sir.collected_at) < %(end)s)
        """,
        {"edition_id": edition_id, "start": start, "end": end},
    )
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


@dataclass
class DuePeriods:
    weeks: list[ChroniclePeriod]
    rollups: list[ChroniclePeriod]
    blocked_week: ChroniclePeriod | None = None
    backlog: int = 0


async def due_periods(conn: Any, *, edition_id: int, now: dt.datetime, tz: dt.tzinfo) -> DuePeriods:
    """Closed weeks ready to build, in order, and rollups whose children are complete."""
    first = await first_story_day(conn, edition_id, tz)
    if first is None:
        return DuePeriods([], [])
    done = await completed_periods(conn, edition_id)
    today = now.astimezone(tz).date()
    weeks: list[ChroniclePeriod] = []
    blocked: ChroniclePeriod | None = None
    backlog = 0
    for segment in periods_between("week", first, today):
        if ("week", segment.start) in done:
            continue
        start, end = period_bounds(segment, tz)
        if now < end + WEEK_GRACE:
            break
        backlog = await week_backlog(conn, edition_id=edition_id, start=start, end=end)
        if backlog and now < end + MAX_BACKLOG_WAIT:
            blocked = segment
            break
        weeks.append(segment)
    rollups: list[ChroniclePeriod] = []
    for kind in ("month", "quarter", "half_year", "year"):
        for period in periods_between(kind, first, today):
            if period.end > today or (kind, period.start) in done:
                continue
            rollups.append(period)
    return DuePeriods(weeks, rollups, blocked, backlog)


async def advance_chronicle(
    *,
    uow: Any,
    provider: Any,
    model: str | None,
    edition_id: int,
    now: dt.datetime,
    max_weeks: int = 1,
    max_rollups: int = 2,
    reasoning_effort: str | None = "none",
    headroom_tokens: int = 0,
    log: logging.Logger = logger,
) -> dict[str, Any]:
    """Build due weeks in order, add late Stories, then roll up completed periods."""
    async with uow.transaction() as conn:
        _name, tz = await _edition(conn, edition_id)
        due = await due_periods(conn, edition_id=edition_id, now=now, tz=tz)
    if due.blocked_week is not None:
        log.info(
            "chronicle week %s waits for processing: %s items pending",
            due.blocked_week.start,
            due.backlog,
        )
    summary: dict[str, Any] = {"weeks": [], "rollups": [], "late_linked": 0}
    for week in due.weeks[:max_weeks]:
        await build_week(
            uow=uow,
            provider=provider,
            model=model,
            edition_id=edition_id,
            period=week,
            reasoning_effort=reasoning_effort,
            headroom_tokens=headroom_tokens,
            log=log,
        )
        summary["weeks"].append(week.start.isoformat())
    summary["late_linked"] = await link_late_stories(
        uow=uow, edition_id=edition_id, now=now, log=log
    )
    built = 0
    for period in due.rollups:
        if built >= max_rollups:
            break
        result = await build_rollup(
            uow=uow,
            provider=provider,
            model=model,
            edition_id=edition_id,
            period=period,
            reasoning_effort=reasoning_effort,
            headroom_tokens=headroom_tokens,
            log=log,
        )
        if result is not None:
            summary["rollups"].append(f"{period.kind}:{period.start.isoformat()}")
            built += 1
    return summary
