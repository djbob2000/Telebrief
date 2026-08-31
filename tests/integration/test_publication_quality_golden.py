"""Integration test oracle for Berdyansk Publication Quality golden regression suite."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

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
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "berdyansk_publication_quality_golden.json"
    )
    with fixture_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.integration
def test_publication_quality_golden_contract(golden_fixture: dict[str, Any]) -> None:
    cases = golden_fixture.get("cases", [])
    negative_cases = golden_fixture.get("negative_cases", [])

    ids = {case["id"] for case in cases}
    assert ids == {
        "power_blackout",
        "water_koloniya_off_on_off",
        "water_azmol_partial",
        "atm_cash",
        "connectivity_backup_power",
        "passport_fee",
        "local_uav_pvo",
        "future_gas_outage",
    }

    neg_ids = {case["id"] for case in negative_cases}
    assert neg_ids == {"bucha_fire", "haircut_discount", "generator_product_ad"}


@pytest.mark.integration
def test_publication_quality_expected_coverage(golden_fixture: dict[str, Any]) -> None:
    cases = golden_fixture.get("cases", [])
    subject_keys = {case.get("expected_subject_key") for case in cases}
    assert "power_supply" in subject_keys
    assert "water_supply" in subject_keys
    assert "telecom_internet" in subject_keys
    assert "financial_services" in subject_keys
    assert "air_defense_security" in subject_keys


@pytest.mark.postgres
async def test_berdyansk_publication_quality_golden_oracle_pipeline(
    conn, pool, edition, sample_config, golden_fixture
):
    """End-to-end regression oracle testing publication quality across digest and article."""
    from src.db.uow import DatabaseUnitOfWork

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
    frag_db_ids: dict[int, int] = {}
    cluster_states: list[StoryClusterState] = []
    triage_results_payload = []
    source_id_map: dict[int, int] = {}

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
            f_time = dt.datetime.fromisoformat(frag["published_at"])
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
                INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
                VALUES (%s, 'message', %s, %s, %s)
                RETURNING id
                """,
                (src_id, f"msg-{frag['source_item_id']}", f_time, f_time),
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
                    "effective_from": exp_obs.get("effective_from"),
                    "effective_until": exp_obs.get("effective_until"),
                }
            )

        evidence_items_payload = []
        for exp_evi in case.get("expected_evidence_items", []):
            mapped_fids = [
                frag_db_ids[fid]
                for fid in exp_evi.get("source_fragment_ids", [])
                if fid in frag_db_ids
            ]
            evidence_items_payload.append(
                {
                    "text": exp_evi["text"],
                    "kind": exp_evi["kind"],
                    "publication_use": exp_evi["publication_use"],
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
                "evidence_items": evidence_items_payload,
                "confidence_score": 0.95,
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

    # 2. Gate V2 Batch Triage
    from unittest.mock import AsyncMock

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
    assert len(batch_result.results) == 11
    results_by_id = {r.story_id: r for r in batch_result.results}

    # 3. Persist briefs
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

    # 4. Publication Run & Eligibility Check
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-oracle", prompt_version="v1"
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
        request_key="test-quality-digest",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )

    # 5. Adapt selected inputs
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
                reason="Golden quality oracle",
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

    editorial_input = await adapter.adapt_inputs_on(conn, run.id, inputs=selected_inputs)

    # Digest rendering & Presentation Plan
    from src.publication.digest_presentation import build_digest_presentation_plan

    evidence_dict = getattr(editorial_input.analysis, "evidence", {}) or {}
    plan = build_digest_presentation_plan(
        cards=editorial_input.analysis.cards,
        city_situation=editorial_input.analysis.city_situation,
        evidence=evidence_dict,
        max_city_situation_items=7,
        max_city_situation_details=2,
    )

    # Presentation plan verifies City Situation grouping and detail separation
    assert len(plan.city_situation.groups) <= 7
    all_covered_and_detail_ids = set(plan.detail_story_ids) | set(
        plan.city_situation.covered_source_refs
    )
    assert len(all_covered_and_detail_ids) > 0

    renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
    title, lead, body = renderer.render_grouped_digest(editorial_input, edition_name="Бердянск")
    digest_text = f"{title}\n{lead}\n{body}"

    # Semantic assertions
    assert digest_text.count("Колони") >= 1
    assert "Буч" not in digest_text
    assert "скидк" not in digest_text.lower()
