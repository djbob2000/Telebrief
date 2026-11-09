# ruff: noqa: S101
"""Chat remarks are routed and read as a conversation, never as evidence.

Regression: the digest of 8 Nov 2026 placed "двери чуть не вынесло" under "Другое"
while the blasts it reacted to led the safety rubric. The remark had no place of
its own and Gate read it apart from the messages around it.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.config_loader import load_config
from src.domain.edition_geography import resolve_edition_geography
from src.domain.event_clusters import ClusterJoinCandidate, StoryClusterState
from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.processing.conversation import (
    TIMELINE_PAUSE_MARK,
    ConversationTimeline,
    ConversationTimelineSettings,
    ThinFragmentRouting,
    TimelineAnchor,
    TimelineLine,
    build_timeline_lines,
    content_words,
    context_only_details,
    is_thin_fragment,
    withhold_context_only_evidence,
)
from src.processing.event_clustering import EventClusteringService
from src.processing.event_triage import StoryTriageService
from src.processing.operational_semantics import normalize_service_state_evidence

SCOPE = load_config("config.yaml").settings.edition_scopes["berdyansk"]
GEO = resolve_edition_geography("berdyansk", SCOPE.name)
NOW = dt.datetime(2026, 11, 8, 1, 30, tzinfo=dt.timezone.utc)
BLASTS = "В районе АКЗ очень сильные взрывы"
DOORS = "Двери чуть не вынесло"
DEFAULT_ROUTING = ThinFragmentRouting()


@pytest.mark.parametrize(
    "text",
    ["Ага на горе", "Куда попало", "Акз сильно", "Ура", "Нет света", "Так и есть, всё так"],
)
def test_remark_without_a_subject_is_thin(text: str) -> None:
    assert is_thin_fragment(text, max_content_words=2)


@pytest.mark.parametrize(
    "text",
    [
        "Акз двери ходуном",
        "На Димитрова с водой печально",
        "Автобус номер 4 сегодня ходит раз в час",
        "+7 949 123-45-67",
    ],
)
def test_remark_that_names_a_subject_is_not_thin(text: str) -> None:
    assert not is_thin_fragment(text, max_content_words=2)


def test_digits_count_as_content_but_short_words_do_not() -> None:
    assert content_words("На 8 Марта") == ("8", "марта")
    assert content_words("Да ну") == ()


class RoutingRepo:
    """Cluster storage with a fixed reply parent and candidate ranking."""

    def __init__(
        self,
        *,
        parent_story: int | None = None,
        similarity: float | None = None,
        nearby: bool = False,
    ) -> None:
        self.parent_story = parent_story
        self.similarity = similarity
        self.nearby = nearby
        self.searches: list[dt.datetime] = []
        self.parent_lookups = 0
        self.saved_centroid: list[float] | None = None
        self.assigned: tuple[int, str, float | None] | None = None

    async def get_fragment_assignment(self, conn: Any, fragment_id: int) -> None:
        return None

    async def find_reply_parent_story(self, conn: Any, **kwargs: Any) -> int | None:
        self.parent_lookups += 1
        return self.parent_story

    async def find_candidate_clusters(
        self, conn: Any, edition_id: int, vector: Any, **kwargs: Any
    ) -> list[ClusterJoinCandidate]:
        self.searches.append(kwargs["active_since"])
        if self.similarity is None:
            return []
        return [
            ClusterJoinCandidate(
                story_id=7, similarity=self.similarity, fragment_count=2, last_seen_at=NOW
            )
        ]

    async def has_fragment_near_in_same_source(self, conn: Any, **kwargs: Any) -> bool:
        return self.nearby

    async def lock_cluster_state(self, conn: Any, story_id: int) -> StoryClusterState:
        return StoryClusterState(
            story_id, [1.0, 0.0], "m", 2, 3, 1, NOW, NOW, 1, None, None, True, NOW
        )

    async def assign_fragment_to_story(self, conn: Any, **kwargs: Any) -> int:
        self.assigned = (kwargs["story_id"], kwargs["assignment_kind"], kwargs["similarity"])
        return 11

    async def get_unique_sources_for_story(self, conn: Any, story_id: int) -> int:
        return 1

    async def upsert_cluster_state(self, conn: Any, **kwargs: Any) -> None:
        self.saved_centroid = kwargs["centroid"]


class ShellRepo:
    async def create_story_shell(self, conn: Any, **kwargs: Any) -> int:
        return 99


async def _route(
    repo: RoutingRepo, text: str, *, routing: ThinFragmentRouting | None = DEFAULT_ROUTING
) -> tuple[int, str]:
    service = EventClusteringService.__new__(EventClusteringService)
    service.cluster_repo = repo  # type: ignore[assignment]
    service.story_repo = ShellRepo()  # type: ignore[assignment]
    result = await service.process_fragment(
        None,  # type: ignore[arg-type]
        SimpleNamespace(id=1, text_content=text, source_item_revision_id=5),  # type: ignore[arg-type]
        edition_id=1,
        fragment_embedding_id=1,
        vector=[0.0, 1.0],
        model="m",
        dimensions=2,
        item_timestamp=NOW,
        thin_routing=routing,
    )
    return result.story_id, result.assignment_kind


@pytest.mark.asyncio
async def test_thin_reply_joins_its_parent_and_leaves_the_centroid_alone() -> None:
    repo = RoutingRepo(parent_story=42, similarity=0.99)

    assert await _route(repo, "Куда попало") == (42, "reply_join")
    assert repo.assigned == (42, "reply_join", None)
    assert repo.saved_centroid == [1.0, 0.0]
    assert repo.searches == []


@pytest.mark.asyncio
async def test_thin_remark_repeating_a_report_keeps_the_vector_route() -> None:
    assert await _route(RoutingRepo(similarity=0.97), "Куда попало") == (7, "vector_join")


@pytest.mark.asyncio
async def test_thin_remark_with_only_a_register_match_starts_its_own_story() -> None:
    assert await _route(RoutingRepo(similarity=0.90), "Куда попало") == (99, "new_story")


@pytest.mark.asyncio
async def test_thin_remark_joins_a_story_running_in_its_own_chat() -> None:
    repo = RoutingRepo(similarity=0.90, nearby=True)

    assert await _route(repo, "Куда попало") == (7, "vector_join")
    assert repo.searches[-1] == NOW - ThinFragmentRouting().conversation_window


@pytest.mark.asyncio
async def test_ordinary_fragment_is_routed_only_by_its_vector() -> None:
    repo = RoutingRepo(parent_story=42, similarity=0.90)

    assert await _route(repo, "На Димитрова с водой печально") == (7, "vector_join")
    assert repo.parent_lookups == 0


@pytest.mark.asyncio
async def test_routing_switched_off_keeps_the_previous_behaviour() -> None:
    repo = RoutingRepo(parent_story=42, similarity=0.90)

    assert await _route(repo, "Куда попало", routing=None) == (7, "vector_join")
    assert repo.parent_lookups == 0


def _row(
    item_id: int,
    text: str,
    *,
    minute: int = 0,
    story_ids: tuple[int, ...] = (),
    drop_reasons: tuple[str, ...] = (),
    parent: int | None = None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "source_id": 3,
        "source_name": "Город",
        "parent_item_id": parent,
        "observed_at": NOW + dt.timedelta(minutes=minute),
        "text": text,
        "story_ids": list(story_ids),
        "drop_reasons": list(drop_reasons),
    }


ANCHORS = [TimelineAnchor(source_id=3, observed_at=NOW)]


def test_timeline_hides_ads_and_unanswered_noise_but_keeps_a_parent() -> None:
    lines = build_timeline_lines(
        [
            _row(1, "Продам диван", story_ids=(), drop_reasons=("commercial_classified",)),
            _row(2, "Ого", minute=1, drop_reasons=("short_noise",)),
            _row(3, "Что это было", minute=2, drop_reasons=("short_noise",)),
            _row(4, BLASTS, minute=3, story_ids=(31,), parent=3),
        ],
        ANCHORS,
        ConversationTimelineSettings(),
    )

    assert [line.text for line in lines] == ["Что это было", BLASTS]


def test_timeline_budget_keeps_the_lines_closest_to_a_judged_story() -> None:
    rows = [_row(i, f"сообщение {i}", minute=i, story_ids=(i,)) for i in range(1, 11)]

    lines = build_timeline_lines(rows, ANCHORS, ConversationTimelineSettings(max_lines=3))

    assert [line.item_id for line in lines] == [1, 2, 3]
    assert [line.label for line in lines] == ["C1", "C2", "C3"]


def _timeline() -> ConversationTimeline:
    return ConversationTimeline(
        lines=(
            TimelineLine("C1", 1, "Город", None, NOW, BLASTS, (31,)),
            TimelineLine("C2", 2, "Город", 1, NOW, DOORS, (30,)),
        )
    )


def test_timeline_render_marks_replies_and_only_judged_stories() -> None:
    rendered = _timeline().render([30])

    assert "NOT CITABLE" in rendered
    assert f"C2 01:30 ↩ C1 {DOORS} [Story #30]" in rendered
    assert "[Story #31]" not in rendered


def test_timeline_render_marks_a_silence_between_conversations() -> None:
    later = NOW + dt.timedelta(hours=7)
    timeline = ConversationTimeline(
        lines=(
            TimelineLine("C1", 1, "Город", None, NOW, BLASTS, (31,)),
            TimelineLine("C2", 2, "Город", None, NOW + dt.timedelta(minutes=5), DOORS, (30,)),
            TimelineLine("C3", 3, "Город", None, later, "Свет дали", (32,)),
        )
    )

    rendered = timeline.render([]).splitlines()

    assert rendered[-2] == TIMELINE_PAUSE_MARK
    assert rendered.count(TIMELINE_PAUSE_MARK) == 1


def test_linked_labels_resolve_to_other_stories_and_ignore_the_rest() -> None:
    timeline = _timeline()

    assert timeline.linked_story_ids(["C1", "C2", "C9", 5], excluding=30) == (31,)
    assert timeline.linked_story_ids("C1", excluding=30) == ()


def _borrowed(text: str, own: str = DOORS) -> tuple[str, ...]:
    return context_only_details(
        text,
        own_texts=[own],
        context_texts=[BLASTS, "Четыре взрыва в 2 часа"],
        scope=SCOPE,
        geo_context=GEO,
    )


def test_a_place_or_number_seen_only_in_the_timeline_is_borrowed() -> None:
    assert _borrowed("В районе АКЗ двери чуть не вынесло")
    assert _borrowed("Двери чуть не вынесло в 2 часа") == ("2",)


def test_a_detail_the_story_states_itself_is_not_borrowed() -> None:
    assert _borrowed("На АКЗ двери ходуном", own="Акз двери ходуном") == ()
    assert _borrowed("Двери чуть не вынесло") == ()


def test_minutes_of_a_whole_hour_are_not_a_borrowed_number() -> None:
    assert (
        context_only_details(
            "Свет дали в 10:00",
            own_texts=["Свет дали в 10 часов"],
            context_texts=["В 12:00 будет вода"],
            scope=SCOPE,
            geo_context=GEO,
        )
        == ()
    )


def test_the_editions_own_city_is_not_a_borrowed_detail() -> None:
    assert (
        context_only_details(
            "В Бердянске двери чуть не вынесло",
            own_texts=[DOORS],
            context_texts=["В Бердянске взрывы"],
            scope=SCOPE,
            geo_context=GEO,
        )
        == ()
    )


def test_borrowed_evidence_is_withheld_and_clean_evidence_is_kept() -> None:
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload("В районе АКЗ вынесло двери", "community_report", "PUBLISH", (1,)),
            EvidenceItemPayload(DOORS, "community_report", "PUBLISH", (1,)),
        )
    )

    result, withheld = withhold_context_only_evidence(
        payload, own_texts=[DOORS], context_texts=[BLASTS], scope=SCOPE, geo_context=GEO
    )

    assert withheld
    assert [item.publication_use for item in result.evidence_items] == ["CONTEXT", "PUBLISH"]


def _chat(*lines: tuple[str, int, int | None, tuple[int, ...]]) -> ConversationTimeline:
    """Build a timeline from (text, minute, parent line number, Story ids) in reading order."""
    return ConversationTimeline(
        lines=tuple(
            TimelineLine(
                f"C{number}",
                number,
                "Город",
                parent,
                NOW + dt.timedelta(minutes=minute),
                text,
                stories,
            )
            for number, (text, minute, parent, stories) in enumerate(lines, start=1)
        )
    )


def test_a_remark_takes_the_only_service_named_right_before_it() -> None:
    timeline = _chat(
        ("Когда дадут свет", 0, None, ()),
        ("Света нет с утра", 1, None, ()),
        ("Колония нету", 2, None, (30,)),
    )

    assert timeline.omitted_service_family(3) == "power"


def test_a_reply_chain_outranks_the_lines_around_it() -> None:
    timeline = _chat(
        ("Свет есть?", 0, None, ()),
        ("Воды нет с утра", 1, None, ()),
        ("Выключили", 2, 1, (30,)),
    )

    assert timeline.omitted_service_family(3) == "power"


def test_a_topic_does_not_compete_with_the_service_of_a_chain() -> None:
    timeline = _chat(
        ("Тариф на свет подняли, цены выросли", 0, None, ()),
        ("Целинная есть", 1, 1, (30,)),
    )

    assert timeline.omitted_service_family(2) == "power"


@pytest.mark.parametrize(
    "lines",
    [
        # Two services in the chain: "обещали дать" could be either.
        (
            ("Отопления и света нет", 0, None, ()),
            ("Сегодня обещали дать", 1, 1, (30,)),
        ),
        # Two services in the lines before a free-standing remark.
        (
            ("Света нет с утра", 0, None, ()),
            ("Воды тоже нет", 1, None, ()),
            ("Колония нету", 2, None, (30,)),
        ),
        # The service was named too long ago to be what the remark means.
        (
            ("Света нет с утра", 0, None, ()),
            ("Колония нету", 20, None, (30,)),
        ),
    ],
)
def test_an_unclear_conversation_names_no_service(
    lines: tuple[tuple[str, int, int | None, tuple[int, ...]], ...],
) -> None:
    timeline = _chat(*lines)

    assert timeline.omitted_service_family(timeline.lines[-1].item_id) is None


def test_a_reply_to_a_message_out_of_view_does_not_read_its_neighbours() -> None:
    timeline = _chat(
        ("Света нет с утра", 0, None, ()),
        ("Колония нету", 1, 999, (30,)),
    )

    assert timeline.omitted_service_family(2) is None


def test_a_parent_too_old_for_the_timeline_still_names_the_service() -> None:
    timeline = _chat(("Воды нет с утра", 0, None, ()), ("Целинная есть", 1, 999, (30,)))

    assert timeline.omitted_service_family(2, parent_text="Весь акз без света") == "power"


def test_a_parent_naming_two_services_settles_nothing() -> None:
    timeline = _chat(("Света нет", 0, None, ()), ("Сегодня обещали дать", 1, 999, (30,)))

    assert timeline.omitted_service_family(2, parent_text="Отопления и света нет") is None


def test_a_message_outside_the_timeline_names_no_service() -> None:
    assert _chat(("Света нет", 0, None, ())).omitted_service_family(404) is None
    assert _chat(("Света нет", 0, None, ())).omitted_service_family(None) is None


def test_a_switched_off_timeline_settles_no_service_even_with_a_parent() -> None:
    assert ConversationTimeline().omitted_service_family(2, parent_text="Света нет") is None


def _power_claim(
    text: str = "В Колонии нет света",
    *,
    kind: str = "community_report",
    state: ServiceStatePayload | None = None,
) -> EventPayload:
    return EventPayload(
        evidence_items=(EvidenceItemPayload(text, kind, "PUBLISH", (7,), service_state=state),)
    )


def _check_claim(
    payload: EventPayload, remark: str, family: str | None = "power"
) -> tuple[EvidenceItemPayload, tuple[str, ...]]:
    result, audit = normalize_service_state_evidence(
        payload,
        {7: remark},
        conversation_service_family_by_fragment_id={7: family} if family else None,
    )
    return result.evidence_items[0], audit.rejection_reasons


@pytest.mark.parametrize(
    "remark",
    ["Колония нету", "Выключили", "на Гайдара нет", "Целинная есть.", "Азмол дали но не везде"],
)
def test_conversation_confirms_the_service_of_a_short_status_remark(remark: str) -> None:
    item, reasons = _check_claim(_power_claim(), remark)

    assert item.publication_use == "PUBLISH"
    assert reasons == ()


def test_a_state_inferred_from_the_conversation_is_never_operational() -> None:
    state = ServiceStatePayload(
        subject_key="power_supply",
        subject_label="Электроснабжение",
        dimension="availability",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )

    item, reasons = _check_claim(_power_claim(kind="service_access", state=state), "Колония нету")

    assert (item.kind, item.publication_use, item.service_state) == (
        "community_report",
        "PUBLISH",
        None,
    )
    assert reasons == ("conversation_only_subject",)


@pytest.mark.parametrize(
    ("claim", "remark", "family"),
    [
        # No conversation to lean on.
        ("В Колонии нет света", "Колония нету", None),
        # A question is not a report, even with a state word.
        ("На Гайдара есть свет", "На Гайдара есть?", "power"),
        # No availability in the remark at all.
        ("Света нет, завтраками кормят", "Каждый день завтраками кормят", "power"),
        # The remark names a different service than the claim.
        ("Света нет", "Воды нет", "power"),
        # The claim names a service the conversation is not about.
        ("В Колонии нет воды", "Колония нету", "power"),
        # The claim adds a second service to the one discussed.
        ("В Колонии нет света и воды", "Колония нету", "power"),
    ],
)
def test_conversation_does_not_supply_a_service_it_cannot_settle(
    claim: str, remark: str, family: str | None
) -> None:
    item, reasons = _check_claim(_power_claim(claim), remark, family)

    assert item.publication_use == "CONTEXT"
    assert reasons == ("ungrounded_service_family",)


def _incident_links(timeline: ConversationTimeline, labels: Any, own: str) -> tuple[int, ...]:
    return timeline.same_incident_story_ids(
        labels, story_id=30, own_texts=[own], scope=SCOPE, geo_context=GEO
    )


def test_a_remark_with_no_place_can_join_an_incident_with_one() -> None:
    timeline = _chat((BLASTS, 0, None, (31,)), (DOORS, 1, None, (30,)))

    assert _incident_links(timeline, ["C1"], DOORS) == (31,)


def test_reports_naming_the_same_place_can_join_one_incident() -> None:
    timeline = _chat((BLASTS, 0, None, (31,)), ("Акз опять сильно", 1, None, (30,)))

    assert _incident_links(timeline, ["C1"], "Акз опять сильно") == (31,)


def test_reports_naming_different_places_are_not_one_incident() -> None:
    timeline = _chat((BLASTS, 0, None, (31,)), ("Колония нету", 1, None, (30,)))

    assert _incident_links(timeline, ["C1"], "Колония нету") == ()


def test_lines_across_a_pause_are_not_one_incident() -> None:
    timeline = _chat((BLASTS, 0, None, (31,)), (DOORS, 200, None, (30,)))

    assert _incident_links(timeline, ["C1"], DOORS) == ()


def test_availability_talk_in_one_conversation_is_not_an_incident() -> None:
    timeline = _chat(
        ("я так понимаю нагорную сегодня опять не включат?", 0, None, (31,)),
        ("Выключили", 1, None, (32,)),
        ("Пытаюсь дозвониться диспетчеру", 2, None, (30,)),
    )

    assert _incident_links(timeline, ["C1", "C2"], "Пытаюсь дозвониться диспетчеру") == ()


@pytest.mark.parametrize(
    ("line", "own"),
    [
        # The event is on the neighbouring line, the remark only reacts.
        ("Прорвало трубу во дворе, течёт по улице", "Вода уже в подъезде"),
        # The Story itself names the event and the line reacts to it.
        ("Ого, вот это было", "Авария на подстанции, всё погасло"),
        # A blast residents describe only by its sound.
        ("Как бахнуло только что", "Весь дом трясло"),
    ],
)
def test_a_named_event_on_either_side_allows_the_link(line: str, own: str) -> None:
    timeline = _chat((line, 0, None, (31,)), (own, 1, None, (30,)))

    assert _incident_links(timeline, ["C1"], own) == (31,)


def test_a_story_absent_from_the_timeline_has_no_incident_links() -> None:
    timeline = _chat((BLASTS, 0, None, (31,)))

    assert _incident_links(timeline, ["C1"], DOORS) == ()


def _quote_check(text: str, *, own: str, context: str) -> tuple[str, ...]:
    return context_only_details(
        text, own_texts=[own], context_texts=[context], scope=SCOPE, geo_context=GEO
    )


def test_a_quote_lifted_from_a_neighbouring_line_is_borrowed() -> None:
    assert _quote_check(
        "На вопрос ответили: «Вроде как есть»",
        own="На Пионерской 65 есть свет?",
        context="Вроде как есть",
    ) == ("вроде как есть",)


def test_a_storys_own_quote_is_not_borrowed() -> None:
    assert (
        _quote_check(
            "Жилец написал: «Двери чуть не вынесло»",
            own=DOORS,
            context=DOORS,
        )
        == ()
    )
    assert _quote_check("Написали: «всё вынесло»", own="Все вынесло", context="Все вынесло") == ()


class Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.position = iter(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return next(self.position, None)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __aiter__(self) -> Cursor:
        return self

    async def __anext__(self) -> tuple[Any, ...]:
        row = next(self.position, None)
        if row is None:
            raise StopAsyncIteration
        return row


class ChatConnection:
    """In-memory SQL boundary for one remark next to the report it reacts to."""

    def __init__(self, fragments: list[tuple[Any, ...]] | None = None) -> None:
        self.fragments = fragments or [(30, 301, DOORS, 3, "Город", "other", NOW, None)]
        self.links: list[dict[str, Any]] = []
        self.timeline_params: dict[str, Any] | None = None
        self.timeline_extra = "Свет дали"
        self.timeline_rows: list[tuple[Any, ...]] | None = None
        self.parent_rows: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: Any = ()) -> Cursor:
        query = " ".join(sql.split())
        if "FROM story_fragments sf" in query and "FROM source_items" not in query.split("sf")[0]:
            if "WITH anchor" not in query:
                return Cursor(self.fragments)
        if "WITH anchor" in query:
            self.timeline_params = params
            if self.timeline_rows is not None:
                return Cursor(self.timeline_rows)
            return Cursor(
                [
                    (1, 3, "Город", None, NOW - dt.timedelta(minutes=1), BLASTS, [31], []),
                    (2, 3, "Город", None, NOW, DOORS, [30], []),
                    (3, 3, "Город", None, NOW, self.timeline_extra, [32], []),
                ]
            )
        if "JOIN source_fragments sib" in query:
            return Cursor([])
        if "max_rev" in query:
            return Cursor(self.parent_rows)
        if query.startswith("SELECT slug FROM editions"):
            return Cursor([("berdyansk",)])
        if query.startswith("INSERT INTO story_event_triage_runs"):
            return Cursor([(1,)])
        if query.startswith("INSERT INTO story_relations"):
            self.links.append(params)
            return Cursor([])
        if query.startswith("INSERT INTO story_"):
            return Cursor([])
        raise AssertionError(f"Unexpected SQL boundary: {query}")


class GateAnswer:
    def __init__(self, *, evidence: str = DOORS, headline: str = "", links: Any = ("C1",)) -> None:
        self.evidence = evidence
        self.headline = headline
        self.links = links
        self.user_prompts: list[str] = []

    async def chat_completion(self, **kwargs: Any) -> str:
        self.user_prompts.append(kwargs["messages"][1]["content"])
        item: dict[str, Any] = {
            "story_id": 30,
            "scope": "LOCAL",
            "scope_confidence": 0.8,
            "scope_reason": "Реплика продолжает обсуждение взрывов.",
            "scope_basis_fragment_ids": [301],
            "retention": "KEEP",
            "enrichment": "BRIEF",
            "confidence": 0.8,
            "reason": "Наблюдение очевидца.",
            "exclusion_reason": None,
            "brief_payload": {
                "publishability": "brief",
                "headline": self.headline,
                "evidence_items": [
                    {
                        "text": self.evidence,
                        "kind": "community_report",
                        "publication_use": "PUBLISH",
                        "source_fragment_ids": [301],
                    }
                ],
            },
        }
        if self.links is not None:
            item["same_incident_context_ids"] = list(self.links)
        return json.dumps({"results": [item]})


async def _gate(
    answer: GateAnswer,
    conn: ChatConnection,
    *,
    max_gate_fragments: int = 6,
    max_input_chars: int | None = None,
) -> Any:
    service = StoryTriageService(answer, conversation_timeline=ConversationTimelineSettings())

    async def no_hints(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    service._load_recent_subject_hints = no_hints  # type: ignore[method-assign]
    story = StoryClusterState(30, [], "test", 0, 1, 1, NOW, NOW, 1, None, None, True, NOW)
    return await service.triage_stories_batch(
        conn,  # type: ignore[arg-type]
        [story],
        edition_id=1,
        scope_config=SCOPE,
        scope_hash="test",
        source_cutoff_at=NOW,
        max_gate_fragments=max_gate_fragments,
        max_input_chars=max_input_chars,
    )


@pytest.mark.asyncio
async def test_gate_reads_the_chat_and_records_the_incident_link() -> None:
    answer = GateAnswer()
    conn = ChatConnection()

    result = await _gate(answer, conn)

    prompt = answer.user_prompts[0]
    assert prompt.index("CHAT TIMELINE") < prompt.index("Stories for Gate V2")
    assert BLASTS in prompt
    assert conn.timeline_params is not None
    assert conn.timeline_params["cutoff"] == NOW
    assert result.results[0].retention == "KEEP"
    assert result.results[0].same_incident_story_ids == (31,)
    assert [(link["story"], link["linked"]) for link in conn.links] == [(30, 31)]


@pytest.mark.asyncio
async def test_timeline_does_not_use_up_the_batch_input_budget() -> None:
    first = GateAnswer()
    await _gate(first, ChatConnection())
    prompt = first.user_prompts[0]
    separator = len("\n\n")
    timeline_chars = prompt.index("Stories for Gate V2") - prompt.index("CHAT TIMELINE") - separator

    result = await _gate(
        GateAnswer(), ChatConnection(), max_input_chars=len(prompt) - timeline_chars
    )

    assert result.batch_error_kind != "input_too_large"
    assert [gate.story_id for gate in result.results] == [30]


@pytest.mark.asyncio
async def test_a_missing_link_field_means_no_link() -> None:
    conn = ChatConnection()

    result = await _gate(GateAnswer(links=None), conn)

    assert result.results[0].same_incident_story_ids == ()
    assert conn.links == []


@pytest.mark.asyncio
async def test_evidence_that_borrows_a_neighbouring_place_is_not_published() -> None:
    result = await _gate(
        GateAnswer(evidence="В районе АКЗ двери чуть не вынесло"), ChatConnection()
    )

    assert result.invalid_story_ids == (30,)
    assert result.results == ()


@pytest.mark.asyncio
async def test_headline_that_borrows_a_neighbouring_place_goes_to_recovery() -> None:
    result = await _gate(GateAnswer(headline="Взрывы в районе АКЗ"), ChatConnection())

    assert result.invalid_story_ids == (30,)
    assert result.invalid_reasons[30] == "context_detail_in_summary"


@pytest.mark.asyncio
async def test_the_day_a_message_was_posted_is_not_a_borrowed_number() -> None:
    conn = ChatConnection()
    conn.timeline_extra = "Сегодня 8 числа ночью было громко"

    result = await _gate(GateAnswer(evidence="8 ноября двери чуть не вынесло"), conn)

    assert [gate.story_id for gate in result.results] == [30]


@pytest.mark.asyncio
async def test_the_first_report_stays_visible_when_replies_fill_the_sample() -> None:
    fragments = [
        (30, 310 - i, f"реплика номер {i} про двери", 3, "Город", "other", NOW, None)
        for i in range(8)
    ]
    fragments[-1] = (30, 301, "Первое сообщение про взрывы", 3, "Город", "other", NOW, None)
    answer = GateAnswer()

    await _gate(answer, ChatConnection(fragments), max_gate_fragments=2)

    assert "Первое сообщение про взрывы" in answer.user_prompts[0]


def _remark_in_chat(*earlier: str) -> ChatConnection:
    """A connection where one unnamed-service remark follows the given lines."""
    conn = ChatConnection([(30, 301, "Колония нету", 3, "Город", "other", NOW, None, 90)])
    conn.timeline_rows = [
        (number, 3, "Город", None, NOW - dt.timedelta(minutes=5 - number), text, [number], [])
        for number, text in enumerate(earlier, start=1)
    ]
    conn.timeline_rows.append((90, 3, "Город", None, NOW, "Колония нету", [30], []))
    return conn


@pytest.mark.asyncio
async def test_gate_keeps_a_service_only_the_conversation_names() -> None:
    conn = _remark_in_chat("Света нет с утра", "Когда дадут свет")

    result = await _gate(GateAnswer(evidence="В Колонии нет света", links=[]), conn)

    assert result.invalid_story_ids == ()
    assert result.results[0].brief_payload.evidence_items[0].publication_use == "PUBLISH"


@pytest.mark.asyncio
async def test_gate_does_not_guess_a_service_when_several_are_discussed() -> None:
    conn = _remark_in_chat("Света нет с утра", "Воды тоже нет")

    result = await _gate(GateAnswer(evidence="В Колонии нет света", links=[]), conn)

    assert result.invalid_story_ids == (30,)
    assert result.results == ()


@pytest.mark.asyncio
async def test_gate_does_not_record_a_link_to_a_report_from_another_place() -> None:
    conn = ChatConnection([(30, 301, "В Колонии двери ходуном", 3, "Город", "other", NOW, None, 2)])

    result = await _gate(GateAnswer(evidence="В Колонии двери ходуном"), conn)

    assert result.results[0].same_incident_story_ids == ()
    assert conn.links == []


@pytest.mark.asyncio
async def test_official_area_name_for_slang_in_a_parent_is_not_borrowed() -> None:
    conn = ChatConnection([(30, 301, "Нету", 3, "Город", "other", NOW, 77, 2)])
    conn.parent_rows = [(77, "На 8-хе свет дали?")]
    conn.timeline_extra = "На 8 Марта тоже темно"

    result = await _gate(GateAnswer(evidence="На 8 Марта нету", links=[]), conn)

    assert result.invalid_story_ids == ()
    assert [gate.story_id for gate in result.results] == [30]
