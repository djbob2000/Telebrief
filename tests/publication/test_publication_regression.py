"""Pure tests for offline legacy regression and loss attribution engine."""

from __future__ import annotations

import pytest

from scripts.publication_regression import (
    AllowedLegacyLossReason,
    CaseStageState,
    LegacyCoverageCase,
    LegacyCoverageUnit,
    LegacyLoss,
    LegacySourceIdentity,
    attribute_loss,
    evaluate_case,
    source_fingerprint,
)


def test_microdetails_measured_from_canonical_trace_text():
    unit = LegacyCoverageUnit(
        id="free_charging_gagarina_1",
        description="Free charging",
        acceptable_sources=(
            LegacySourceIdentity(
                fixture_fragment_id="frag:charge1",
                source_fingerprint="fp123",
            ),
        ),
        required_microdetails=("Гагарина, 1",),
    )
    case = LegacyCoverageCase(
        id="test_case",
        legacy_commit="commit1",
        window_start="2026-08-31T00:00:00Z",
        window_end="2026-09-01T00:00:00Z",
        coverage_units=(unit,),
    )
    exported_case = {
        "source_fragment_ids": ["frag:charge1"],
        "source_fingerprints": ["fp123"],
        "evidence_fragment_ids": ["frag:charge1"],
        "evidence_fingerprints": ["fp123"],
        "candidate_fragment_ids": ["frag:charge1"],
        "sealed_fragment_ids": ["frag:charge1"],
        "plan_fragment_ids": ["frag:charge1"],
        "final_trace_units": [
            {
                "text": "В городе работает пункт зарядки.",
                "fixture_fragment_ids": ["frag:charge1"],
                "source_fingerprints": ["fp123"],
                "source_refs": [],
            }
        ],
    }

    report = evaluate_case(case, exported_case)
    result = report.units[0]
    assert result.loss == LegacyLoss.COVERED
    assert result.retained_microdetails == ()
    assert result.missing_microdetails == ("Гагарина, 1",)
    assert report.legacy_microdetail_retention == 0.0
    assert report.regression_unit_ids == ("free_charging_gagarina_1",)


def test_loss_attribution_stops_at_first_missing_stage():
    unit = LegacyCoverageUnit(id="free_charging_gagarina_1", description="Free charging")

    assert attribute_loss(unit, CaseStageState(source=False)) == LegacyLoss.SOURCE_CORPUS_LOSS
    assert (
        attribute_loss(unit, CaseStageState(source=True, evidence=False))
        == LegacyLoss.KNOWLEDGE_LAYER_LOSS
    )
    assert (
        attribute_loss(unit, CaseStageState(source=True, evidence=True, candidate=False))
        == LegacyLoss.ELIGIBILITY_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(source=True, evidence=True, candidate=True, sealed=False),
        )
        == LegacyLoss.SELECTION_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(
                source=True,
                evidence=True,
                candidate=True,
                sealed=True,
                plan=False,
            ),
        )
        == LegacyLoss.COVERAGE_PLAN_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(
                source=True,
                evidence=True,
                candidate=True,
                sealed=True,
                plan=True,
                final_trace=False,
            ),
        )
        == LegacyLoss.PUBLICATION_COVERAGE_LOSS
    )
    assert attribute_loss(unit, CaseStageState.all_present()) == LegacyLoss.COVERED


def test_allowed_loss_reason_enum_validation():
    # Valid reasons
    for reason in (
        "HARD_EXCLUSION",
        "TRUE_DUPLICATE",
        "UNSUPPORTED_LEGACY_INFERENCE",
    ):
        unit = LegacyCoverageUnit.from_dict(
            {"id": "u1", "description": "d", "allowed_loss_reason": reason}
        )
        assert unit.allowed_loss_reason == AllowedLegacyLossReason(reason)

    # Invalid reasons rejected
    for invalid in ("WRITER_OMITTED", "LOW_PRIORITY", "TOO_MINOR", "UNKNOWN"):
        with pytest.raises(ValueError, match="Invalid allowed_loss_reason"):
            LegacyCoverageUnit.from_dict(
                {"id": "u1", "description": "d", "allowed_loss_reason": invalid}
            )


def test_source_fingerprint_deterministic():
    text1 = "На ул. Гагарина, 1 открыли пункт подзарядки"
    text2 = "  на ул.  Гагарина, 1   открыли пункт подзарядки  \n"
    assert source_fingerprint(text1) == source_fingerprint(text2)


def test_trace_matching_and_offline_gate_metrics():
    fp1 = source_fingerprint("Пункт подзарядки на Гагарина 1")
    fp2 = source_fingerprint("Движение автобуса 4 восстановлено")
    fp3 = source_fingerprint("Реклама такси круглосуточно")

    case = LegacyCoverageCase(
        id="berdyansk_case_1",
        legacy_commit="abc1234",
        window_start="2026-08-30T00:00:00Z",
        window_end="2026-08-31T00:00:00Z",
        coverage_units=(
            LegacyCoverageUnit(
                id="unit_charging",
                description="Charging station",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_1",
                        source_ref="ref_1",
                        source_fingerprint=fp1,
                    ),
                ),
                required_microdetails=("ул. Гагарина, 1", "бесплатно"),
            ),
            LegacyCoverageUnit(
                id="missing_binding_unit",
                description="Bus line 4",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_2",
                        source_ref="ref_2",
                        source_fingerprint=fp2,
                    ),
                ),
                required_microdetails=("маршрут №4",),
            ),
            LegacyCoverageUnit(
                id="excluded_ad_unit",
                description="Taxi ad",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_3",
                        source_ref="ref_3",
                        source_fingerprint=fp3,
                    ),
                ),
                allowed_loss_reason=AllowedLegacyLossReason.HARD_EXCLUSION,
            ),
        ),
    )

    # 1. First scenario: missing_binding_unit drops at PUBLICATION_COVERAGE_LOSS
    exported_case_partial = {
        "source_fingerprints": [fp1, fp2, fp3],
        "source_refs": ["ref_1", "ref_2", "ref_3"],
        "source_fragment_ids": ["frag_1", "frag_2", "frag_3"],
        "evidence_fingerprints": [fp1, fp2],
        "evidence_refs": ["ref_1", "ref_2"],
        "evidence_fragment_ids": ["frag_1", "frag_2"],
        "candidate_refs": ["ref_1", "ref_2"],
        "candidate_fragment_ids": ["frag_1", "frag_2"],
        "sealed_refs": ["ref_1", "ref_2"],
        "sealed_fragment_ids": ["frag_1", "frag_2"],
        "plan_refs": ["ref_1", "ref_2"],
        "plan_fragment_ids": ["frag_1", "frag_2"],
        # only unit_charging in final trace
        "final_trace_supports": ["ref_1"],
        "final_trace_refs": ["ref_1"],
        "final_trace_fragment_ids": ["frag_1"],
        "retained_microdetails_by_unit": {
            "unit_charging": ["ул. Гагарина, 1", "бесплатно"],
        },
    }

    report_partial = evaluate_case(case, exported_case_partial)
    assert report_partial.legacy_floor_coverage == 0.5
    assert report_partial.legacy_microdetail_retention < 1.0
    assert report_partial.regression_unit_ids == ("missing_binding_unit",)

    # 2. Second scenario: both binding units covered with all microdetails
    exported_case_full = {
        **exported_case_partial,
        "final_trace_supports": ["ref_1", "ref_2"],
        "final_trace_refs": ["ref_1", "ref_2"],
        "final_trace_fragment_ids": ["frag_1", "frag_2"],
        "retained_microdetails_by_unit": {
            "unit_charging": ["ул. Гагарина, 1", "бесплатно"],
            "missing_binding_unit": ["маршрут №4"],
        },
    }

    report_full = evaluate_case(case, exported_case_full)
    assert report_full.legacy_floor_coverage == 1.0
    assert report_full.legacy_microdetail_retention == 1.0
    assert report_full.regression_unit_ids == ()


def test_build_export_payload_pure():
    from scripts.export_publication_regression_case import build_export_payload

    run = {
        "id": 123,
        "edition": "berdyansk",
        "snapshot_at": "2026-08-31T20:00:00+00:00",
    }
    source_corpus = [
        {
            "fixture_fragment_id": "frag-1",
            "source_fingerprint": "a" * 64,
        }
    ]
    publish_evidence = [{"evidence_id": "story:1:evidence:0:frag:1"}]
    candidates = [{"story_id": "story:1"}]
    sealed_story_ids = ["story:1"]
    article_plan_story_ids = ["story:1"]
    digest_plan_story_ids = ["story:1"]
    article_claim_trace = [{"unit_id": "P001", "support_ids": ["story:1:evidence:0:frag:1"]}]
    digest_coverage_trace = [{"story_id": "story:1", "mode": "DETAIL_ONLY"}]

    payload = build_export_payload(
        run=run,
        source_corpus=source_corpus,
        publish_evidence=publish_evidence,
        candidates=candidates,
        sealed_story_ids=sealed_story_ids,
        article_plan_story_ids=article_plan_story_ids,
        digest_plan_story_ids=digest_plan_story_ids,
        article_claim_trace=article_claim_trace,
        digest_coverage_trace=digest_coverage_trace,
    )
    assert payload["run"] == run
    assert payload["source_corpus"] == source_corpus
    assert payload["publish_evidence"] == publish_evidence
    assert payload["candidates"] == candidates
    assert payload["sealed_story_ids"] == sealed_story_ids
    assert payload["article_plan_story_ids"] == article_plan_story_ids
    assert payload["digest_plan_story_ids"] == digest_plan_story_ids
    assert payload["article_claim_trace"] == article_claim_trace
    assert payload["digest_coverage_trace"] == digest_coverage_trace


@pytest.mark.postgres
async def test_export_publication_case_postgres(conn, edition):
    import datetime as dt
    import json

    from scripts.export_publication_regression_case import export_publication_case
    from src.publication.repository import PublicationPolicyRepository

    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-test", prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-test", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-test", prompt_version="v1"
    )

    now = dt.datetime.now(dt.timezone.utc)
    # Seed publication run
    cur = await conn.execute(
        """
        INSERT INTO publication_runs (
            edition_id, publication_type, status, request_key, snapshot_at,
            eligibility_policy_id, selection_policy_id, writer_policy_id, created_at
        )
        VALUES (%s, 'article', 'succeeded', 'req-exp-test', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (edition.id, now, elig.id, sel.id, wri.id, now),
    )
    run_id = (await cur.fetchone())[0]

    # Seed story with revision
    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition.id, now),
    )
    story_id = (await cur.fetchone())[0]

    ep = {
        "evidence_items": [
            {
                "evidence_id": f"story:{story_id}:evidence:0:frag:1",
                "source_fragment_ids": [1],
                "text": "Электричество восстановлено в Центре.",
                "publication_use": "PUBLISH",
            }
        ]
    }
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, event_payload, created_at)
        VALUES (%s, 1, 'open', 'Электричество в Центре', 'h-exp-1', %s, %s)
        RETURNING id
        """,
        (story_id, json.dumps(ep), now),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )

    # Seed candidate and sealed input
    cur = await conn.execute(
        """
        INSERT INTO publication_candidates (
            publication_run_id, story_id, story_revision_id, deterministic_rank, snapshot_features
        )
        VALUES (%s, %s, %s, 1, '{}')
        RETURNING id
        """,
        (run_id, story_id, rev_id),
    )
    cand_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO publication_selection_decisions (
            publication_run_id, candidate_id, decision, presentation_intent, confidence, reason, rank, metadata
        )
        VALUES (%s, %s, 'INCLUDE', 'normal', 1.0, 'test', 1, '{}')
        RETURNING id
        """,
        (run_id, cand_id),
    )
    dec_id = (await cur.fetchone())[0]

    await conn.execute(
        """
        INSERT INTO publication_inputs (
            publication_run_id, story_id, story_revision_id, selection_decision_id, presentation_intent, rank
        )
        VALUES (%s, %s, %s, %s, 'normal', 1)
        """,
        (run_id, story_id, rev_id, dec_id),
    )

    # Seed generation attempt
    cur = await conn.execute(
        """
        INSERT INTO publication_generation_attempts (
            publication_run_id, attempt_no, kind, provider, model, prompt_hash, metadata
        )
        VALUES (%s, 1, 'writer', 'mock', 'mock', 'h-mock', '{}')
        RETURNING id
        """,
        (run_id,),
    )
    att_id = (await cur.fetchone())[0]

    # Seed publication with trace metadata
    meta = {
        "article_claim_trace": {
            "units": [{"unit_id": "P001", "support_ids": [f"story:{story_id}:evidence:0:frag:1"]}],
            "story_coverage": 1.0,
        }
    }
    await conn.execute(
        """
        INSERT INTO publications (publication_run_id, winning_generation_attempt_id, publication_type, title, lead, body, metadata, created_at)
        VALUES (%s, %s, 'article', 'Заголовок', 'Лид', 'Текст статьи', %s, %s)
        """,
        (run_id, att_id, json.dumps(meta), now),
    )

    exported = await export_publication_case(conn, run_id)
    assert exported["run"]["id"] == run_id
    assert exported["run"]["edition_id"] == edition.id
    assert len(exported["candidates"]) == 1
    assert exported["sealed_story_ids"] == [f"story:{story_id}"]
    assert len(exported["publish_evidence"]) == 1
    assert len(exported["article_claim_trace"]) == 1
    assert exported["article_claim_trace"][0]["unit_id"] == "P001"


@pytest.mark.postgres
async def test_export_publication_case_source_corpus_independent_of_stories(conn, edition):
    import datetime as dt

    from scripts.export_publication_regression_case import export_publication_case
    from src.publication.repository import PublicationPolicyRepository

    now = dt.datetime.now(dt.timezone.utc)
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-source-test",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-source-test", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-source-test", prompt_version="v1"
    )

    # Seed source bound to edition
    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-100999', 'https://t.me/charge', 'Charge') RETURNING id"
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )

    # Seed source item, revision, fragment inside window
    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'msg-charge-1', %s) RETURNING id",
        (src_id, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-ch-1', 'На Гагарина, 1 бесплатно заряжают телефоны') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'На Гагарина, 1 бесплатно заряжают телефоны', 'h-f-ch-1', 'v1', TRUE, %s)",
        (sir_id, now),
    )

    # Seed publication run with no candidates/stories
    cur = await conn.execute(
        """
        INSERT INTO publication_runs (
            edition_id, publication_type, status, request_key, snapshot_at,
            eligibility_policy_id, selection_policy_id, writer_policy_id, created_at
        )
        VALUES (%s, 'digest', 'succeeded', 'req-source-only-test', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (edition.id, now, elig.id, sel.id, wri.id, now),
    )
    run_id = (await cur.fetchone())[0]

    exported = await export_publication_case(conn, run_id)
    assert len(exported["source_corpus"]) == 1
    assert exported["source_corpus"][0]["text"] == "На Гагарина, 1 бесплатно заряжают телефоны"
    assert exported["evidence_fragment_ids"] == []
    assert exported["candidate_fragment_ids"] == []


@pytest.mark.postgres
async def test_export_publication_case_distinct_evidence_and_candidate_stages(conn, edition):
    import datetime as dt
    import json

    from scripts.export_publication_regression_case import export_publication_case
    from src.publication.repository import PublicationPolicyRepository

    now = dt.datetime.now(dt.timezone.utc)
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-distinct-test",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-distinct-test", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-distinct-test", prompt_version="v1"
    )

    # Seed source and fragment
    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-100888', 'https://t.me/gen', 'Gen') RETURNING id"
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )
    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'msg-gen-1', %s) RETURNING id",
        (src_id, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-g-1', 'Поставки генераторов в школы') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Поставки генераторов в школы', 'h-f-g-1', 'v1', TRUE, %s) RETURNING id",
        (sir_id, now),
    )
    frag_id = (await cur.fetchone())[0]

    # Seed story with PUBLISH evidence
    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition.id, now),
    )
    story_id = (await cur.fetchone())[0]
    ep = {
        "evidence_items": [
            {
                "evidence_id": f"story:{story_id}:evidence:0:frag:{frag_id}",
                "source_fragment_ids": [frag_id],
                "text": "Поставки генераторов в школы",
                "publication_use": "PUBLISH",
            }
        ]
    }
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, event_payload, created_at)
        VALUES (%s, 1, 'open', 'Генераторы', 'h-sr-g-1', %s, %s) RETURNING id
        """,
        (story_id, json.dumps(ep), now),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )

    # Seed publication run without candidates
    cur = await conn.execute(
        """
        INSERT INTO publication_runs (
            edition_id, publication_type, status, request_key, snapshot_at,
            eligibility_policy_id, selection_policy_id, writer_policy_id, created_at
        )
        VALUES (%s, 'digest', 'succeeded', 'req-distinct-test', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (edition.id, now, elig.id, sel.id, wri.id, now),
    )
    run_id = (await cur.fetchone())[0]

    exported = await export_publication_case(conn, run_id)
    assert len(exported["source_corpus"]) == 1
    assert len(exported["evidence_fragment_ids"]) == 1
    assert exported["candidate_fragment_ids"] == []


def test_evaluate_case_rejects_unbound_coverage_units():
    unit = LegacyCoverageUnit(
        id="unbound_unit",
        description="Unbound unit without sources",
        acceptable_sources=(),
        required_microdetails=("детали",),
    )
    case = LegacyCoverageCase(
        id="test_unbound",
        legacy_commit="commit1",
        edition_slug="berdyansk",
        publication_type="digest",
        snapshot_at="2026-09-01T09:00:00+03:00",
        lookback_hours=24,
        window_start="2026-08-31T09:00:00+03:00",
        window_end="2026-09-01T09:00:00+03:00",
        coverage_units=(unit,),
    )
    with pytest.raises(ValueError, match="unbound legacy coverage unit 'unbound_unit'"):
        evaluate_case(case, {})


def test_berdyansk_2026_09_01_digest_legacy_floor_fixture():
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "berdyansk_2026_09_01_digest_legacy_floor.json"
    )
    assert fixture_path.exists(), f"Missing fixture at {fixture_path}"

    case = LegacyCoverageCase.load_json(fixture_path)
    assert case.id == "berdyansk_2026_09_01_digest_legacy_floor"
    assert case.publication_type == "digest"
    assert len(case.coverage_units) == 8
    assert all(unit.acceptable_sources for unit in case.coverage_units)


def test_berdyansk_2026_08_31_legacy_floor_fixture():
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "berdyansk_2026_08_31_legacy_floor.json"
    )
    assert fixture_path.exists(), f"Missing fixture at {fixture_path}"

    case = LegacyCoverageCase.load_json(fixture_path)
    assert case.id == "berdyansk_2026_08_31_legacy_floor"
    assert case.publication_type == "article"
    assert len(case.coverage_units) >= 12
    assert all(unit.acceptable_sources or unit.allowed_loss_reason for unit in case.coverage_units)

    required_units = {
        "water_azmol_and_upper_floors",
        "water_pipe_ordzhonikidze",
        "stoves_firewood_and_heating_prep",
        "free_charging_gagarina_1",
        "combined_energy_heating_bill",
        "disputed_utility_debts",
        "mfc_outdoor_consultations",
        "rosreestr_service_pause",
        "fluorography_generator_room_12",
        "school_20_generator_fuel_discussion",
        "dead_traffic_lights",
        "mobile_operator_reliability_difference",
    }
    unit_ids = {u.id for u in case.coverage_units}
    assert required_units.issubset(unit_ids)

    # Validate free_charging_gagarina_1 specifics
    charging_unit = next(u for u in case.coverage_units if u.id == "free_charging_gagarina_1")
    assert "Гагарина, 1" in charging_unit.required_microdetails
    assert "Gagarina 1" in charging_unit.description

    # Test evaluation without sources -> all report SOURCE_CORPUS_LOSS (no UNEXPLAINED publication loss)
    empty_export = {}
    report = evaluate_case(case, empty_export)
    for u in report.units:
        assert u.loss.value in ("SOURCE_CORPUS_LOSS", "COVERED")


def test_compare_article_approaches_no_fake_legacy_generation():
    import scripts.compare_article_approaches as comp

    # Ensure fake legacy generation function is removed
    assert not hasattr(comp, "generate_old_custom_article")
    assert not hasattr(comp, "ArticleGenerator")
    assert hasattr(comp, "generate_event_first_article")
    assert hasattr(comp, "run_comparison")
