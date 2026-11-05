"""Period reviews built on the storyline chronicle (AGENTS.md §0.12).

A monthly, quarterly, half-year or yearly review is the city-life long read of
one closed calendar period. Its material is not a snapshot of every Story in a
long window: it is the set of PUBLISH supports that the period's chronicle
entries cite. The chronicle decides which storylines lead (weight across the
period); the writer still cites only primary-source supports, and the
Evidence Boundary is unchanged.

The review plan is computed once, when candidates are sealed, and stored in
the publication run metadata so selection, the editorial adapter and frozen
replays all read the same storylines, depths and fragment refs.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from src.publication.chronicle import (
    CHRONICLE_VERSION,
    THEME_LABELS,
    ChroniclePeriod,
    StoredEntry,
    _zone,
    first_story_day,
    load_entries,
    period_bounds,
    period_for,
    weight_score,
)

PERIOD_REVIEW_KINDS: dict[str, str] = {
    "monthly_article": "month",
    "quarterly_article": "quarter",
    "half_year_article": "half_year",
    "yearly_article": "year",
}
PERIOD_REVIEW_TYPES: frozenset[str] = frozenset(PERIOD_REVIEW_KINDS)
PERIOD_REVIEW_PLAN_VERSION = "period-review-v1"

# A week closes at most 48 hours after its end and rollups follow every two
# hours, so the review waits for the chronicle instead of failing at the slot.
PERIOD_REVIEW_DEADLINE_MINUTES = 72 * 60

PERIOD_REVIEW_WORDS: dict[str, int] = {
    "month": 3500,
    "quarter": 4000,
    "half_year": 4500,
    "year": 5000,
}
DEVELOP_STORYLINES: dict[str, int] = {"month": 5, "quarter": 6, "half_year": 7, "year": 8}
WEAVE_STORYLINES: dict[str, int] = {"month": 15, "quarter": 18, "half_year": 20, "year": 24}
FACTS_BY_DEPTH: dict[str, int] = {"DEVELOP": 12, "WEAVE": 6, "BRIEF": 3}
# Facts a storyline keeps before the budget may cut it further.
FACT_FLOORS: dict[str, int] = {"DEVELOP": 6, "WEAVE": 2, "BRIEF": 1}
DETAILS_BY_DEPTH: dict[str, int] = {"DEVELOP": 2, "WEAVE": 1, "BRIEF": 0}
TRAJECTORY_CHARS: dict[str, int] = {"DEVELOP": 900, "WEAVE": 400, "BRIEF": 0}
REFS_PER_FACT = 3
# Estimated writer material (fact + source text plus packet overhead). The
# dossier keeps its own 999,999-character ceiling; this budget keeps a review
# well inside it by trimming the lowest-ranked storylines' facts first.
MATERIAL_CHAR_BUDGET = 520_000
_REF_OVERHEAD_CHARS = 250
_STORY_OVERHEAD_CHARS = 250
_CLIPPED_SOURCE_CHARS = 400
_CLIPPED_SOURCE_ESTIMATE = 800

_MONTHS_NOMINATIVE = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
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
_QUARTERS_GENITIVE = ("I квартала", "II квартала", "III квартала", "IV квартала")
_HALVES_GENITIVE = ("первого полугодия", "второго полугодия")
_KIND_NAMES = {
    "month": "monthly",
    "quarter": "quarterly",
    "half_year": "half-year",
    "year": "yearly",
}
_PERIOD_NOUNS = {"month": "month", "quarter": "quarter", "half_year": "half-year", "year": "year"}


def is_period_review(publication_type: str | None) -> bool:
    return publication_type in PERIOD_REVIEW_TYPES


def review_zone(name: str | None) -> dt.tzinfo:
    """The edition's local zone (UTC when the profile names an invalid zone)."""
    return _zone(name)


# ---------------------------------------------------------------------------
# Period and window


def review_period(publication_type: str, slot_at: dt.datetime, tz: dt.tzinfo) -> ChroniclePeriod:
    """The closed calendar period a review published at ``slot_at`` covers."""
    kind = PERIOD_REVIEW_KINDS[publication_type]
    current = period_for(kind, slot_at.astimezone(tz).date())
    return period_for(kind, current.start - dt.timedelta(days=1))


def review_lookback_hours(publication_type: str, slot_at: dt.datetime, tz: dt.tzinfo) -> int:
    """Hours from the period's local start to the slot, so the window holds the period."""
    start, _end = period_bounds(review_period(publication_type, slot_at, tz), tz)
    return max(1, math.ceil((slot_at - start).total_seconds() / 3600))


async def edition_zone(conn: Any, edition_id: int) -> dt.tzinfo:
    cursor = await conn.execute("SELECT timezone FROM editions WHERE id = %s", (edition_id,))
    row = await cursor.fetchone()
    return review_zone(row[0] if row else None)


async def period_complete(conn: Any, *, edition_id: int, period: ChroniclePeriod) -> bool:
    cursor = await conn.execute(
        """
        SELECT 1 FROM chronicle_periods
        WHERE edition_id = %s AND period_kind = %s AND period_start = %s
          AND chronicle_version = %s
        """,
        (edition_id, period.kind, period.start, CHRONICLE_VERSION),
    )
    return await cursor.fetchone() is not None


async def period_review_ready(
    conn: Any, *, edition_id: int, publication_type: str, slot_at: dt.datetime
) -> bool:
    """A review is ready once the chronicle marks its whole period complete."""
    tz = await edition_zone(conn, edition_id)
    period = review_period(publication_type, slot_at, tz)
    return await period_complete(conn, edition_id=edition_id, period=period)


# ---------------------------------------------------------------------------
# Labels


def _period_genitive(period: ChroniclePeriod) -> str:
    year = period.start.year
    if period.kind == "month":
        return f"{_MONTHS_GENITIVE[period.start.month - 1]} {year} года"
    if period.kind == "quarter":
        return f"{_QUARTERS_GENITIVE[(period.start.month - 1) // 3]} {year} года"
    if period.kind == "half_year":
        return f"{_HALVES_GENITIVE[0 if period.start.month <= 6 else 1]} {year} года"
    return f"{year} года"


def _coverage_span(start: dt.date, last: dt.date) -> str:
    if start.day == 1:
        if start.month == last.month:
            return _MONTHS_NOMINATIVE[start.month - 1]
        return f"{_MONTHS_NOMINATIVE[start.month - 1]}–{_MONTHS_NOMINATIVE[last.month - 1]}"
    if start.month == last.month:
        return f"{start.day}–{last.day} {_MONTHS_GENITIVE[last.month - 1]}"
    return (
        f"{start.day} {_MONTHS_GENITIVE[start.month - 1]} – "
        f"{last.day} {_MONTHS_GENITIVE[last.month - 1]}"
    )


def review_label(period: ChroniclePeriod, coverage_start: dt.date) -> str:
    """The phrase after «Итоги», e.g. «октября 2026 года» or «2026 года: 7 сентября – 31 декабря»."""
    label = _period_genitive(period)
    if coverage_start > period.start:
        label += f": {_coverage_span(coverage_start, period.last_day)}"
    return label


def review_writer_note(plan: Mapping[str, Any]) -> str:
    """System-prompt note naming the period and, for a partial period, its real span."""
    kind = str(plan.get("kind") or "")
    label = str(plan.get("label") or "")
    note = (
        f"This is the {_KIND_NAMES.get(kind, 'period')} review of a closed calendar period, "
        "not a daily or weekly report. Name the period in the title and lead, for example "
        f"«Итоги {label}». The storylines that ran longest and reached the most residents lead "
        "the article; smaller storylines stay compact. Show how situations changed across the "
        "period where the evidence dates support it, and do not frame the lead as a snapshot "
        "of the last day."
    )
    if plan.get("partial"):
        start = dt.date.fromisoformat(str(plan["coverage_start"]))
        note += (
            f" Material was collected only from {start.strftime('%d.%m.%Y')}, so this review "
            f"covers part of the {_PERIOD_NOUNS.get(kind, 'period')}: say so plainly in the lead "
            "and never imply that the whole period is covered."
        )
    return note


# ---------------------------------------------------------------------------
# Review plan


def _ref_fragment_id(ref: str) -> int | None:
    head, _, tail = str(ref).partition(":")
    if head != "fragment" or not tail.isdigit():
        return None
    return int(tail)


def _ref_story_id(entry: StoredEntry, ref: str) -> int | None:
    meta = entry.ref_texts.get(ref)
    if not isinstance(meta, Mapping):
        return None
    try:
        return int(meta.get("story_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ref_cost(entry: StoredEntry, ref: str) -> int:
    meta = entry.ref_texts.get(ref)
    if not isinstance(meta, Mapping):
        return _REF_OVERHEAD_CHARS
    source = str(meta.get("source") or "")
    source_cost = _CLIPPED_SOURCE_ESTIMATE if len(source) >= _CLIPPED_SOURCE_CHARS else len(source)
    return len(str(meta.get("evidence") or "")) + source_cost + _REF_OVERHEAD_CHARS


def _usable_refs(entry: StoredEntry, refs: Sequence[Any], limit: int) -> list[str]:
    out: list[str] = []
    for ref in refs or ():
        ref = str(ref)
        if ref in out or _ref_fragment_id(ref) is None or _ref_story_id(entry, ref) is None:
            continue
        out.append(ref)
        if len(out) >= limit:
            break
    return out


def _depths(entries: Sequence[StoredEntry], kind: str) -> dict[int, str]:
    """DEVELOP/WEAVE/BRIEF by storyline weight; «other news» entries stay BRIEF."""
    named = sorted(
        (e for e in entries if e.storyline_kind == "storyline"),
        key=lambda e: (-weight_score(e.weight), e.title, e.storyline_id),
    )
    develop = DEVELOP_STORYLINES.get(kind, 5)
    weave = WEAVE_STORYLINES.get(kind, 15)
    depths: dict[int, str] = {}
    for index, entry in enumerate(named):
        depths[entry.storyline_id] = (
            "DEVELOP" if index < develop else "WEAVE" if index < develop + weave else "BRIEF"
        )
    for entry in entries:
        depths.setdefault(entry.storyline_id, "BRIEF")
    return depths


def _ranked(entries: Sequence[StoredEntry], depths: Mapping[int, str]) -> list[StoredEntry]:
    order = {"DEVELOP": 0, "WEAVE": 1, "BRIEF": 2}
    return sorted(
        entries,
        key=lambda e: (
            order[depths[e.storyline_id]],
            e.storyline_kind != "storyline",
            -weight_score(e.weight),
            e.title,
            e.storyline_id,
        ),
    )


def _choose_facts(entry: StoredEntry, depth: str) -> list[list[str]]:
    """Fact ref groups for one storyline: best-sourced facts, then in date order."""
    candidates: list[tuple[int, str, int, list[str]]] = []
    for index, fact in enumerate(entry.facts):
        if not isinstance(fact, Mapping):
            continue
        refs = _usable_refs(entry, fact.get("refs") or (), REFS_PER_FACT)
        if refs:
            candidates.append((len(refs), str(fact.get("date") or ""), index, refs))
    best = sorted(candidates, key=lambda c: (-c[0], c[1], c[2]))[: FACTS_BY_DEPTH[depth]]
    groups = [c[3] for c in sorted(best, key=lambda c: (c[1], c[2]))]
    for detail in entry.details[: DETAILS_BY_DEPTH[depth]]:
        if isinstance(detail, Mapping):
            refs = _usable_refs(entry, detail.get("refs") or (), 2)
            if refs:
                groups.append(refs)
    return groups


def _trim_to_budget(
    ranked: Sequence[StoredEntry],
    chosen: dict[int, list[list[str]]],
    budget: int,
    depths: Mapping[int, str],
) -> int:
    """Drop facts from the lowest-ranked storylines until the material fits.

    Storylines first shrink to their depth's floor, so leading storylines keep
    room to develop; only then may any storyline shrink to a single fact.
    """
    by_id = {e.storyline_id: e for e in ranked}
    ref_uses: dict[str, int] = defaultdict(int)
    story_uses: dict[int, int] = defaultdict(int)
    ref_cost: dict[str, int] = {}
    ref_story: dict[str, int] = {}
    for sid, groups in chosen.items():
        entry = by_id[sid]
        for group in groups:
            for ref in group:
                ref_uses[ref] += 1
                ref_cost.setdefault(ref, _ref_cost(entry, ref))
                story = _ref_story_id(entry, ref)
                if story is not None:
                    ref_story.setdefault(ref, story)
    for ref, story in ref_story.items():
        story_uses[story] += ref_uses[ref]
    total = sum(ref_cost.values()) + _STORY_OVERHEAD_CHARS * len(story_uses)
    order = [e.storyline_id for e in reversed(ranked)]
    while total > budget:
        victim = next(
            (sid for sid in order if len(chosen.get(sid, ())) > FACT_FLOORS[depths[sid]]),
            None,
        )
        if victim is None:
            victim = next((sid for sid in order if len(chosen.get(sid, ())) > 1), None)
        if victim is None:
            break
        groups = chosen[victim]
        weakest = min(range(len(groups)), key=lambda i: (len(groups[i]), -i))
        for ref in groups.pop(weakest):
            ref_uses[ref] -= 1
            story = ref_story.get(ref)
            if story is not None:
                story_uses[story] -= 1
                if story_uses[story] == 0:
                    total -= _STORY_OVERHEAD_CHARS
                    del story_uses[story]
            if ref_uses[ref] == 0:
                total -= ref_cost[ref]
                del ref_uses[ref]
    return total


async def build_review_plan(
    conn: Any, *, edition_id: int, period: ChroniclePeriod, tz: dt.tzinfo
) -> dict[str, Any]:
    """Storylines, depths and cited fragments of one completed chronicle period."""
    entries = await load_entries(conn, edition_id=edition_id, periods=[period])
    first_day = await first_story_day(conn, edition_id, tz)
    coverage_start = max(period.start, first_day) if first_day else period.start
    depths = _depths(entries, period.kind)
    ranked = _ranked(entries, depths)
    chosen = {e.storyline_id: _choose_facts(e, depths[e.storyline_id]) for e in ranked}
    estimated_chars = _trim_to_budget(ranked, chosen, MATERIAL_CHAR_BUDGET, depths)

    storylines: list[dict[str, Any]] = []
    fragments_by_story: dict[int, set[int]] = defaultdict(set)
    for rank, entry in enumerate(ranked, start=1):
        depth = depths[entry.storyline_id]
        story_ids: list[int] = []
        for group in chosen[entry.storyline_id]:
            for ref in group:
                story = _ref_story_id(entry, ref)
                fragment = _ref_fragment_id(ref)
                if story is None or fragment is None:
                    continue
                fragments_by_story[story].add(fragment)
                if story not in story_ids:
                    story_ids.append(story)
        if not story_ids:
            continue
        weight = entry.weight or {}
        trajectory_limit = TRAJECTORY_CHARS[depth]
        storylines.append(
            {
                "storyline_id": entry.storyline_id,
                "theme": entry.theme,
                "kind": entry.storyline_kind,
                "title": entry.title,
                "depth": depth,
                "rank": rank,
                "score": weight_score(weight),
                "days": len(weight.get("days") or ()),
                "sources": int(weight.get("sources") or 0),
                "stories": int(weight.get("stories") or 0),
                "trajectory": (entry.trajectory or "")[:trajectory_limit]
                if trajectory_limit
                else "",
                "fact_count": len(chosen[entry.storyline_id]),
                "story_ids": story_ids,
            }
        )
    return {
        "version": PERIOD_REVIEW_PLAN_VERSION,
        "chronicle_version": CHRONICLE_VERSION,
        "kind": period.kind,
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "partial": coverage_start > period.start,
        "label": review_label(period, coverage_start),
        "estimated_chars": estimated_chars,
        "storylines": storylines,
        "fragments_by_story": {
            str(story): sorted(fragments) for story, fragments in sorted(fragments_by_story.items())
        },
    }


def run_review_plan(run: Any) -> dict[str, Any] | None:
    """The review plan frozen into a publication run, if it is a period review."""
    if run is None or not is_period_review(getattr(run, "publication_type", None)):
        return None
    metadata = getattr(run, "metadata", None) or {}
    plan = metadata.get("period_review") if isinstance(metadata, Mapping) else None
    return plan if isinstance(plan, dict) else None


def review_window_hours(plan: Mapping[str, Any], snapshot_at: dt.datetime, tz: dt.tzinfo) -> int:
    """Hours from the reviewed period's local start to the run snapshot."""
    period = ChroniclePeriod(
        str(plan["kind"]),
        dt.date.fromisoformat(str(plan["period_start"])),
        dt.date.fromisoformat(str(plan["period_end"])),
    )
    start, _end = period_bounds(period, tz)
    return max(1, math.ceil((snapshot_at - start).total_seconds() / 3600))


def review_fragments(plan: Mapping[str, Any], story_id: int) -> set[int]:
    raw = (plan.get("fragments_by_story") or {}).get(str(story_id)) or ()
    return {int(fid) for fid in raw}


# ---------------------------------------------------------------------------
# Sealing


_REVIEW_REVISIONS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (sr.story_id) sr.story_id, sr.id, sr.event_payload
    FROM story_revisions sr
    WHERE sr.story_id = ANY(%(story_ids)s::bigint[]) AND sr.created_at <= %(snapshot_at)s
    ORDER BY sr.story_id, sr.created_at DESC, sr.id DESC
)
SELECT l.story_id, l.id, l.event_payload->>'publishability', l.event_payload->>'urgency'
FROM latest l
JOIN stories s ON s.id = l.story_id AND s.edition_id = %(edition_id)s
WHERE l.event_payload IS NOT NULL
  AND COALESCE(l.event_payload->>'publishability', 'news') IN ('news', 'brief')
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
          CASE WHEN jsonb_typeof(l.event_payload->'evidence_items') = 'array'
               THEN l.event_payload->'evidence_items' ELSE '[]'::jsonb END) AS e
      WHERE e->>'publication_use' = 'PUBLISH'
  )
  AND (
      SELECT d.scope_class
      FROM story_edition_scope_decisions d
      WHERE d.story_id = l.story_id AND d.edition_id = %(edition_id)s
        AND d.created_at <= %(snapshot_at)s
      ORDER BY d.created_at DESC, d.id DESC
      LIMIT 1
  ) IN ('LOCAL', 'DIRECT_IMPACT')
  AND COALESCE((
      SELECT t.retention
      FROM story_event_triage_decisions t
      WHERE t.story_id = l.story_id AND t.created_at <= %(snapshot_at)s
      ORDER BY t.created_at DESC, t.id DESC
      LIMIT 1
  ), 'KEEP') = 'KEEP'
"""


async def seal_period_review(conn: Any, repo: Any, run: Any) -> list[Any]:
    """Seal the Stories a completed chronicle period cites, in storyline order.

    Stories keep the revision and Gate decision they had when they were
    chronicled: a review spans several Gate versions and must not drop a
    month of knowledge because the current Gate version changed since.
    """
    tz = await edition_zone(conn, run.edition_id)
    raw_slot = (run.metadata or {}).get("scheduled_slot_at")
    slot_at = dt.datetime.fromisoformat(raw_slot) if raw_slot else run.snapshot_at
    period = review_period(run.publication_type, slot_at, tz)
    if not await period_complete(conn, edition_id=run.edition_id, period=period):
        raise ValueError(
            f"chronicle {period.kind} {period.start} is not complete for edition {run.edition_id}"
        )
    plan = await build_review_plan(conn, edition_id=run.edition_id, period=period, tz=tz)
    order = [sid for line in plan["storylines"] for sid in line["story_ids"]]
    order = list(dict.fromkeys(order))
    cursor = await conn.execute(
        _REVIEW_REVISIONS_SQL,
        {"story_ids": order, "snapshot_at": run.snapshot_at, "edition_id": run.edition_id},
    )
    revisions = {int(row[0]): (int(row[1]), row[2], row[3]) for row in await cursor.fetchall()}
    plan["sealed_story_count"] = sum(1 for sid in order if sid in revisions)
    await conn.execute(
        """
        UPDATE publication_runs
        SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('period_review', %s::jsonb)
        WHERE id = %s
        """,
        (_json(plan), run.id),
    )
    candidates = []
    rank = 0
    for story_id in order:
        revision = revisions.get(story_id)
        if revision is None:
            continue
        rank += 1
        revision_id, publishability, urgency = revision
        candidates.append(
            await repo.insert_candidate(
                conn,
                run.id,
                story_id=story_id,
                story_revision_id=revision_id,
                deterministic_rank=rank,
                snapshot_features={
                    "knowledge_source": "event_first",
                    "publishability": publishability,
                    "urgency": urgency,
                    "period_review": True,
                },
            )
        )
    await repo.transition_run(conn, run.id, "candidates_sealed")
    return candidates


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Writer material


def build_review_coverage_plan(plan: Mapping[str, Any], context: Any) -> Any:
    """Chapters by theme and Story depth by storyline weight."""
    from src.publication.article_coverage import (
        ArticleCoveragePlan,
        ArticleStoryAssignment,
        ArticleStoryCoverage,
        ArticleThematicSection,
        _detail_support_ids,
        _publish_supports_by_story,
    )

    supports_by_story = _publish_supports_by_story(context)
    cards = {card.id: card for card in context.story_cards}
    stories: list[ArticleStoryCoverage] = []
    assignments: dict[str, list[ArticleStoryAssignment]] = defaultdict(list)
    titles: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    rank = 0

    def add(story_key: str, theme: str, depth: Any, topic_fallback: str) -> None:
        nonlocal rank
        supports = supports_by_story.get(story_key, ())
        if story_key in seen or not supports:
            return
        seen.add(story_key)
        rank += 1
        card = cards.get(story_key)
        support_ids = tuple(s.support_id for s in supports)
        stories.append(
            ArticleStoryCoverage(
                story_id=story_key,
                topic=card.topic if card else topic_fallback,
                rank=rank,
                prominence=depth,
                support_ids=support_ids,
                detail_support_ids=_detail_support_ids(supports, depth),
            )
        )
        assignments[theme].append(
            ArticleStoryAssignment(
                story_id=story_key,
                section_id=f"review:{theme}",
                depth=depth,
                rank=rank,
                primary_evidence_ids=support_ids,
            )
        )

    for line in plan.get("storylines") or ():
        theme = str(line.get("theme") or "other")
        before = rank
        for sid in line.get("story_ids") or ():
            add(f"story:{sid}", theme, line.get("depth") or "BRIEF", str(line.get("title") or ""))
        if rank > before and line.get("kind") == "storyline":
            titles[theme].append(f"«{line.get('title')}» ({line.get('depth')})")
    # A sealed Story always has a chronicle storyline; keep any stray card visible.
    for card in context.story_cards:
        add(card.id, "other", "BRIEF", card.topic)

    sections = []
    for theme, theme_assignments in assignments.items():
        named = titles.get(theme) or []
        intent = (
            "Сюжеты главы по весу за период: " + "; ".join(named[:6]) + "."
            if named
            else "Короткие сообщения темы за период."
        )
        sections.append(
            ArticleThematicSection(
                section_id=f"review:{theme}",
                title=THEME_LABELS.get(theme, THEME_LABELS["other"]),
                lead_story_id=theme_assignments[0].story_id,
                story_assignments=tuple(theme_assignments),
                narrative_intent=intent,
            )
        )
    return ArticleCoveragePlan(stories=tuple(stories), sections=tuple(sections))


def render_review_outline(plan: Mapping[str, Any], coverage_plan: Any) -> str:
    """Non-citable storyline navigation for the writer."""
    visible = set(coverage_plan.story_ids)
    lines = [
        "PERIOD REVIEW STORYLINES (navigation only, not evidence; cite facts only from the "
        "evidence inventory).",
        f"Period: {plan.get('period_start')} .. {plan.get('period_end')} (end exclusive); "
        f"collected material starts {plan.get('coverage_start')}.",
        "Each storyline is one continuing situation of the period, ranked by how many days it "
        "ran and how many sources reported it. DEVELOP storylines lead their chapters, WEAVE "
        "storylines get a compact passage, BRIEF items get one mention or join the chapter's "
        "round-up; small chapters may share one heading. A trajectory is a non-citable "
        "orientation summary of the same sources: never "
        "quote it, and never state a number, date, name or place from it unless an evidence "
        "record cited for that sentence contains it.",
    ]
    current_theme = None
    by_theme: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for line in plan.get("storylines") or ():
        by_theme[str(line.get("theme") or "other")].append(line)
    for theme, theme_lines in by_theme.items():
        for line in theme_lines:
            story_keys = [f"story:{sid}" for sid in line.get("story_ids") or ()]
            story_keys = [key for key in story_keys if key in visible]
            if not story_keys:
                continue
            if theme != current_theme:
                lines.append(f"\nCHAPTER {THEME_LABELS.get(theme, THEME_LABELS['other'])}")
                current_theme = theme
            title = (
                "other news of the theme"
                if line.get("kind") == "theme_other"
                else f"«{line.get('title')}»"
            )
            lines.append(
                f"- {line.get('depth')} {title}: {line.get('days')} active days, "
                f"{line.get('sources')} sources, {line.get('stories')} Stories in the period"
            )
            trajectory = str(line.get("trajectory") or "").strip()
            if trajectory:
                lines.append(f"  trajectory (non-citable): {trajectory}")
            lines.append(f"  stories: {', '.join(story_keys)}")
    return "\n".join(lines)
