"""Tests for PublicationEvidence domain model and adapter evidence generation."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.evidence import PublicationEvidence
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import PublicationPolicyRepository, PublicationRepository

_T1 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
_T2 = dt.datetime(2026, 8, 29, 10, 15, tzinfo=dt.timezone.utc)


def test_publication_evidence_dataclass() -> None:
    pe = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="Substation outage",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="telegram:source:1:item:1:rev:1:frag:101",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_T1,
    )
    assert pe.evidence_id == "story:1:evidence:0:frag:101"
    assert pe.story_id == 1
    assert pe.publication_use == "PUBLISH"
    assert pe.observed_at == _T1


@pytest.mark.postgres
async def test_adapter_produces_separately_timestamped_evidence_for_multi_fragment_fact(
    conn, pool, edition
):
    """Given one structured fact citing fragments 101 and 102 with distinct timestamps,

    the adapter must produce two PublicationEvidence rows with distinct observed_at times.
    """
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-evi", prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-evi", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-evi", prompt_version="v1"
    )

    # 1. Create story
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _T1),
    )
    story_id = (await cur.fetchone())[0]

    # 2. Create source & items with distinct timestamps
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10088', 'https://t.me/src', 'Channel', 'official')
        RETURNING id
        """
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )

    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
        VALUES (%s, 'message', 'msg-1', %s, %s)
        RETURNING id
        """,
        (src_id, _T1, _T1),
    )
    item1_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
        VALUES (%s, 'message', 'msg-2', %s, %s)
        RETURNING id
        """,
        (src_id, _T2, _T2),
    )
    item2_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-r1', 'Text 1'), (%s, 1, 'h-r2', 'Text 2')
        RETURNING id
        """,
        (item1_id, item2_id),
    )
    sir_rows = await cur.fetchall()
    sir1_id, sir2_id = sir_rows[0][0], sir_rows[1][0]

    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Fragment 1', 'hf1', 'v1', TRUE, %s),
               (%s, 0, 'Fragment 2', 'hf2', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir1_id, _T1, sir2_id, _T2),
    )
    f_rows = await cur.fetchall()
    f1_id, f2_id = f_rows[0][0], f_rows[1][0]

    # Story revision with 1 evidence item citing BOTH f1_id and f2_id
    event_payload = {
        "topic": "Power outage",
        "headline": "Outage in Center",
        "digest_summary": "Substation broke down.",
        "evidence_items": [
            {
                "text": "Substation accident caused blackout",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [f1_id, f2_id],
            }
        ],
        "operational_observations": [
            {
                "subject_key": "power_supply",
                "subject_label": "Power",
                "dimension": "availability",
                "location": "Center",
                "entity": "grid",
                "state": "UNAVAILABLE",
                "detail": "Outage",
                "source_fragment_ids": [f1_id, f2_id],
            }
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-rev-evi', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            story_id,
            event_payload["digest_summary"],
            event_payload["headline"],
            event_payload["digest_summary"],
            json.dumps(event_payload),
            _T1,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-evidence-key",
        snapshot_at=_T2,
        policy_ids=(elig.id, sel.id, wri.id),
    )

    cand = await repo.insert_candidate(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        deterministic_rank=1,
    )
    dec = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_T2,
        ),
    )
    pub_input = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[f1_id, f2_id],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial_input = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    evidence_map = editorial_input.analysis.evidence
    assert len(evidence_map) == 2

    evi1_id = f"story:{story_id}:evidence:0:frag:{f1_id}"
    evi2_id = f"story:{story_id}:evidence:0:frag:{f2_id}"

    assert evi1_id in evidence_map
    assert evi2_id in evidence_map

    assert evidence_map[evi1_id].observed_at == _T1
    assert evidence_map[evi2_id].observed_at == _T2
    assert evidence_map[evi1_id].publication_use == "PUBLISH"
    assert evidence_map[evi2_id].publication_use == "PUBLISH"
