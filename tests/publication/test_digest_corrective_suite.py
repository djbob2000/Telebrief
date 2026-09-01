"""Comprehensive corrective integration and parity regression suite for digest pipeline (Option B)."""

from __future__ import annotations

import datetime as dt
import json
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.benchmark_publication_floor_parity import run_benchmark
from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.editorial_models import StoryCard
from src.processing.fragments import split_into_fragments
from src.processing.hard_exclusion import evaluate_story_hard_exclusion
from src.processing.operational_semantics import (
    derive_operational_observations,
)
from src.publication.city_situation import CitySituationItem
from src.publication.digest_presentation import (
    _are_cards_merge_compatible,
    _canonical_city_situation_subject,
    _compute_merge_groups,
)
from src.publication.rubrics import (
    DigestRubricClassifier,
    DigestRubricConfig,
    DigestRubricsConfig,
)


def test_pure_commercial_classified_dropped_at_gate_level():
    """Pure commercial advertisements must be hard-excluded and dropped entirely."""
    fragments = (
        {
            "id": 1,
            "text": "Продам гараж в кооперативе Космос, 5000 руб, звонить по телефону +7 990 123 45 67",
        },
        {"id": 2, "text": "Цена договорная, обращаться с 9 до 18"},
    )
    audit = evaluate_story_hard_exclusion(fragments)
    assert audit.drop_story is True
    assert len(audit.excluded_fragment_ids) == 2
    assert len(audit.retained_fragment_ids) == 0
    assert audit.story_exclusion_reason == "commercial_classified"


def test_mixed_story_preserves_civic_content_and_excludes_commercial_payload():
    """Mixed stories containing both civic news and ads must NOT be dropped; commercial items marked EXCLUDE."""
    fragments = (
        {
            "id": 1,
            "text": "На Восточном проспекте отсутствует водоснабжение из-за аварии на трубопроводе",
        },
        {
            "id": 2,
            "text": "Магазин Инструменты на Ленина предлагает насосы и генераторы, скидки 10%, звоните +79901112233",
        },
    )
    audit = evaluate_story_hard_exclusion(fragments)
    assert audit.drop_story is False
    assert 2 in audit.excluded_fragment_ids
    assert 1 in audit.retained_fragment_ids
    assert audit.story_exclusion_reason is None


def test_dependent_continuation_preserves_actionable_civic_service_context():
    """Dependent continuation blocks (address, hours, phone) attach backward preserving practical context."""
    text = (
        "Бесплатная зарядка гаджетов открыта для всех жителей города.\n\n"
        "Адрес: ул. Гагарина, 1\n"
        "Режим работы: с 9:00 до 18:00\n"
        "Телефон: +7 990 000 11 22"
    )
    fragments = split_into_fragments(text)
    # Dependent blocks are packed with the parent paragraph
    assert len(fragments) == 1
    assert "ул. Гагарина, 1" in fragments[0].text_content
    assert "Режим работы" in fragments[0].text_content
    assert "Бесплатная зарядка" in fragments[0].text_content


def test_city_situation_admission_rejects_retail_sale_and_intercity_transport():
    """Retail commodities and intercity transport routes are excluded from City Situation dashboard."""
    # 1. Retail water sale at 3 rub/liter
    retail_item = EvidenceItemPayload(
        text="Вода на розлив по 3 ₽/литр в киоске на Восточном",
        kind="service_access",
        publication_use="PUBLISH",
        source_fragment_ids=(1,),
        service_state=ServiceStatePayload(
            subject_key="water",
            subject_label="Вода на розлив",
            dimension="availability",
            state="AVAILABLE",
            location="",
            entity="",
            expected_now=True,
            basis="normal_operation",
        ),
    )
    obs = derive_operational_observations(EventPayload(evidence_items=(retail_item,)))
    assert len(obs) == 0

    # 2. Intercity route
    now = dt.datetime.now(dt.timezone.utc)
    bus_item = CitySituationItem(
        subject_key="transport",
        subject_label="Автобус Бердянск — Ростов",
        dimension="availability",
        location="",
        entity="Автовокзал",
        state="AVAILABLE",
        detail="Ежедневные межгород рейсы в Ростов",
        source_refs=("r1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    assert _canonical_city_situation_subject(bus_item) is None

    # 3. Legitimate city electricity outage
    power_item = CitySituationItem(
        subject_key="power",
        subject_label="Электроснабжение",
        dimension="availability",
        location="Гора",
        entity="БЭС",
        state="UNAVAILABLE",
        detail="Аварийное отключение света на Горе",
        source_refs=("r2",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    assert _canonical_city_situation_subject(power_item) == "electricity"


@pytest.mark.asyncio
async def test_rubric_family_fallback_routes_borderline_telecom_to_communications():
    """Sub-threshold vector similarity routes to communications via family fallback."""

    class MockEmbeddingProvider:
        async def embed_many(self, texts, **kwargs):
            result = []
            for t in texts:
                if t.startswith("Инфраструктура\n"):
                    result.append([1.0, 0.0])
                elif t.startswith("Связь\n"):
                    result.append([0.95, 0.30])
                else:
                    # Unresolved card text
                    result.append([0.0, 1.0])
            return result

    rubrics_cfg = DigestRubricsConfig(
        min_similarity=0.38,
        items=(
            DigestRubricConfig(
                id="infrastructure",
                name="Инфраструктура",
                description="жкх свет вода",
                emoji="⚡️",
                fallback=False,
            ),
            DigestRubricConfig(
                id="communications",
                name="Связь",
                description="интернет связь провайдер",
                emoji="📱",
                fallback=False,
            ),
            DigestRubricConfig(
                id="other",
                name="Другое",
                description="прочее",
                emoji="📌",
                fallback=True,
            ),
        ),
    )
    classifier = DigestRubricClassifier(provider=MockEmbeddingProvider(), dimensions=2)
    card = StoryCard(
        id="story:854",
        topic="Связь и интернет",
        summary="Провайдер Миранда проводит работы на улицах Морозова и Гайдара, интернет отключен",
        tags=["интернет", "миранда", "связь"],
        importance="medium",
    )

    result_cards, assignments = await classifier.classify([card], rubrics=rubrics_cfg)
    assert len(assignments) == 1
    assert assignments[0].rubric_id == "communications"
    assert assignments[0].method == "family_fallback"
    assert result_cards[0].rubric_id == "communications"


def test_thematic_merge_rules_enforce_clique_6_and_reject_incompatible_workarounds():
    """Merge grouping accommodates up to 6 stories in cliques and isolates workarounds from status."""
    # 1. Homogeneous power reports coalesce up to 6
    power_cards = [
        StoryCard(
            id=f"story:p{i}",
            topic=f"Свет на районе {i}",
            importance="medium",
            summary=f"В районе {i} дали свет после аварии",
            tags=["свет"],
            rubric_id="utilities",
            category="utilities",
        )
        for i in range(1, 8)
    ]
    groups = _compute_merge_groups(power_cards)
    mg1 = groups["story:p1"]
    for i in range(2, 7):
        assert groups[f"story:p{i}"] == mg1
    assert groups["story:p7"] != mg1

    # 2. Incompatible presentation kinds (status vs workaround) cannot merge
    card_status = StoryCard(
        id="story:w1",
        topic="Водоснабжение",
        importance="medium",
        summary="На Самолёте вода есть со слабым напором",
        tags=["вода", "самолёт"],
        rubric_id="utilities",
        category="utilities",
    )
    card_workaround = StoryCard(
        id="story:w2",
        topic="Услуги сантехника",
        importance="low",
        summary="Установка накопительных баков и насосов для воды",
        tags=["накопительные баки", "сантехник"],
        rubric_id="utilities",
        category="utilities",
    )
    assert _are_cards_merge_compatible(card_status, card_workaround) is False


@pytest.mark.asyncio
async def test_digest_legacy_floor_parity_and_coverage_denominator(tmp_path: Path):
    """Run benchmark against frozen legacy floor fixture verifying 100% coverage and detail retention."""
    from scripts.publication_regression import LegacyCoverageCase

    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "berdyansk_2026_09_01_digest_legacy_floor.json"
    )
    assert fixture_path.exists(), f"Fixture missing at {fixture_path}"

    case = LegacyCoverageCase.load_json(fixture_path)
    # Build complete export payload covering all fixture units
    all_frag_ids = [
        s.fixture_fragment_id
        for u in case.coverage_units
        for s in u.acceptable_sources
        if s.fixture_fragment_id
    ]
    all_fingerprints = [
        s.source_fingerprint
        for u in case.coverage_units
        for s in u.acceptable_sources
        if s.source_fingerprint
    ]
    final_trace_units = []
    for u in case.coverage_units:
        text = f"{u.description} " + " ".join(u.required_microdetails)
        frag_ids = [s.fixture_fragment_id for s in u.acceptable_sources if s.fixture_fragment_id]
        fps = [s.source_fingerprint for s in u.acceptable_sources if s.source_fingerprint]
        final_trace_units.append(
            {
                "text": text,
                "fixture_fragment_ids": frag_ids,
                "source_fingerprints": fps,
                "source_refs": [],
            }
        )

    export_payload = {
        "source_fragment_ids": all_frag_ids,
        "source_fingerprints": all_fingerprints,
        "evidence_fragment_ids": all_frag_ids,
        "evidence_fingerprints": all_fingerprints,
        "candidate_fragment_ids": all_frag_ids,
        "candidate_fingerprints": all_fingerprints,
        "sealed_fragment_ids": all_frag_ids,
        "sealed_fingerprints": all_fingerprints,
        "plan_fragment_ids": all_frag_ids,
        "final_trace_fragment_ids": all_frag_ids,
        "final_trace_units": final_trace_units,
    }

    export_path = tmp_path / "export.json"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(export_payload, f)

    report_path = tmp_path / "parity_report.json"
    args = Namespace(
        case=str(fixture_path),
        export=str(export_path),
        run_id=None,
        database_url=None,
        output=str(report_path),
    )
    exit_code = await run_benchmark(args)
    assert exit_code == 0
    assert report_path.exists()

    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    assert report["legacy_floor_coverage"] == 1.0
    assert report["legacy_microdetail_retention"] == 1.0
