"""Deterministic conversation signals for chat-like sources.

Residents write in short remarks that only make sense in sequence. This module
holds the two signals built on that fact: which fragments carry too little text
for their vector to mean anything, and the chat timeline Gate reads around the
Stories it judges.

A timeline is context only. It explains what a remark refers to and is never
evidence for it.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

import psycopg

from src.config_loader import EditionScopeConfig, EventPipelineConfig
from src.domain.edition_geography import EditionGeographyContext
from src.domain.event_payload import EventPayload
from src.domain.service_taxonomy import (
    SAFETY_INCIDENT_PATTERN,
    SUPPLY_FAMILIES,
    detect_service_families,
)
from src.processing.edition_scope import local_detail_anchors, names_anchor

SAME_INCIDENT_RELATION = "same_incident"
TIMELINE_PAUSE_MARK = "--- pause ---"

_WORD_RE = re.compile(r"[a-zа-яёіїєґ0-9'’]+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+")
_QUOTED_SPAN_RE = re.compile(r"«([^«»]{3,})»|“([^”]{3,})”|\"([^\"]{3,})\"")

# One incident needs one event to point at. "Выключили" or "света нет" state
# availability and name no event, however long the conversation runs.
_CONCRETE_INCIDENT_RE = re.compile(
    rf"\b(?:{SAFETY_INCIDENT_PATTERN}|"
    r"авари(?:я|и|ю|ей)|дтп|прорыв\w*|прорвал\w*|загорел\w*|столкнов\w*|"
    r"затопил\w*|обрушен\w*|обрушил\w*|обвал\w*)\b",
    re.IGNORECASE,
)

# How far a short remark may look for the service it leaves unnamed.
_SERVICE_REPLY_HOPS = 3
_SERVICE_PRECEDING_LINES = 5
_SERVICE_PRECEDING_WINDOW = dt.timedelta(minutes=15)

# Function words and chat fillers. "Нет" and "есть" are here on purpose: they
# carry the state of a report but say nothing about its subject.
_STOP_WORDS = frozenset(
    """
    а аж ага алло ай б без бы был была были было быть в вам вас вдруг ведь весь вот вроде
    все всё всем всех всего вы где да даже для до его ее её ей ему если есть еще ещё ж же
    за зато здесь и из или им их к как какая какие какой кого когда кому кто куда ли либо
    лишь мало меня мне много мной мы на над надо нам нас не него нее неё ней нет нету ни
    нибудь никто ним них ничего но ну о об обо он она они оно от ого опять очень по под
    пока после потом почему при про просто раз разве с сам сама сами само свой своя свои
    себе себя сегодня сейчас со совсем так такая такие такой там тебе тебя теперь то тоже
    только тот ту ты тут у уж уже хоть хотя чего чей чем через что чтоб чтобы чуть эта эти
    это этот этих этой я яка який які що це та ні але або вже ще він вона вони ми ви ти від
    як де коли хто чи бо нема немає
    """.split()
)

# Advertising and classified paragraphs would only add noise to a timeline.
_HIDDEN_DROP_REASONS = frozenset(
    {"commercial_classified", "private_classified", "directory_payload"}
)


def content_words(text: str) -> tuple[str, ...]:
    """Return the words of a text that can name a subject."""
    words = (word.casefold() for word in _WORD_RE.findall(text))
    return tuple(
        word for word in words if word not in _STOP_WORDS and (len(word) >= 3 or word.isdigit())
    )


def is_thin_fragment(text: str, *, max_content_words: int) -> bool:
    """Return True for a remark whose embedding reflects chat register, not an event.

    A fragment with no letters at all (a bare phone number) is not a remark, so
    it keeps the ordinary vector route.
    """
    if not any(char.isalpha() for char in text):
        return False
    return len(content_words(text)) <= max_content_words


@dataclass(frozen=True)
class ThinFragmentRouting:
    """How thin chat fragments reach a Story."""

    max_content_words: int = 2
    near_identical_similarity: float = 0.95
    conversation_window: dt.timedelta = dt.timedelta(minutes=30)
    max_reply_hops: int = 3
    max_conversation_candidates: int = 5


@dataclass(frozen=True)
class ConversationTimelineSettings:
    """Size of the chat window read around each judged Story."""

    messages_before: int = 10
    messages_after: int = 3
    max_gap: dt.timedelta = dt.timedelta(minutes=45)
    max_lines: int = 60
    line_chars: int = 140
    anchors_per_story: int = 3


def thin_routing_from_config(cfg: EventPipelineConfig) -> ThinFragmentRouting | None:
    """Return thin-fragment routing, or None when the pipeline has it switched off."""
    if not cfg.thin_fragment_routing_enabled:
        return None
    return ThinFragmentRouting(
        max_content_words=cfg.thin_fragment_max_content_words,
        near_identical_similarity=cfg.thin_fragment_near_identical_similarity,
        conversation_window=dt.timedelta(minutes=cfg.thin_fragment_conversation_minutes),
    )


def timeline_settings_from_config(
    cfg: EventPipelineConfig,
) -> ConversationTimelineSettings | None:
    """Return timeline settings, or None when the pipeline has it switched off."""
    if not cfg.conversation_timeline_enabled:
        return None
    return ConversationTimelineSettings(
        messages_before=cfg.conversation_timeline_messages_before,
        messages_after=cfg.conversation_timeline_messages_after,
        max_gap=dt.timedelta(minutes=cfg.conversation_timeline_max_gap_minutes),
        max_lines=cfg.conversation_timeline_max_lines,
        line_chars=cfg.conversation_timeline_line_chars,
    )


@dataclass(frozen=True)
class TimelineAnchor:
    """A message position that a timeline must surround."""

    source_id: int
    observed_at: dt.datetime


@dataclass(frozen=True)
class TimelineLine:
    """One message of a chat timeline."""

    label: str
    item_id: int
    source_name: str
    parent_item_id: int | None
    observed_at: dt.datetime
    text: str
    story_ids: tuple[int, ...]


@dataclass(frozen=True)
class ConversationTimeline:
    """Messages around a batch of Stories, in reading order, one per label."""

    lines: tuple[TimelineLine, ...] = ()
    pause: dt.timedelta = ConversationTimelineSettings.max_gap

    def render(self, judged_story_ids: Iterable[int]) -> str:
        """Render the timeline for a prompt, or an empty string when there is nothing to read."""
        if not self.lines:
            return ""
        judged = set(judged_story_ids)
        label_by_item = {line.item_id: line.label for line in self.lines}
        rendered: list[str] = [
            "CHAT TIMELINE — CONTEXT ONLY, NOT CITABLE (oldest first, times in UTC):"
        ]
        current_source: str | None = None
        previous_at: dt.datetime | None = None
        for line in self.lines:
            if line.source_name != current_source:
                current_source = line.source_name
                rendered.append(f"«{current_source}»:")
            elif previous_at is not None and line.observed_at - previous_at > self.pause:
                rendered.append(TIMELINE_PAUSE_MARK)
            previous_at = line.observed_at
            parts = [line.label, f"{line.observed_at:%H:%M}"]
            parent_label = label_by_item.get(line.parent_item_id or -1)
            if parent_label:
                parts.append(f"↩ {parent_label}")
            parts.append(line.text)
            marks = [f"#{story_id}" for story_id in line.story_ids if story_id in judged]
            if marks:
                parts.append(f"[Story {', '.join(marks)}]")
            rendered.append(" ".join(parts))
        return "\n".join(rendered)

    def linked_story_ids(self, labels: object, *, excluding: int) -> tuple[int, ...]:
        """Resolve labels returned by Gate to other Stories, ignoring unknown labels."""
        if not isinstance(labels, (list, tuple)):
            return ()
        by_label = {line.label: line.story_ids for line in self.lines}
        resolved: set[int] = set()
        for label in labels:
            resolved.update(by_label.get(str(label).strip(), ()))
        resolved.discard(excluding)
        return tuple(sorted(resolved))

    def same_incident_story_ids(
        self,
        labels: object,
        *,
        story_id: int,
        own_texts: Sequence[str],
        scope: EditionScopeConfig,
        geo_context: EditionGeographyContext | None,
    ) -> tuple[int, ...]:
        """Resolve Gate's incident labels, dropping lines that cannot be one incident.

        A label is only a proposal. It stands only when one side names a concrete
        event and both sit in the same stretch of the chat. A line naming a
        different place describes something else, but a line or Story with no
        place stays eligible: "двери чуть не вынесло" is exactly the remark this
        relation exists to connect.
        """
        if not isinstance(labels, (list, tuple)):
            return ()
        wanted = {str(label).strip() for label in labels}
        stretch_by_label = self._stretch_by_label()
        story_stretches = {
            stretch_by_label[line.label] for line in self.lines if story_id in line.story_ids
        }
        anchors = local_detail_anchors(scope, geo_context)
        own_places = _named_places(own_texts, anchors)
        story_names_event = any(_CONCRETE_INCIDENT_RE.search(text) for text in own_texts)
        plausible = [
            line.label
            for line in self.lines
            if line.label in wanted
            and stretch_by_label[line.label] in story_stretches
            and (story_names_event or _CONCRETE_INCIDENT_RE.search(line.text))
            and not _names_a_different_place(line.text, own_places, anchors)
        ]
        return self.linked_story_ids(plausible, excluding=story_id)

    def omitted_service_family(self, item_id: int | None, *, parent_text: str = "") -> str | None:
        """Return the only service a conversation discusses around a message, if unambiguous.

        A reply belongs to its own thread, so its chain decides whenever it names
        a service. ``parent_text`` covers a direct parent too old for the
        timeline. Neighbouring lines are read only by a remark with no reply
        link or one whose visible chain never names a service.
        """
        if not self.lines:
            return None
        line = next((candidate for candidate in self.lines if candidate.item_id == item_id), None)
        visible_chain = self._reply_chain_texts(line) if line is not None else ()
        named_by_chain = _supply_families((parent_text, *visible_chain))
        if named_by_chain:
            return _only_family(named_by_chain)
        if line is None or (line.parent_item_id is not None and not visible_chain):
            return None
        return _only_family(_supply_families(self._preceding_texts(line)))

    def context_texts_for(self, story_id: int) -> tuple[str, ...]:
        """Return the timeline text that a Story does not own."""
        return tuple(line.text for line in self.lines if story_id not in line.story_ids)

    def _stretch_by_label(self) -> dict[str, int]:
        """Number the unbroken stretches of chat, one per source and silence."""
        stretch = 0
        result: dict[str, int] = {}
        previous: TimelineLine | None = None
        for line in self.lines:
            if previous is not None and (
                line.source_name != previous.source_name
                or line.observed_at - previous.observed_at > self.pause
            ):
                stretch += 1
            result[line.label] = stretch
            previous = line
        return result

    def _reply_chain_texts(self, line: TimelineLine) -> tuple[str, ...]:
        """Return the visible ancestors of a reply, nearest first."""
        by_item = {candidate.item_id: candidate for candidate in self.lines}
        texts: list[str] = []
        parent_id = line.parent_item_id
        for _ in range(_SERVICE_REPLY_HOPS):
            parent = by_item.get(parent_id) if parent_id is not None else None
            if parent is None:
                break
            texts.append(parent.text)
            parent_id = parent.parent_item_id
        return tuple(texts)

    def _preceding_texts(self, line: TimelineLine) -> tuple[str, ...]:
        """Return the lines of the same chat said shortly before a message."""
        earlier = [
            candidate.text
            for candidate in self.lines
            if candidate.item_id != line.item_id
            and candidate.source_name == line.source_name
            and candidate.observed_at <= line.observed_at
            and line.observed_at - candidate.observed_at <= _SERVICE_PRECEDING_WINDOW
        ]
        return tuple(earlier[-_SERVICE_PRECEDING_LINES:])


def _supply_families(texts: Iterable[str]) -> set[str]:
    return {family for text in texts for family in detect_service_families(text) & SUPPLY_FAMILIES}


def _only_family(families: set[str]) -> str | None:
    return next(iter(families)) if len(families) == 1 else None


def _named_places(texts: Iterable[str], anchors: Iterable[str]) -> set[str]:
    materialized = tuple(texts)
    return {anchor for anchor in anchors if any(names_anchor(t, anchor) for t in materialized)}


def _names_a_different_place(text: str, own_places: set[str], anchors: Iterable[str]) -> bool:
    if not own_places:
        return False
    line_places = _named_places([text], anchors)
    return bool(line_places) and own_places.isdisjoint(line_places)


def _fold_quote(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _single_line(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _is_shown(
    *,
    text: str,
    story_ids: Sequence[int],
    drop_reasons: Sequence[str],
) -> bool:
    if not text:
        return False
    return bool(story_ids) or not _HIDDEN_DROP_REASONS.intersection(drop_reasons)


def build_timeline_lines(
    rows: Sequence[Mapping[str, object]],
    anchors: Sequence[TimelineAnchor],
    settings: ConversationTimelineSettings,
) -> tuple[TimelineLine, ...]:
    """Choose and label the messages to show.

    A one-word reaction that nothing replied to adds no flow, so noise is kept
    only as the parent of a shown line. When a busy hour exceeds the line budget,
    the messages closest to a judged Story stay.
    """
    candidates = [
        row
        for row in rows
        if _is_shown(
            text=str(row["text"] or "").strip(),
            story_ids=_int_tuple(row["story_ids"]),
            drop_reasons=_str_tuple(row["drop_reasons"]),
        )
    ]
    replied_to = {row["parent_item_id"] for row in candidates if row["parent_item_id"] is not None}
    kept = [
        row for row in candidates if _int_tuple(row["story_ids"]) or row["item_id"] in replied_to
    ]

    anchors_by_source: dict[int, list[dt.datetime]] = {}
    for anchor in anchors:
        anchors_by_source.setdefault(anchor.source_id, []).append(anchor.observed_at)

    def distance(row: Mapping[str, object]) -> float:
        observed = _timestamp(row["observed_at"])
        times = anchors_by_source.get(int(str(row["source_id"])), [])
        return min((abs((observed - at).total_seconds()) for at in times), default=float("inf"))

    closest = sorted(kept, key=lambda row: (distance(row), int(str(row["item_id"]))))
    chosen = closest[: settings.max_lines]
    chosen.sort(
        key=lambda row: (
            str(row["source_name"]),
            _timestamp(row["observed_at"]),
            int(str(row["item_id"])),
        )
    )
    return tuple(
        TimelineLine(
            label=f"C{number}",
            item_id=int(str(row["item_id"])),
            source_name=str(row["source_name"]),
            parent_item_id=(
                int(str(row["parent_item_id"])) if row["parent_item_id"] is not None else None
            ),
            observed_at=_timestamp(row["observed_at"]),
            text=_single_line(str(row["text"]), settings.line_chars),
            story_ids=_int_tuple(row["story_ids"]),
        )
        for number, row in enumerate(chosen, start=1)
    )


def _timestamp(value: object) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    return value


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(int(item) for item in value if item is not None)


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if item is not None)


async def load_conversation_timeline(
    conn: psycopg.AsyncConnection,
    anchors: Sequence[TimelineAnchor],
    *,
    edition_id: int,
    settings: ConversationTimelineSettings,
    source_cutoff_at: dt.datetime | None = None,
) -> ConversationTimeline:
    """Load the chat messages around the given positions, as of the source cutoff."""
    if not anchors:
        return ConversationTimeline()
    positions = sorted({(a.source_id, a.observed_at) for a in anchors})
    cursor = await conn.execute(
        """
        WITH anchor(source_id, ts) AS (
            SELECT * FROM unnest(%(sources)s::bigint[], %(times)s::timestamptz[])
        ), nearby AS (
            SELECT DISTINCT w.id, w.source_id, w.parent_item_id, w.observed_at
            FROM anchor a
            CROSS JOIN LATERAL (
                (
                    SELECT si.id, si.source_id, si.parent_item_id,
                           COALESCE(si.published_at, si.first_collected_at) AS observed_at
                    FROM source_items si
                    WHERE si.source_id = a.source_id
                      AND COALESCE(si.published_at, si.first_collected_at) <= a.ts
                      AND COALESCE(si.published_at, si.first_collected_at) >= a.ts - %(gap)s
                      AND (
                          %(cutoff)s::timestamptz IS NULL
                          OR COALESCE(si.published_at, si.first_collected_at) <= %(cutoff)s
                      )
                    ORDER BY COALESCE(si.published_at, si.first_collected_at) DESC
                    LIMIT %(before)s
                )
                UNION ALL
                (
                    SELECT si.id, si.source_id, si.parent_item_id,
                           COALESCE(si.published_at, si.first_collected_at) AS observed_at
                    FROM source_items si
                    WHERE si.source_id = a.source_id
                      AND COALESCE(si.published_at, si.first_collected_at) > a.ts
                      AND COALESCE(si.published_at, si.first_collected_at) <= a.ts + %(gap)s
                      AND (
                          %(cutoff)s::timestamptz IS NULL
                          OR COALESCE(si.published_at, si.first_collected_at) <= %(cutoff)s
                      )
                    ORDER BY COALESCE(si.published_at, si.first_collected_at) ASC
                    LIMIT %(after)s
                )
            ) w
        )
        SELECT n.id, n.source_id, src.name, n.parent_item_id, n.observed_at,
               rev.text_content, frag.story_ids, frag.drop_reasons
        FROM nearby n
        JOIN sources src ON src.id = n.source_id
        JOIN LATERAL (
            SELECT r.id, r.text_content
            FROM source_item_revisions r
            WHERE r.source_item_id = n.id
              AND (%(cutoff)s::timestamptz IS NULL OR r.collected_at <= %(cutoff)s)
            ORDER BY r.revision_no DESC
            LIMIT 1
        ) rev ON TRUE
        LEFT JOIN LATERAL (
            SELECT array_remove(array_agg(DISTINCT st.id), NULL) AS story_ids,
                   array_remove(array_agg(DISTINCT f.drop_reason), NULL) AS drop_reasons
            FROM source_fragments f
            LEFT JOIN story_fragments sf ON sf.fragment_id = f.id
            LEFT JOIN stories st ON st.id = sf.story_id AND st.edition_id = %(edition)s
            WHERE f.source_item_revision_id = rev.id
        ) frag ON TRUE
        ORDER BY n.observed_at, n.id
        """,
        {
            "sources": [source_id for source_id, _ in positions],
            "times": [observed_at for _, observed_at in positions],
            "gap": settings.max_gap,
            "cutoff": source_cutoff_at,
            "before": settings.messages_before,
            "after": settings.messages_after,
            "edition": edition_id,
        },
    )
    rows = [
        {
            "item_id": row[0],
            "source_id": row[1],
            "source_name": row[2],
            "parent_item_id": row[3],
            "observed_at": row[4],
            "text": row[5],
            "story_ids": row[6],
            "drop_reasons": row[7],
        }
        for row in await cursor.fetchall()
    ]
    return ConversationTimeline(
        lines=build_timeline_lines(rows, anchors, settings), pause=settings.max_gap
    )


def context_only_details(
    text: str,
    *,
    own_texts: Sequence[str],
    context_texts: Sequence[str],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> tuple[str, ...]:
    """Return places, numbers and quotes a text can only have taken from the timeline.

    The edition's own city is excluded: residents of a city chat rarely name it,
    and "в городе" is not a detail borrowed from a neighbouring message.
    """
    if not text.strip() or not context_texts:
        return ()
    found: list[str] = []
    own_numbers = {number for own in own_texts for number in _NUMBER_RE.findall(own)}
    context_numbers = {number for ctx in context_texts for number in _NUMBER_RE.findall(ctx)}
    for number in _NUMBER_RE.findall(text):
        # "00" is the minutes of a time the Story gave as a whole hour.
        if number.strip("0") and number in context_numbers and number not in own_numbers:
            found.append(number)
    for anchor in local_detail_anchors(scope, geo_context):
        if (
            names_anchor(text, anchor)
            and any(names_anchor(context, anchor) for context in context_texts)
            and not any(names_anchor(own, anchor) for own in own_texts)
        ):
            found.append(anchor)
    folded_own = [_fold_quote(own) for own in own_texts]
    folded_context = [_fold_quote(context) for context in context_texts]
    for match in _QUOTED_SPAN_RE.finditer(text):
        span = _fold_quote(next(group for group in match.groups() if group))
        if any(span in context for context in folded_context) and not any(
            span in own for own in folded_own
        ):
            found.append(span)
    return tuple(dict.fromkeys(found))


def withhold_context_only_evidence(
    payload: EventPayload,
    *,
    own_texts: Sequence[str],
    context_texts: Sequence[str],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> tuple[EventPayload, bool]:
    """Turn PUBLISH evidence that borrowed a timeline detail into CONTEXT.

    Returns the payload and whether a PUBLISH item was withheld.
    """
    withheld = False
    items = []
    for item in payload.evidence_items:
        if item.publication_use == "PUBLISH" and context_only_details(
            item.text,
            own_texts=own_texts,
            context_texts=context_texts,
            scope=scope,
            geo_context=geo_context,
        ):
            withheld = True
            items.append(replace(item, publication_use="CONTEXT", service_state=None))
        else:
            items.append(item)
    if not withheld:
        return payload, False
    return replace(payload, evidence_items=tuple(items)), True
