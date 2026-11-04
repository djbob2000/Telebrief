# ruff: noqa: S101
"""A split paragraph keeps the geography its own source message states.

Regression: article run 370 (4 Nov 2026) presented demolitions in Mariupol's
Slobidka and Havan as Berdyansk news. The post's headline paragraph named
Mariupol; its bullet paragraph named only "Slobidka" and clustered into a Story
of its own, so Gate saw a Berdyansk neighborhood name and no external city.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from src.config_loader import load_config
from src.domain.edition_geography import resolve_edition_geography
from src.domain.event_clusters import StoryClusterState
from src.processing import event_triage
from src.processing.edition_scope import (
    FragmentMessageContext,
    fragment_placed_outside_by_message,
    story_placed_outside_by_message,
)
from src.processing.event_triage import StoryTriageService

SCOPE = load_config("config.yaml").settings.edition_scopes["berdyansk"]
GEO = resolve_edition_geography("berdyansk", SCOPE.name)
NOW = dt.datetime(2026, 11, 4, 9, 9, tzinfo=dt.timezone.utc)

EXTERNAL_HEADLINE = (
    "Окупанти зносять будинки маріупольців. У тимчасово окупованому Маріуполі "
    "адміністрація готує нові знесення, під забудову віддадуть Слобідку та Гавань."
)
UNLOCATED_BULLETS = (
    "▶️Окупаційна адміністрація готує нові знесення багатоповерхівок і приватних будинків. "
    "▶️Під забудову планують віддати території Слобідки та Гавані."
)
EXTERNAL_LINK_LINE = "Детальніше про знесення будинків в окупованому Маріуполі — за посиланням."


def placed_outside(fragment: FragmentMessageContext) -> bool:
    return fragment_placed_outside_by_message(fragment, scope=SCOPE, geo_context=GEO)


def test_paragraph_inherits_external_city_from_earlier_headline() -> None:
    fragment = FragmentMessageContext(
        text=UNLOCATED_BULLETS,
        earlier_message_texts=(EXTERNAL_HEADLINE,),
        later_message_texts=(EXTERNAL_LINK_LINE,),
    )

    assert placed_outside(fragment)


def test_message_naming_the_edition_city_keeps_the_paragraph_local() -> None:
    fragment = FragmentMessageContext(
        text="Под застройку отдадут Слободку.",
        earlier_message_texts=("В Мариуполе и Бердянске готовят снос домов.",),
    )

    assert not placed_outside(fragment)


def test_external_city_only_after_the_paragraph_is_left_to_gate() -> None:
    # "No light in Slobodka. And in Zaporizhzhia it came back." must stay a local report.
    fragment = FragmentMessageContext(
        text="В Слободке нет света.",
        later_message_texts=("А в Запорожье дали.",),
    )

    assert not placed_outside(fragment)


def test_first_person_locator_keeps_the_author_local() -> None:
    fragment = FragmentMessageContext(
        text="У нас на Слободке света нет с утра.",
        earlier_message_texts=("Вчера ездила в Запорожье к врачу.",),
    )

    assert not placed_outside(fragment)


def test_single_paragraph_message_is_not_judged_by_this_rule() -> None:
    fragment = FragmentMessageContext(text="В Слободке нет света, а в Запорожье дали.")

    assert not placed_outside(fragment)


def test_unrelated_local_report_in_the_same_story_keeps_the_story() -> None:
    external = FragmentMessageContext(
        text=UNLOCATED_BULLETS, earlier_message_texts=(EXTERNAL_HEADLINE,)
    )
    local_reply = FragmentMessageContext(text="На Слободке тоже говорят про снос домов.")

    assert story_placed_outside_by_message(
        basis_fragments=[external],
        story_fragments=[external],
        scope=SCOPE,
        geo_context=GEO,
    )
    assert not story_placed_outside_by_message(
        basis_fragments=[external],
        story_fragments=[external, local_reply],
        scope=SCOPE,
        geo_context=GEO,
    )


def test_gate_excerpt_shows_message_context_as_non_citable() -> None:
    formatted = event_triage.format_gate_fragment_excerpt(
        {
            "fragment_id": 587389,
            "observed_at": NOW,
            "source_role": "other",
            "source_name": "channel",
            "text": UNLOCATED_BULLETS,
        },
        None,
        EXTERNAL_HEADLINE,
    )

    context_line = next(line for line in formatted.splitlines() if "Маріуполі" in line)
    assert "SAME-MESSAGE CONTEXT ONLY — NOT CITABLE" in context_line
    assert "frag=" not in context_line


def test_message_excerpt_skips_place_free_paragraphs() -> None:
    context = FragmentMessageContext(
        text=UNLOCATED_BULLETS,
        earlier_message_texts=("Підписуйтесь на наш канал.", EXTERNAL_HEADLINE),
    )

    excerpt = event_triage.same_message_geography_excerpt(context, scope=SCOPE, geo_context=GEO)

    assert excerpt is not None
    assert "Маріуполі" in excerpt
    assert "Підписуйтесь" not in excerpt
    assert len(excerpt) <= event_triage.SAME_MESSAGE_CONTEXT_CHARS
    no_place = FragmentMessageContext(text="x", later_message_texts=("Підписуйтесь.",))
    assert (
        event_triage.same_message_geography_excerpt(no_place, scope=SCOPE, geo_context=GEO) is None
    )


def test_gate_prompt_lets_message_geography_override_shared_neighborhood_names() -> None:
    prompt = event_triage._GATE_V2_SYSTEM_PROMPT.casefold()

    assert "unless the same message explicitly places them in another city or region" in prompt
    assert "district and neighborhood names repeat across cities" in prompt
    assert "same-message context" in prompt
    assert "never cite same-message context" in prompt


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


class SplitPostConnection:
    """In-memory SQL boundary for one Story holding the middle paragraph of a post."""

    def __init__(self) -> None:
        self.scope_rows: list[tuple[Any, ...]] = []
        self.decisions: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        query = " ".join(sql.split())
        if "FROM story_fragments sf" in query:
            return Cursor([(20, 201, UNLOCATED_BULLETS, 3, "channel", "other", NOW, None)])
        if "JOIN source_fragments sib" in query:
            assert params == ([201],)
            return Cursor([(201, True, EXTERNAL_HEADLINE), (201, False, EXTERNAL_LINK_LINE)])
        if query.startswith("SELECT slug FROM editions"):
            return Cursor([("berdyansk",)])
        if query.startswith("INSERT INTO story_event_triage_runs"):
            return Cursor([(1,)])
        if query.startswith("INSERT INTO story_event_triage_decisions"):
            self.decisions.append(params)
            return Cursor([])
        if query.startswith("INSERT INTO story_edition_scope_decisions"):
            self.scope_rows.append(params)
            return Cursor([])
        raise AssertionError(f"Unexpected SQL boundary: {query}")


class LocalClaimingProvider:
    """Reproduces the production answer: Slobidka read as a Berdyansk district."""

    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    async def chat_completion(self, **kwargs: Any) -> str:
        self.user_prompts.append(kwargs["messages"][1]["content"])
        return json.dumps(
            {
                "results": [
                    {
                        "story_id": 20,
                        "scope": "LOCAL",
                        "scope_confidence": 0.85,
                        "scope_reason": "Упоминаются Слободка и Гавань — районы Бердянска.",
                        "scope_basis_fragment_ids": [201],
                        "retention": "KEEP",
                        "enrichment": "BRIEF",
                        "confidence": 0.75,
                        "reason": "Планы сноса жилья.",
                        "exclusion_reason": None,
                        "brief_payload": {
                            "publishability": "brief",
                            "evidence_items": [
                                {
                                    "text": "Под застройку планируют отдать Слободку и Гавань.",
                                    "kind": "community_report",
                                    "publication_use": "PUBLISH",
                                    "source_fragment_ids": [201],
                                }
                            ],
                        },
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_gate_drops_split_paragraph_of_external_post(monkeypatch) -> None:
    provider = LocalClaimingProvider()
    service = StoryTriageService(provider)

    async def no_hints(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(service, "_load_recent_subject_hints", no_hints)
    conn = SplitPostConnection()
    story = StoryClusterState(20, [], "test", 0, 1, 1, NOW, NOW, 1, None, None, True, NOW)

    result = await service.triage_stories_batch(
        conn,
        [story],
        edition_id=1,
        scope_config=SCOPE,
        scope_hash="test",
        source_cutoff_at=NOW,
    )

    assert "SAME-MESSAGE CONTEXT ONLY — NOT CITABLE" in provider.user_prompts[0]
    assert "Маріуполі" in provider.user_prompts[0]
    gate = result.results[0]
    assert gate.scope == "OUT_OF_SCOPE"
    assert gate.retention == "DROP"
    assert gate.enrichment == "NONE"
    assert "Same source message" in gate.scope_reason
