"""Running-story memory keeps only source-grounded background."""

# ruff: noqa: S101
import asyncio
import datetime as dt
import json

from src.publication.situation_memory import (
    MemoryReport,
    MemorySnapshot,
    apply_memory_operations,
    digest_background,
)

AS_OF = dt.datetime(2026, 10, 7, 4, 20, tzinfo=dt.UTC)


def _report(fragment_id: int, text: str, *, days_ago: int = 1) -> MemoryReport:
    return MemoryReport(
        ref=f"fragment:{fragment_id}",
        story_id=fragment_id,
        observed_at=AS_OF - dt.timedelta(days=days_ago),
        evidence_text=text,
        source_text=text,
    )


REPORTS = [
    _report(1, "По словам жителя, света нет с 3 августа."),
    _report(2, "Жители пишут, что подстанции разбили, свет не дают."),
]


def _add(**overrides):
    op = {
        "op": "ADD",
        "title": "Перебои с электроснабжением",
        "service": "power",
        "status": "ongoing",
        "summary": "По словам жителей, в городе длительно нет стабильного электроснабжения.",
        "summary_refs": ["fragment:1", "fragment:2"],
        "since_text": "по словам жителя, с 3 августа",
        "since_refs": ["fragment:1"],
        "causes": [{"text": "жители пишут, что подстанции разбили", "refs": ["fragment:2"]}],
        "open_questions": ["сроки восстановления не названы"],
    }
    op.update(overrides)
    return op


def test_grounded_situation_is_added_with_temporal_bookkeeping():
    situations, ref_texts, stats = apply_memory_operations(
        None, [_add()], reports=REPORTS, as_of=AS_OF
    )
    assert stats["applied"] == 1
    [situation] = situations
    assert situation["since_text"] == "по словам жителя, с 3 августа"
    assert situation["causes"][0]["refs"] == ["fragment:2"]
    assert situation["first_seen_at"] <= situation["last_confirmed_at"]
    assert set(ref_texts) == {"fragment:1", "fragment:2"}


def test_invented_date_and_unknown_ref_are_dropped():
    situations, _, stats = apply_memory_operations(
        None,
        [
            _add(
                since_text="с 1 июля",
                causes=[{"text": "удар 12 сентября", "refs": ["fragment:999"]}],
            )
        ],
        reports=REPORTS,
        as_of=AS_OF,
    )
    [situation] = situations
    assert situation["since_text"] == ""
    assert situation["causes"] == []
    assert stats["dropped_fields"] == 2


def test_add_without_grounded_summary_is_rejected():
    situations, _, stats = apply_memory_operations(
        None, [_add(summary="Света нет 40 дней.", summary_refs=["fragment:1"])],
        reports=REPORTS,
        as_of=AS_OF,
    )
    assert situations == []
    assert stats["rejected"] == 1


def test_update_merges_and_stale_situation_ages_out():
    first, refs, _ = apply_memory_operations(None, [_add()], reports=REPORTS, as_of=AS_OF)
    previous = MemorySnapshot(1, 1, AS_OF, tuple(first), refs)
    later = AS_OF + dt.timedelta(days=1)
    change = _report(3, "На АКЗ свет дали на два часа.", days_ago=0)
    updated, _, _ = apply_memory_operations(
        previous,
        [
            {
                "op": "UPDATE",
                "situation_id": first[0]["situation_id"],
                "latest_change": {"text": "на АКЗ свет дали на два часа", "refs": ["fragment:3"]},
            }
        ],
        reports=[change],
        as_of=later,
    )
    assert updated[0]["since_text"] == "по словам жителя, с 3 августа"
    assert "fragment:3" in updated[0]["refs"]
    assert updated[0]["latest_change"]["text"] == "на АКЗ свет дали на два часа"

    stale, _, _ = apply_memory_operations(
        MemorySnapshot(2, 1, later, tuple(updated), refs),
        [],
        reports=[],
        as_of=later + dt.timedelta(days=40),
    )
    assert stale == []


def test_digest_background_hides_refs_and_resolved_situations():
    situations, refs, _ = apply_memory_operations(None, [_add()], reports=REPORTS, as_of=AS_OF)
    resolved = {**situations[0], "situation_id": "sit:x", "status": "resolved"}
    snapshot = MemorySnapshot(1, 1, AS_OF, (situations[0], resolved), refs)
    [row] = digest_background(snapshot, as_of=AS_OF)
    assert row["since"] == "по словам жителя, с 3 августа"
    assert "refs" not in json.dumps(row)


def test_writer_prompt_carries_background_as_non_citable_context():
    from dataclasses import replace

    from digest_evaluation_helpers import assessment_inputs

    from src.publication.digest_narrative import DigestNarrativeWriter

    values, _ = assessment_inputs()
    plan = replace(values["plan"], background=({"title": "Перебои с электроснабжением"},))
    captured = {}

    class Provider:
        async def chat_completion(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

    writer = DigestNarrativeWriter(Provider())
    try:
        asyncio.run(
            writer.generate_narrative_draft(
                plan=plan,
                cards=list(values["frozen"].analysis.cards),
                evidence=values["evidence"],
                language="Russian",
            )
        )
    except RuntimeError:
        pass
    system, user = (message["content"] for message in captured["messages"])
    assert "never citable evidence" in system
    assert json.loads(user)["edition_background"][0]["title"] == "Перебои с электроснабжением"
