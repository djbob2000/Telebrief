"""Running-story memory: what a local newsroom already knows about long situations.

A digest reads one reporting window. A resident, and a professional journalist,
also know the background: power has been unstable since August, a substation
was reported damaged, nobody has named a repair date. Without that memory a
24-hour digest writes every outage as if it were new.

The memory follows established agent-memory practice:

* running stories, not messages: a small set of long-lived situations;
* incremental CRUD updates: the model sees the current memory plus newly revised
  Stories and returns ADD/UPDATE/RESOLVE operations instead of rewriting it;
* temporal bookkeeping: ``first_seen_at`` / ``last_confirmed_at``, and
  situations age out when no new evidence confirms them;
* grounded fields: every statement cites source fragments, and concrete numbers
  and dates must occur in those fragments, otherwise the field is dropped;
* immutable snapshots, so a publication can name the exact memory it used.

The memory is newsroom background, never citable digest evidence: publication
coverage, Evidence Boundary and fact ownership are unchanged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MEMORY_VERSION = "situation-memory-v2-digest-line"
MAX_SITUATIONS = 10
STALE_AFTER_DAYS = 21
BOOTSTRAP_DAYS = 30
BOOTSTRAP_CHUNK_DAYS = 3
MAX_INPUT_STORIES = 220
MAX_INPUT_CHARS = 110_000
MAX_REFS_PER_SITUATION = 40
_REF_TEXT_CHARS = 280
_SERVICES = (
    "power",
    "water",
    "gas",
    "heating",
    "connectivity",
    "transport",
    "safety",
    "services",
    "other",
)
_STATUSES = ("ongoing", "improving", "worsening", "resolved")
_REF_RE = re.compile(r"^fragment:\d+$")
_DATE_LIKE_RE = re.compile(
    r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryReport:
    """One citable source fragment offered to the memory update."""

    ref: str
    story_id: int
    observed_at: dt.datetime
    evidence_text: str
    source_text: str

    def check_texts(self) -> tuple[str, ...]:
        return tuple(text for text in (self.evidence_text, self.source_text) if text)


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable memory version as stored in the database."""

    snapshot_id: int | None
    edition_id: int
    as_of: dt.datetime
    situations: tuple[dict[str, Any], ...]
    ref_texts: dict[str, dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prompt


MEMORY_SYSTEM_PROMPT = """You maintain the running-story memory of a local newsroom.

The memory lists the few long-running city situations a resident already knows
about: chronic outages, damaged infrastructure, supply schedules, ongoing safety
conditions, a closed service. A digest uses it only as background to judge what
is new today. Keep it small (at most 10 situations), concrete and faithful.

You receive the current memory and community/official reports from Stories that
were revised since the last update. Return JSON:
{"operations":[{
  "op":"ADD|UPDATE|RESOLVE",
  "situation_id":"existing id for UPDATE/RESOLVE",
  "title":"short Russian label, e.g. «Перебои с электроснабжением»",
  "service":"power|water|gas|heating|connectivity|transport|safety|services|other",
  "areas":["at most 6 main districts/places named by the reports; no dates or streets lists"],
  "status":"ongoing|improving|worsening|resolved",
  "since_text":"Russian phrase for when it began, only if a report states it, with attribution",
  "since_refs":["fragment:ID"],
  "summary":"1–2 Russian sentences: the situation as residents know it, attributed",
  "summary_refs":["fragment:ID"],
  "causes":[{"text":"attributed cause, only as reported","refs":["fragment:ID"]}],
  "latest_change":{"text":"what changed in these new reports","refs":["fragment:ID"]},
  "open_questions":["what residents ask that no report answers, e.g. сроки ремонта"],
  "digest_line":"one standalone attributed Russian background sentence for a daily digest",
  "digest_line_refs":["fragment:ID"]
}]}

Rules:
- Use only the supplied reports. Every text field cites the fragment refs that state it.
- A situation is long-running: a single one-off event belongs in the daily digest,
  not in memory, unless reports show it has a lasting consequence.
- Preserve epistemic status: «по словам жителей», «по сообщению водоканала». A
  question, sarcasm or a guess is not a fact; do not turn «наверное» into a cause.
- Do not invent dates, durations, causes, repair plans or city-wide scope. Quote
  only numbers and dates that occur in the cited reports.
- Prefer UPDATE of an existing situation over a near-duplicate ADD. RESOLVE only
  when reports state that the situation ended.
- Omit operations for situations the new reports do not touch; they stay unchanged.
- digest_line (for every ADD/UPDATE): one short sentence (at most 160 characters) a
  journalist could put before today's news, e.g. «Перебои с электроснабжением, по
  словам жителей, продолжаются с начала августа.» Attribute it, keep only what the
  cited reports state, use absolute dates rather than «сегодня/вчера» or day counts
  such as «65-й день» that would be wrong tomorrow.
- Write all text in Russian.
"""


# ---------------------------------------------------------------------------
# Database


async def load_latest_snapshot(
    conn: Any, *, edition_id: int, as_of: dt.datetime
) -> MemorySnapshot | None:
    """Return the newest memory snapshot available at ``as_of``."""
    cursor = await conn.execute(
        """
        SELECT id, as_of, situations, metadata
        FROM edition_situation_memory_snapshots
        WHERE edition_id = %s AND as_of <= %s
        ORDER BY as_of DESC, id DESC
        LIMIT 1
        """,
        (edition_id, as_of),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    metadata = row[3] if isinstance(row[3], Mapping) else {}
    return MemorySnapshot(
        snapshot_id=int(row[0]),
        edition_id=edition_id,
        as_of=row[1],
        situations=tuple(row[2] or ()),
        ref_texts=dict(metadata.get("ref_texts") or {}),
    )


async def load_memory_reports(
    conn: Any,
    *,
    edition_id: int,
    since: dt.datetime,
    until: dt.datetime,
    limit: int = MAX_INPUT_STORIES,
) -> list[tuple[int, str, list[MemoryReport]]]:
    """Load in-scope Stories revised in the window with their PUBLISH evidence."""
    cursor = await conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (sr.story_id)
                sr.story_id, sr.created_at, sr.event_payload
            FROM story_revisions sr
            JOIN stories s ON s.id = sr.story_id
            WHERE s.edition_id = %s
              AND sr.created_at >= %s AND sr.created_at < %s
              AND sr.event_payload IS NOT NULL
            ORDER BY sr.story_id, sr.created_at DESC, sr.id DESC
        )
        SELECT l.story_id, l.event_payload
        FROM latest l
        WHERE COALESCE(l.event_payload->>'publishability', 'news') IN ('news', 'brief')
          AND (
              SELECT d.scope_class
              FROM story_edition_scope_decisions d
              WHERE d.story_id = l.story_id AND d.edition_id = %s
              ORDER BY d.created_at DESC, d.id DESC
              LIMIT 1
          ) IN ('LOCAL', 'DIRECT_IMPACT')
        ORDER BY l.created_at DESC
        LIMIT %s
        """,
        (edition_id, since, until, edition_id, limit),
    )
    rows = await cursor.fetchall()
    stories: list[tuple[int, str, list[tuple[str, int]]]] = []
    fragment_ids: set[int] = set()
    for story_id, payload in rows:
        if not isinstance(payload, Mapping):
            continue
        headline = str(payload.get("headline") or payload.get("topic") or "").strip()
        evidence: list[tuple[str, int]] = []
        for item in payload.get("evidence_items") or ():
            if not isinstance(item, Mapping) or item.get("publication_use") != "PUBLISH":
                continue
            text = " ".join(str(item.get("text") or "").split())
            for fragment_id in item.get("source_fragment_ids") or ():
                if isinstance(fragment_id, int) and text:
                    evidence.append((text, fragment_id))
                    fragment_ids.add(fragment_id)
        if evidence:
            stories.append((int(story_id), headline, evidence))
    if not fragment_ids:
        return []
    cursor = await conn.execute(
        """
        SELECT f.id, f.text_content,
               COALESCE(si.published_at, si.first_collected_at, f.created_at)
        FROM source_fragments f
        JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
        JOIN source_items si ON si.id = sir.source_item_id
        WHERE f.id = ANY(%s)
        """,
        (sorted(fragment_ids),),
    )
    fragments = {
        int(fid): (" ".join(str(text or "").split()), observed)
        for fid, text, observed in await cursor.fetchall()
    }
    output = []
    for story_id, headline, evidence in stories:
        reports = []
        seen: set[str] = set()
        for text, fragment_id in evidence:
            source = fragments.get(fragment_id)
            if source is None or source[1] is None or source[1] > until:
                continue
            ref = f"fragment:{fragment_id}"
            if ref in seen:
                continue
            seen.add(ref)
            reports.append(
                MemoryReport(
                    ref=ref,
                    story_id=story_id,
                    observed_at=source[1],
                    evidence_text=text[:_REF_TEXT_CHARS],
                    source_text=source[0][:_REF_TEXT_CHARS],
                )
            )
        if reports:
            output.append((story_id, headline, reports))
    return output


async def save_snapshot(
    conn: Any,
    *,
    edition_id: int,
    previous: MemorySnapshot | None,
    as_of: dt.datetime,
    since: dt.datetime,
    model: str | None,
    situations: Sequence[Mapping[str, Any]],
    ref_texts: Mapping[str, Mapping[str, str]],
    metadata: Mapping[str, Any],
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO edition_situation_memory_snapshots
            (edition_id, previous_snapshot_id, as_of, input_since, memory_version,
             model, situations, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        RETURNING id
        """,
        (
            edition_id,
            previous.snapshot_id if previous else None,
            as_of,
            since,
            MEMORY_VERSION,
            model,
            json.dumps(list(situations), ensure_ascii=False),
            json.dumps({**metadata, "ref_texts": dict(ref_texts)}, ensure_ascii=False),
        ),
    )
    row = await cursor.fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Update


def build_memory_prompt(
    *,
    edition_name: str,
    as_of: dt.datetime,
    previous: MemorySnapshot | None,
    stories: Sequence[tuple[int, str, Sequence[MemoryReport]]],
    max_chars: int = MAX_INPUT_CHARS,
) -> str:
    current = [
        {
            key: situation.get(key)
            for key in (
                "situation_id",
                "title",
                "service",
                "areas",
                "status",
                "since_text",
                "summary",
                "causes",
                "latest_change",
                "open_questions",
                "first_seen_at",
                "last_confirmed_at",
            )
        }
        for situation in (previous.situations if previous else ())
    ]
    rows: list[dict[str, Any]] = []
    used = len(json.dumps(current, ensure_ascii=False)) + 400
    for _story_id, headline, reports in stories:
        row = {
            "story": headline,
            "reports": [
                {
                    "ref": report.ref,
                    "observed_at": report.observed_at.date().isoformat(),
                    "text": report.evidence_text,
                }
                for report in reports
            ],
        }
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > max_chars:
            break
        used += size
        rows.append(row)
    return json.dumps(
        {
            "edition": edition_name,
            "as_of": as_of.date().isoformat(),
            "current_memory": current,
            "new_reports": rows,
        },
        ensure_ascii=False,
        indent=1,
    )


def _situation_id(service: str, title: str) -> str:
    digest = hashlib.sha256(f"{service}|{title.casefold().strip()}".encode()).hexdigest()
    return f"sit:{digest[:12]}"


def _clean_text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _grounded(text: str, refs: Sequence[str], texts: Mapping[str, Sequence[str]]) -> bool:
    """A field is kept only when its concrete claims occur in its own cited sources."""
    from src.publication.article_claims import find_unsupported_claims

    if not text or not refs or any(ref not in texts for ref in refs):
        return False
    supports = [support for ref in refs for support in texts[ref]]
    return not find_unsupported_claims(text, supports)


_ATTRIBUTION_RE = re.compile(
    r"\b(?:по\s+(?:словам|сообщени\w*|данным|информации)|сообща\w*|жител\w*|"
    r"утвержда\w*|пишут|рассказыва\w*)",
    re.IGNORECASE,
)
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:сегодня|вчера|завтра|сейчас|накануне|позавчера)\b|"
    r"(?:\d+\s*[-‑–]?\s*(?:й|го|ий|ый)?|\b(?:одн|дв|тр|четыр|пят|шест|сем|восьм|восем|девят|"
    r"десят|нескольк|пар|втор|трет)\w*)\s+(?:день|дня|дней|сутк\w*|недел\w*|месяц\w*)",
    re.IGNORECASE,
)


def _valid_digest_line(text: str, refs: Sequence[str], texts: Mapping[str, Sequence[str]]) -> bool:
    """A digest background line is attributed, durable and grounded in its refs."""
    return (
        0 < len(text) <= 170
        and text.endswith((".", "!", "?"))
        and bool(_ATTRIBUTION_RE.search(text))
        and not _RELATIVE_TIME_RE.search(text)
        and _grounded(text, refs, texts)
    )


def _refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(ref) for ref in value if _REF_RE.match(str(ref))]


def apply_memory_operations(
    previous: MemorySnapshot | None,
    operations: Any,
    *,
    reports: Sequence[MemoryReport],
    as_of: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, int]]:
    """Validate model operations against their sources and merge deterministically."""
    ref_texts: dict[str, dict[str, str]] = dict(previous.ref_texts) if previous else {}
    for report in reports:
        ref_texts[report.ref] = {
            "evidence_text": report.evidence_text,
            "source_text": report.source_text,
            "observed_at": report.observed_at.isoformat(),
        }
    texts = {
        ref: tuple(t for t in (row.get("evidence_text", ""), row.get("source_text", "")) if t)
        for ref, row in ref_texts.items()
    }
    situations: dict[str, dict[str, Any]] = {
        str(s["situation_id"]): dict(s) for s in (previous.situations if previous else ())
    }
    stats = {"applied": 0, "rejected": 0, "dropped_fields": 0}
    for raw in operations if isinstance(operations, list) else ():
        if not isinstance(raw, Mapping):
            stats["rejected"] += 1
            continue
        op = str(raw.get("op") or "").upper()
        title = _clean_text(raw.get("title"), 80)
        service = str(raw.get("service") or "other")
        service = service if service in _SERVICES else "other"
        existing_id = str(raw.get("situation_id") or "")
        if op in ("UPDATE", "RESOLVE") and existing_id not in situations:
            op = "ADD" if title else ""
        if op == "ADD":
            existing_id = _situation_id(service, title)
        if op not in ("ADD", "UPDATE", "RESOLVE") or (op == "ADD" and not title):
            stats["rejected"] += 1
            continue
        base = situations.get(existing_id, {})
        summary = _clean_text(raw.get("summary"))
        summary_refs = _refs(raw.get("summary_refs"))
        summary_ok = _grounded(summary, summary_refs, texts)
        if op == "ADD" and not summary_ok:
            stats["rejected"] += 1
            continue
        situation = {
            "situation_id": existing_id,
            "title": title or base.get("title", ""),
            "service": service if title else base.get("service", service),
            "areas": [
                _clean_text(area, 60)
                for area in (raw.get("areas") or base.get("areas") or [])
                if _clean_text(area, 60) and not _DATE_LIKE_RE.search(str(area))
            ][:6],
            "status": str(raw.get("status") or base.get("status") or "ongoing"),
            "since_text": base.get("since_text", ""),
            "summary": summary if summary_ok else base.get("summary", ""),
            "causes": list(base.get("causes") or []),
            "latest_change": base.get("latest_change"),
            "open_questions": list(base.get("open_questions") or []),
            "refs": list(base.get("refs") or []),
            "digest_line": base.get("digest_line", ""),
            "digest_line_refs": list(base.get("digest_line_refs") or []),
            "first_seen_at": base.get("first_seen_at"),
            "last_confirmed_at": base.get("last_confirmed_at"),
        }
        if situation["status"] not in _STATUSES:
            situation["status"] = "ongoing"
        if op == "RESOLVE":
            situation["status"] = "resolved"
        if not summary_ok and summary:
            stats["dropped_fields"] += 1
        since_text = _clean_text(raw.get("since_text"), 160)
        since_refs = _refs(raw.get("since_refs"))
        if since_text:
            if _grounded(since_text, since_refs, texts):
                situation["since_text"] = since_text
                situation["since_refs"] = since_refs
            else:
                stats["dropped_fields"] += 1
        causes = []
        for cause in raw.get("causes") or ():
            if not isinstance(cause, Mapping):
                continue
            text = _clean_text(cause.get("text"), 240)
            refs = _refs(cause.get("refs"))
            if _grounded(text, refs, texts):
                causes.append({"text": text, "refs": refs})
            elif text:
                stats["dropped_fields"] += 1
        if causes:
            situation["causes"] = causes[:3]
        change = raw.get("latest_change")
        if isinstance(change, Mapping):
            text = _clean_text(change.get("text"), 300)
            refs = _refs(change.get("refs"))
            if _grounded(text, refs, texts):
                situation["latest_change"] = {"text": text, "refs": refs}
            elif text:
                stats["dropped_fields"] += 1
        line = _clean_text(raw.get("digest_line"), 200)
        line_refs = _refs(raw.get("digest_line_refs"))
        if line:
            if _valid_digest_line(line, line_refs, texts):
                situation["digest_line"] = line
                situation["digest_line_refs"] = line_refs
            else:
                stats["dropped_fields"] += 1
        questions = [
            _clean_text(question, 160)
            for question in raw.get("open_questions") or ()
            if _clean_text(question, 160) and not re.search(r"\d", str(question))
        ]
        if questions:
            situation["open_questions"] = questions[:3]
        new_refs = [
            ref
            for ref in (
                *summary_refs,
                *since_refs,
                *(situation["digest_line_refs"] if situation.get("digest_line") else ()),
                *(ref for cause in causes for ref in cause["refs"]),
                *(
                    situation["latest_change"]["refs"]
                    if isinstance(situation.get("latest_change"), Mapping)
                    else ()
                ),
            )
            if ref in texts
        ]
        situation["refs"] = list(dict.fromkeys([*situation["refs"], *new_refs]))[
            -MAX_REFS_PER_SITUATION:
        ]
        observed = sorted(
            ref_texts[ref]["observed_at"] for ref in situation["refs"] if ref in ref_texts
        )
        if observed:
            situation["first_seen_at"] = min(
                [observed[0], *([base["first_seen_at"]] if base.get("first_seen_at") else [])]
            )
            situation["last_confirmed_at"] = observed[-1]
        situations[existing_id] = situation
        stats["applied"] += 1
    horizon = (as_of - dt.timedelta(days=STALE_AFTER_DAYS)).isoformat()
    kept = [
        s
        for s in situations.values()
        if s.get("summary") and str(s.get("last_confirmed_at") or "") >= horizon
    ]
    kept.sort(key=lambda s: str(s.get("last_confirmed_at") or ""), reverse=True)
    kept = kept[:MAX_SITUATIONS]
    used_refs = {ref for s in kept for ref in s.get("refs", ())}
    ref_texts = {ref: row for ref, row in ref_texts.items() if ref in used_refs}
    return kept, ref_texts, stats


async def update_situation_memory(
    *,
    uow: Any,
    provider: Any,
    model: str | None,
    edition_id: int,
    as_of: dt.datetime,
    log: logging.Logger = logger,
) -> int | None:
    """Advance the memory to ``as_of``; bootstrap day by day when it is empty."""
    async with uow.transaction() as conn:
        previous = await load_latest_snapshot(conn, edition_id=edition_id, as_of=as_of)
        cursor = await conn.execute("SELECT name FROM editions WHERE id = %s", (edition_id,))
        row = await cursor.fetchone()
    edition_name = str(row[0]) if row else ""
    if previous is not None:
        windows = [(previous.as_of, as_of)]
    else:
        start = as_of - dt.timedelta(days=BOOTSTRAP_DAYS)
        windows = [
            (
                start + dt.timedelta(days=day),
                min(as_of, start + dt.timedelta(days=day + BOOTSTRAP_CHUNK_DAYS)),
            )
            for day in range(0, BOOTSTRAP_DAYS, BOOTSTRAP_CHUNK_DAYS)
        ]
    snapshot_id = previous.snapshot_id if previous else None
    for since, until in windows:
        if since >= until:
            continue
        async with uow.transaction() as conn:
            stories = await load_memory_reports(
                conn, edition_id=edition_id, since=since, until=until
            )
        if not stories:
            continue
        prompt = build_memory_prompt(
            edition_name=edition_name, as_of=until, previous=previous, stories=stories
        )
        kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "reasoning_effort": "none",
            "thinking": False,
            "max_tokens": 9000,
        }
        if model:
            kwargs["model"] = model
        from src.utils import robust_extract_json

        try:
            parsed = robust_extract_json(await provider.chat_completion(**kwargs))
        except Exception as exc:  # noqa: BLE001 - memory is advisory; keep the last snapshot
            log.warning("situation memory update failed (%s: %s)", type(exc).__name__, exc)
            continue
        reports = [report for _, _, story_reports in stories for report in story_reports]
        situations, ref_texts, stats = apply_memory_operations(
            previous,
            parsed.get("operations") if isinstance(parsed, Mapping) else None,
            reports=reports,
            as_of=until,
        )
        async with uow.transaction() as conn:
            snapshot_id = await save_snapshot(
                conn,
                edition_id=edition_id,
                previous=previous,
                as_of=until,
                since=since,
                model=model,
                situations=situations,
                ref_texts=ref_texts,
                metadata={"input_story_count": len(stories), "operations": stats},
            )
        log.info(
            "situation memory snapshot %s: %s situations, ops=%s",
            snapshot_id,
            len(situations),
            stats,
        )
        previous = MemorySnapshot(snapshot_id, edition_id, until, tuple(situations), ref_texts)
    return snapshot_id


# ---------------------------------------------------------------------------
# Digest background


def digest_background(snapshot: MemorySnapshot | None, *, as_of: dt.datetime) -> tuple[dict, ...]:
    """Project active situations as non-citable writer/editor background."""
    if snapshot is None:
        return ()
    horizon = (as_of - dt.timedelta(days=STALE_AFTER_DAYS)).isoformat()
    rows = []
    for situation in snapshot.situations:
        if situation.get("status") == "resolved":
            continue
        if str(situation.get("last_confirmed_at") or "") < horizon:
            continue
        rows.append(
            {
                "title": situation.get("title", ""),
                "service": situation.get("service", ""),
                "areas": list(situation.get("areas") or []),
                "status": situation.get("status", "ongoing"),
                "since": situation.get("since_text", ""),
                "summary": situation.get("summary", ""),
                "reported_causes": [c.get("text", "") for c in situation.get("causes") or ()],
                "open_questions": list(situation.get("open_questions") or []),
            }
        )
        line = str(situation.get("digest_line") or "")
        supports = [
            text
            for ref in situation.get("digest_line_refs") or ()
            for text in (
                (snapshot.ref_texts.get(ref) or {}).get("evidence_text", ""),
                (snapshot.ref_texts.get(ref) or {}).get("source_text", ""),
            )
            if text
        ]
        if line and supports:
            rows[-1]["digest_line"] = line
            # Assessment re-checks the line; never sent to the model.
            rows[-1]["digest_line_supports"] = supports
    return tuple(rows)


def writer_background(background: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Background as shown to writer/editor: verification texts stay internal."""
    return [{k: v for k, v in row.items() if k != "digest_line_supports"} for row in background]


def _normalized_sentence(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[«»“”\"'„]", "", text)
    text = re.sub(r"[–—‑]", "-", text)
    return " ".join(text.split()).rstrip(" .!?")


def strip_verified_background_lines(draft: Any, background: Sequence[Mapping[str, Any]]) -> Any:
    """Remove each verified digest_line once from a copy used for evidence checks.

    The line was grounded against its own memory citations when the memory was
    updated, and is re-checked here against the same texts. Only an unchanged
    sentence is exempt; an edited one is validated as ordinary digest prose.
    """
    from dataclasses import replace as dc_replace

    from src.publication.article_claims import find_unsupported_claims

    allowed = {
        _normalized_sentence(str(row["digest_line"])): list(row.get("digest_line_supports") or ())
        for row in background
        if row.get("digest_line") and row.get("digest_line_supports")
    }
    allowed = {
        key: supports
        for key, supports in allowed.items()
        if key and not find_unsupported_claims(key, supports)
    }
    if not allowed:
        return draft
    used: set[str] = set()
    blocks = []
    for block in draft.blocks:
        items = []
        for item in block.items:
            kept = []
            for sentence in re.split(r"(?<=[.!?])\s+", item.body):
                key = _normalized_sentence(sentence)
                if key in allowed and key not in used:
                    used.add(key)
                    continue
                kept.append(sentence)
            body = " ".join(kept).strip()
            items.append(dc_replace(item, body=body) if body != item.body.strip() else item)
        blocks.append(dc_replace(block, items=tuple(items)))
    return dc_replace(draft, blocks=tuple(blocks))


DIGEST_BACKGROUND_GUIDANCE = (
    "edition_background is the newsroom's memory of long-running city situations from "
    "earlier days. It is background for judging what is new, never citable evidence: do "
    "not restate its dates, causes, durations or scope as today's facts, and keep every "
    "sentence grounded in today's supplied facts. The only exception is a situation's "
    "digest_line, an already verified background sentence: you may copy it verbatim, "
    "unchanged and at most once, as the first sentence of the item whose facts continue "
    "that situation on the same service. Do not edit, shorten, merge or paraphrase it; if "
    "it does not fit, omit it. When today's facts continue a listed "
    "situation, do not present them as a sudden new event (no headlines such as "
    "«Исчезновение света» or «Свет пропал»): lead the theme with what changed — power "
    "or water returned, schedules, repairs, new areas, worsening or improvement — and "
    "report the continuing absence plainly after that. Answers to its open_questions "
    "deserve prominence when today's facts supply them."
)
