"""Integration test oracle for the 7-event Berdyansk City Situation golden regression suite."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.db.uow import DatabaseUnitOfWork
from src.domain.event_clusters import StoryClusterState
from src.jobs.event_processing import resolve_edition_scope, scope_config_hash
from src.processing.event_brief import EventBriefService
from src.processing.event_triage import StoryTriageService
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.models import PublicationSelectionDecision
from src.publication.renderers import PublicationDigestRenderer
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def golden_fixture() -> dict[str, Any]:
    fixture_path = (
        Path(__file__).resolve().parent.parent / "fixtures" / "berdyansk_city_situation_golden.json"
    )
    with fixture_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.integration
def test_city_situation_golden_contract(golden_fixture: dict[str, Any]) -> None:
    cases = golden_fixture.get("cases", [])
    negative_cases = golden_fixture.get("negative_cases", [])

    ids = {case["id"] for case in cases}
    assert ids == {
        "electricity_citywide",
        "nikolaevka_grp",
        "local_uav_pvo",
        "connectivity_workarounds",
        "atm_cash",
        "passport_fee",
        "water_koloniya_timeline",
    }

    neg_ids = {case["id"] for case in negative_cases}
    assert neg_ids == {"bucha_fire", "commercial_haircut"}


@pytest.mark.postgres
async def test_berdyansk_city_situation_golden_oracle_pipeline(
    conn, pool, edition, sample_config, golden_fixture
):
    """End-to-end regression oracle testing triage, brief persistence, temporal rollup, and digest rendering."""
    uow = DatabaseUnitOfWork(pool)
    cluster_repo = EventClusterRepository()
    story_repo = StoryRepository()
    pub_repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()
    brief_service = EventBriefService(story_repo=story_repo, cluster_repo=cluster_repo)
    adapter = EventEditorialAdapter(uow=uow, repo=pub_repo)

    cases = golden_fixture["cases"]
    negative_cases = golden_fixture["negative_cases"]
    all_cases = cases + negative_cases

    story_ids_by_case: dict[str, int] = {}
    frag_db_ids: dict[int, int] = {}  # fixture frag_id -> db frag_id
    cluster_states: list[StoryClusterState] = []
    triage_results_payload = []

    source_id_map: dict[int, int] = {}

    # 1. Populate DB with stories, sources, items, revisions, fragments
    for _idx, case in enumerate(all_cases, start=1):
        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
            VALUES (%s, 'active', 'event_first', %s)
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        s_id = (await cur.fetchone())[0]
        story_ids_by_case[case["id"]] = s_id

        last_frag_id = 0
        last_assignment_id = 0

        for f_idx, frag in enumerate(case["fragments"]):
            f_time = dt.datetime.fromisoformat(frag["observed_at"])
            f_sid = int(frag["source_id"])
            raw_role = str(frag.get("source_role", "community"))
            db_role = (
                raw_role
                if raw_role in ("official", "local_media", "community", "individual", "other")
                else "other"
            )
            if f_sid not in source_id_map:
                cur = await conn.execute(
                    """
                    INSERT INTO sources (platform, kind, external_id, url, name, role)
                    VALUES ('telegram', 'channel', %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        f"-100{f_sid}",
                        f"https://t.me/src_{f_sid}",
                        frag.get("source_name", "Source"),
                        db_role,
                    ),
                )
                src_id = (await cur.fetchone())[0]
                await conn.execute(
                    "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (src_id, edition.id),
                )
                source_id_map[f_sid] = src_id
            else:
                src_id = source_id_map[f_sid]

            cur = await conn.execute(
                """
                INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
                VALUES (%s, 'message', %s, %s)
                RETURNING id
                """,
                (src_id, f"msg-{frag['source_item_id']}", f_time),
            )
            item_id = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
                VALUES (%s, 1, %s, %s)
                RETURNING id
                """,
                (item_id, f"h-item-{frag['fragment_id']}", frag["text"]),
            )
            sir_id = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
                VALUES (%s, %s, %s, %s, 'v1', TRUE, %s)
                RETURNING id
                """,
                (sir_id, f_idx, frag["text"], f"h-frag-{frag['fragment_id']}", f_time),
            )
            f_db_id = (await cur.fetchone())[0]
            frag_db_ids[frag["fragment_id"]] = f_db_id
            last_frag_id = f_db_id

            cur = await conn.execute(
                """
                INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
                VALUES (%s, '[0.1, 0.2]'::vector, 'text-embedding-3-small', 2)
                ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET embedding = EXCLUDED.embedding
                RETURNING id
                """,
                (f"h-frag-{frag['fragment_id']}",),
            )

            vec_id = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
                VALUES (%s, %s)
                RETURNING id
                """,
                (f_db_id, vec_id),
            )
            sfe_id = (await cur.fetchone())[0]

            cur = await conn.execute(
                """
                INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind, assigned_at)
                VALUES (%s, %s, %s, 'new_story', %s)
                RETURNING id
                """,
                (s_id, f_db_id, sfe_id, f_time),
            )
            last_assignment_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_cluster_state (
                story_id, centroid, model, dimensions, fragment_count,
                unique_source_count, first_seen_at, last_seen_at,
                latest_assignment_id, analysis_dirty
            ) VALUES (%s, '[0.1, 0.2]'::vector, 'text-embedding-3-small', 2, %s, %s, %s, %s, %s, TRUE)
            """,
            (
                s_id,
                len(case["fragments"]),
                len({f["source_id"] for f in case["fragments"]}),
                _NOW,
                _NOW,
                last_assignment_id,
            ),
        )

        cluster_states.append(
            StoryClusterState(
                story_id=s_id,
                centroid=[0.1, 0.2],
                model="text-embedding-3-small",
                dimensions=2,
                fragment_count=len(case["fragments"]),
                unique_source_count=len({f["source_id"] for f in case["fragments"]}),
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                latest_assignment_id=last_assignment_id,
                last_analyzed_assignment_id=None,
                last_analyzed_at=None,
                analysis_dirty=True,
                updated_at=_NOW,
            )
        )

        # Build simulated AI triage output for this story
        obs_payloads = []
        for exp_obs in case.get("expected_observations", []):
            mapped_fids = [
                frag_db_ids[fid]
                for fid in exp_obs.get("source_fragment_ids", [])
                if fid in frag_db_ids
            ]
            obs_payloads.append(
                {
                    "subject_key": exp_obs["subject_key"],
                    "subject_label": exp_obs["subject_label"],
                    "dimension": exp_obs["dimension"],
                    "location": exp_obs["location"],
                    "entity": exp_obs["entity"],
                    "state": exp_obs["state"],
                    "detail": exp_obs["detail"],
                    "source_fragment_ids": mapped_fids or [last_frag_id],
                }
            )

        brief_dict = None
        if case["expected_retention"] == "KEEP":
            brief_dict = {
                "topic": case.get("topic", f"Story #{s_id}"),
                "headline": case.get("topic", f"Story #{s_id}"),
                "digest_summary": case.get("topic", f"Summary of #{s_id}"),
                "category": case.get("expected_subject_key", "general"),
                "key_facts": [case.get("topic", "Fact")],
                "operational_observations": obs_payloads,
                "confidence_score": 0.95,
                "publishability": "brief",
            }

        triage_results_payload.append(
            {
                "story_id": s_id,
                "scope": case["expected_scope"],
                "scope_confidence": 0.95,
                "scope_reason": f"Scope reason for {case['id']}",
                "confidence": 0.95,
                "reason": f"Triage reason for {case['id']}",
                "retention": case["expected_retention"],
                "enrichment": case.get("expected_enrichment", "NONE"),
                "exclusion_reason": case.get("exclusion_reason"),
                "brief_payload": brief_dict,
            }
        )

    # 2. Run Gate V2 Batch Triage
    mock_ai = AsyncMock()
    mock_ai.generate_text = AsyncMock(return_value=json.dumps({"results": triage_results_payload}))
    mock_ai.provider_name = "oracle_test_provider"
    mock_ai.model_name = "oracle_test_model"

    from src.config_loader import EditionScopeConfig

    sample_config.settings.edition_scopes["berdyansk"] = EditionScopeConfig(
        name="Бердянск",
        focus_places=("Бердянск", "Бердянский район", "Николаевка"),
        direct_impact_only=True,
    )

    triage_service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    _slug, scope_config = await resolve_edition_scope(conn, sample_config, edition.id)
    sc_hash = scope_config_hash(scope_config)

    batch_result = await triage_service.triage_stories_batch(
        conn,
        cluster_states,
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=sc_hash,
    )

    # Assert triage outcomes
    assert len(batch_result.results) == 9
    results_by_id = {r.story_id: r for r in batch_result.results}

    # Verify 7 positives
    for case in cases:
        s_id = story_ids_by_case[case["id"]]
        r = results_by_id[s_id]
        assert r.scope == "LOCAL"
        assert r.retention == "KEEP"
        assert r.enrichment == "BRIEF"

    # Verify 2 negatives
    bucha_id = story_ids_by_case["bucha_fire"]
    assert results_by_id[bucha_id].scope == "OUT_OF_SCOPE"
    assert results_by_id[bucha_id].retention == "DROP"

    commercial_id = story_ids_by_case["commercial_haircut"]
    assert results_by_id[commercial_id].scope == "LOCAL"
    assert results_by_id[commercial_id].retention == "DROP"
    assert results_by_id[commercial_id].exclusion_reason == "commercial_classified"

    # 3. Persist briefs for KEEP stories
    revisions_by_story_id: dict[int, int] = {}
    for case in cases:
        s_id = story_ids_by_case[case["id"]]
        r = results_by_id[s_id]
        st = [cs for cs in cluster_states if cs.story_id == s_id][0]
        rev = await brief_service.persist_brief(
            conn,
            story_id=s_id,
            assignment_id=st.latest_assignment_id,
            payload=r.brief_payload,
        )
        assert rev is not None
        revisions_by_story_id[s_id] = rev.id

    await conn.execute(
        "UPDATE story_revisions SET created_at = %s WHERE id = ANY(%s)",
        (_NOW, list(revisions_by_story_id.values())),
    )
    await conn.execute(
        "UPDATE story_edition_scope_decisions SET created_at = %s",
        (_NOW,),
    )
    await conn.execute(
        "UPDATE story_event_triage_decisions SET created_at = %s",
        (_NOW,),
    )

    # 4. Publication Run & Eligibility Check
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash=sc_hash, prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-oracle", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-oracle", prompt_version="v1"
    )

    run = await pub_repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-oracle-digest",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )

    eligible_revs = await pub_repo.eligible_story_revisions(
        conn,
        edition_id=edition.id,
        snapshot_at=_NOW,
        eligibility_policy_id=elig.id,
    )
    eligible_story_ids = {r["story_id"] for r in eligible_revs}

    # All 7 positive cases must be eligible
    for case in cases:
        assert story_ids_by_case[case["id"]] in eligible_story_ids

    # Neither negative case must be eligible
    assert bucha_id not in eligible_story_ids
    assert commercial_id not in eligible_story_ids

    # 5. Freeze selected inputs and adapt for digest publication
    selected_inputs = []
    for rank, case in enumerate(cases, start=1):
        s_id = story_ids_by_case[case["id"]]
        rev_id = revisions_by_story_id[s_id]
        cand = await pub_repo.insert_candidate(
            conn,
            run.id,
            story_id=s_id,
            story_revision_id=rev_id,
            deterministic_rank=rank,
        )
        dec = await pub_repo.insert_selection_decision(
            conn,
            run.id,
            PublicationSelectionDecision(
                id=0,
                publication_run_id=run.id,
                candidate_id=cand.id,
                decision="INCLUDE",
                presentation_intent="normal",
                confidence=0.95,
                reason="Golden oracle",
                rank=rank,
                metadata={},
                created_at=_NOW,
            ),
        )
        c_frag_ids = [frag_db_ids[f["fragment_id"]] for f in case["fragments"]]
        inp = await pub_repo.freeze_selected_input(
            conn,
            run.id,
            story_id=s_id,
            story_revision_id=rev_id,
            selection_decision_id=dec.id,
            presentation_intent="normal",
            rank=rank,
            fragment_ids=c_frag_ids,
        )
        selected_inputs.append(inp)

    # 6. Adapt inputs and verify CitySituationRollup & exact provenance
    editorial_input = await adapter.adapt_inputs_on(conn, run.id, inputs=selected_inputs)

    assert len(editorial_input.analysis.cards) == 7
    assert editorial_input.analysis.city_situation is not None

    items = editorial_input.analysis.city_situation.items
    assert len(items) > 0
    items_by_subject = {item.subject_key: item for item in items}

    # Power supply in Center / Nagornaya is UNAVAILABLE
    assert "power_supply" in items_by_subject
    p_item = items_by_subject["power_supply"]
    assert p_item.state == "UNAVAILABLE"
    assert "Центр" in p_item.location

    # Gas supply in Nikolaevka is RESTRICTED
    assert "gas_supply" in items_by_subject
    g_item = items_by_subject["gas_supply"]
    assert g_item.state == "RESTRICTED"
    assert g_item.location == "Николаевка"

    # ATM Cash in Delmar is AVAILABLE
    assert "banking_cash" in items_by_subject
    atm_item = items_by_subject["banking_cash"]
    assert atm_item.state == "AVAILABLE"

    # Water timeline (morning unavailable, afternoon available, evening unavailable)
    # Temporal resolution MUST resolve to the latest (evening UNAVAILABLE) state!
    assert "water_supply" in items_by_subject
    w_item = items_by_subject["water_supply"]
    assert w_item.state == "UNAVAILABLE"
    assert w_item.observation_count == 3

    # 7. Render final Telegram grouped digest
    renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
    title, lead, body = renderer.render_grouped_digest(editorial_input, edition_name="Бердянск")

    assert "Дайджест: Бердянск" in title
    assert "Городская обстановка:" in body
    assert "🔴 <b>Электроснабжение (Центр / Нагорная)</b>:" in body
    assert "🟡 <b>Газоснабжение (Николаевка)</b>:" in body
    assert "🟢 <b>Банкоматы и наличные (АКЗ)</b>:" in body
    assert "🔴 <b>Водоснабжение (Колония)</b>:" in body
    assert "Статистика: источников:" in body
    assert "событий: 7." in body
