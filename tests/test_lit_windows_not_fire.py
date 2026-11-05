# ruff: noqa: S101
"""Lit-up apartment blocks report electric light, not a fire.

Regression: digest 158 (5 Nov 2026) placed "На Пролетарском проспекте горят
девятиэтажки" under "Безопасность и чрезвычайные ситуации" with a 🔥. The
resident had written, in reply to "в других районах есть, а колония крайняя",
that the nine-storey blocks along the railway "все горят" — their windows were
lit during a blackout. Gate titled the Story "Пожар в девятиэтажках". Story 89758
("Пожар на улице Гайдара") had the same cause: "минут 10 назад была на гайдара,
все горит", in reply to a message about power being restored.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from src.config_loader import load_config
from src.domain.event_clusters import StoryClusterState
from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.domain.service_taxonomy import (
    detect_service_families,
    has_explicit_fire_signal,
    reports_lit_buildings,
)
from src.processing.event_analysis import _EVENT_ANALYSIS_SYSTEM_PROMPT
from src.processing.event_triage import _GATE_V2_SYSTEM_PROMPT, StoryTriageService
from src.processing.fire_reading import payload_claims_fire, unsupported_fire_reading
from src.processing.operational_semantics import normalize_service_state_evidence

SCOPE = load_config("config.yaml").settings.edition_scopes["berdyansk"]
NOW = dt.datetime(2026, 11, 4, 15, 30, tzinfo=dt.timezone.utc)

LIT_REPLY = "я глянул - Пролетарский ( по ЖД) девятиэтажки все горят!😳"
PARENT = "Так в других районах есть, а колония, как всегда крайняя."
FRAGMENT_ID = 586490
PARENT_ITEM_ID = 1193167


@pytest.mark.parametrize(
    "text",
    [
        LIT_REPLY,
        "окна горят на Победе",
        "в полумраке горят окна",
        "фонари горят по Центральной",
        "весь район горит",
        "А багатоповерхівки горять",
    ],
)
def test_lit_buildings_read_as_electric_light(text: str) -> None:
    assert reports_lit_buildings(text)
    assert "power" in detect_service_families(text)


@pytest.mark.parametrize(
    "text",
    [
        "На Лисках горит трава у дороги",
        "На Морской горит машина",
        "Девятиэтажка горит, дым валит",
        "пожарные тушат девятиэтажку",
        "Горит дом на Лазурной",
        "У нас сроки горят",
    ],
)
def test_fires_and_idioms_are_not_lit_buildings(text: str) -> None:
    assert not reports_lit_buildings(text)


def test_explicit_fire_signals() -> None:
    assert has_explicit_fire_signal("Пожар в девятиэтажках")
    assert has_explicit_fire_signal("дым валит из окон")
    assert has_explicit_fire_signal("приехало МЧС")
    assert not has_explicit_fire_signal(LIT_REPLY)
    assert not has_explicit_fire_signal("в полумраке горят окна")


def fire_payload() -> EventPayload:
    """The production Gate brief for Story 91223."""
    return EventPayload(
        topic="Пожар в девятиэтажках на Пролетарском проспекте",
        tags=("Бердянск", "пожар", "девятиэтажки"),
        urgency="high",
        publishability="brief",
        headline="Житель сообщает о горящих девятиэтажках на Пролетарском проспекте",
        digest_summary="По сообщению жителя, на Пролетарском проспекте горят девятиэтажные дома.",
        evidence_items=(
            EvidenceItemPayload(
                text="По сообщению жителя, на Пролетарском проспекте горят все девятиэтажки.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(FRAGMENT_ID,),
            ),
        ),
        enrichment_level="brief",
    )


def light_evidence() -> EvidenceItemPayload:
    return EvidenceItemPayload(
        text="По сообщению жителя, в девятиэтажках на Пролетарском у железной дороги горит свет.",
        kind="service_access",
        publication_use="PUBLISH",
        source_fragment_ids=(FRAGMENT_ID,),
        service_state=ServiceStatePayload(
            subject_key="power_supply",
            subject_label="Электроснабжение",
            dimension="availability",
            state="AVAILABLE",
            location="девятиэтажки на Пролетарском у ЖД",
            basis="normal_operation",
        ),
    )


def test_fire_payload_over_lit_buildings_is_a_misreading() -> None:
    payload = fire_payload()

    assert payload_claims_fire(payload)
    assert unsupported_fire_reading(payload, source_texts=[LIT_REPLY], context_texts=[PARENT])


def test_fire_named_in_parent_or_source_keeps_the_fire_reading() -> None:
    payload = fire_payload()

    assert not unsupported_fire_reading(
        payload, source_texts=[LIT_REPLY], context_texts=["Пожар на Пролетарском!"]
    )
    assert not unsupported_fire_reading(payload, source_texts=[LIT_REPLY, "Дым видно с Азовской"])


def test_street_that_all_burns_after_power_returned_is_a_misreading() -> None:
    payload = EventPayload(topic="Пожар на улице Гайдара", enrichment_level="brief")

    assert unsupported_fire_reading(
        payload,
        source_texts=["реально, минут 10 назад была на гайдара, все горит"],
        context_texts=["Дом напротив рынка: у них позавчера был и сегодня снова им дали"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "На Лисках горит трава у дороги",
        "Горит дом на Лазурной",
        "Прилёт в порту, всё горит",
        "горит склад после прилета шахеда",
    ],
)
def test_fire_object_or_incident_keeps_the_fire_reading(source: str) -> None:
    payload = EventPayload(topic="Пожар", enrichment_level="brief")

    assert not unsupported_fire_reading(payload, source_texts=[source])


def test_fire_without_burning_verb_is_left_to_gate() -> None:
    payload = EventPayload(topic="Возможный пожар", enrichment_level="brief")

    assert not unsupported_fire_reading(payload, source_texts=["Что там за зарево над портом?"])


def test_light_reading_is_not_flagged() -> None:
    payload = EventPayload(
        topic="Свет в девятиэтажках у ЖД",
        evidence_items=(light_evidence(),),
        enrichment_level="brief",
    )

    assert not unsupported_fire_reading(payload, source_texts=[LIT_REPLY])


def test_power_state_for_lit_buildings_is_grounded() -> None:
    payload = EventPayload(evidence_items=(light_evidence(),), enrichment_level="brief")

    normalized, audit = normalize_service_state_evidence(
        payload,
        {FRAGMENT_ID: LIT_REPLY},
        reply_parent_context_by_fragment_id={FRAGMENT_ID: PARENT},
        edition_name=SCOPE.name,
    )

    assert audit.rejected_count == 0
    item = normalized.evidence_items[0]
    assert item.publication_use == "PUBLISH"
    assert item.service_state is not None
    assert item.service_state.state == "AVAILABLE"


def test_gate_and_analysis_prompts_explain_lit_windows() -> None:
    for prompt in (_GATE_V2_SYSTEM_PROMPT, _EVENT_ANALYSIS_SYSTEM_PROMPT):
        assert "LIT WINDOWS VS FIRE" in prompt
        assert "normally means that the electric light is on there" in prompt
        assert "Never derive a fire, emergency, or safety topic" in prompt


class Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = iter(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return next(self.rows, None)

    def __aiter__(self) -> Cursor:
        return self

    async def __anext__(self) -> tuple[Any, ...]:
        row = next(self.rows, None)
        if row is None:
            raise StopAsyncIteration
        return row


class ReplyConnection:
    """In-memory SQL boundary for the one-message Story and its reply parent."""

    def __init__(self) -> None:
        self.decisions: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        query = " ".join(sql.split())
        if "FROM story_fragments sf" in query:
            return Cursor(
                [(30, FRAGMENT_ID, LIT_REPLY, 1, "chat", "community", NOW, PARENT_ITEM_ID)]
            )
        if "SELECT sir.source_item_id, sir.text_content" in query:
            return Cursor([(PARENT_ITEM_ID, PARENT)])
        if "JOIN source_fragments sib" in query:
            return Cursor([])
        if query.startswith("SELECT slug FROM editions"):
            return Cursor([("berdyansk",)])
        if query.startswith("INSERT INTO story_event_triage_runs"):
            return Cursor([(1,)])
        if query.startswith("INSERT INTO story_event_triage_decisions"):
            self.decisions.append(params)
            return Cursor([])
        if query.startswith("INSERT INTO story_edition_scope_decisions"):
            return Cursor([])
        raise AssertionError(f"Unexpected SQL boundary: {query}")


class GateProvider:
    def __init__(self, brief_payload: dict[str, Any]) -> None:
        self.brief_payload = brief_payload
        self.user_prompts: list[str] = []

    async def chat_completion(self, **kwargs: Any) -> str:
        self.user_prompts.append(kwargs["messages"][1]["content"])
        return json.dumps(
            {
                "results": [
                    {
                        "story_id": 30,
                        "scope": "LOCAL",
                        "scope_confidence": 0.8,
                        "scope_reason": "Пролетарский проспект в Бердянске.",
                        "scope_basis_fragment_ids": [FRAGMENT_ID],
                        "retention": "KEEP",
                        "enrichment": "BRIEF",
                        "confidence": 0.7,
                        "reason": "Сообщение жителя.",
                        "exclusion_reason": None,
                        "brief_payload": self.brief_payload,
                    }
                ]
            }
        )


async def run_gate(monkeypatch, brief_payload: dict[str, Any]) -> Any:
    provider = GateProvider(brief_payload)
    service = StoryTriageService(provider)

    async def no_hints(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(service, "_load_recent_subject_hints", no_hints)
    story = StoryClusterState(30, [], "test", 0, 1, 1, NOW, NOW, 1, None, None, True, NOW)
    result = await service.triage_stories_batch(
        ReplyConnection(),
        [story],
        edition_id=1,
        scope_config=SCOPE,
        scope_hash="test",
        source_cutoff_at=NOW,
    )
    assert PARENT in provider.user_prompts[0]
    return result


@pytest.mark.asyncio
async def test_gate_rejects_unsupported_fire_reading(monkeypatch) -> None:
    result = await run_gate(monkeypatch, fire_payload().to_dict())

    assert result.results == ()
    assert result.invalid_story_ids == (30,)
    assert result.invalid_reasons[30] == "unsupported_fire_reading"


@pytest.mark.asyncio
async def test_gate_keeps_power_reading_of_lit_buildings(monkeypatch) -> None:
    payload = EventPayload(
        topic="Свет в девятиэтажках на Пролетарском",
        tags=("электроснабжение", "свет"),
        publishability="brief",
        headline="В девятиэтажках у ЖД на Пролетарском есть свет",
        digest_summary="По сообщению жителя, в девятиэтажках у железной дороги горит свет.",
        evidence_items=(light_evidence(),),
        enrichment_level="brief",
    )

    result = await run_gate(monkeypatch, payload.to_dict())

    gate = result.results[0]
    assert gate.retention == "KEEP"
    assert gate.brief_payload is not None
    item = gate.brief_payload.evidence_items[0]
    assert item.publication_use == "PUBLISH"
    assert item.service_state is not None
    assert item.service_state.subject_key == "power_supply"
    assert item.service_state.state == "AVAILABLE"
