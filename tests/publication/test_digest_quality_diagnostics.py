"""Unit tests for non-blocking prose quality diagnostics."""

from __future__ import annotations

import datetime as dt

from src.publication.digest_narrative import (
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
)
from src.publication.digest_quality_diagnostics import (
    DIGEST_DIAGNOSTICS_VERSION,
    audit_digest_prose_quality,
)
from src.publication.evidence import PublicationEvidence


def _make_evidence(
    eid: str,
    sid: int,
    text: str,
    kind: str = "established_fact",
    pub_use: str = "PUBLISH",
) -> PublicationEvidence:
    now = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)
    return PublicationEvidence(
        evidence_id=eid,
        story_id=sid,
        text=text,
        source_text=text,
        kind=kind,
        publication_use=pub_use,
        fragment_id=1,
        source_ref="ref:1",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )


def test_clean_draft_returns_no_warnings():
    evi = _make_evidence("evi:1", 1, "Автобус №4 возобновил движение")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Возобновлено движение автобуса №4",
        body="По сообщениям очевидцев, транспорт ходит с интервалом в 30 минут по обычному маршруту.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:trans",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    assert audit.version == DIGEST_DIAGNOSTICS_VERSION
    assert audit.is_clean is True
    assert len(audit.warnings) == 0


def test_detects_duplicated_attribution():
    evi = _make_evidence("evi:1", 1, "На Горе нет света")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Жители сообщают об отключении света на Горе",
        body="По сообщениям жителей, электричество пропало около полудня во всем микрорайоне.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:util",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    assert audit.is_clean is False
    codes = [w.code for w in audit.warnings]
    assert "DUPLICATED_ATTRIBUTION" in codes
    warning = next(w for w in audit.warnings if w.code == "DUPLICATED_ATTRIBUTION")
    assert warning.block_id == "block:util"
    assert warning.item_index == 0


def test_detects_question_as_meta_news():
    evi = _make_evidence(
        "evi:1",
        1,
        "Работает ли пенсионный фонд?",
        kind="resident_question",
        pub_use="CONTEXT",
    )

    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Жители интересуются работой пенсионного фонда",
        body="В соцсетях появились вопросы о графике приема граждан в понедельник.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:soc",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    codes = [w.code for w in audit.warnings]
    assert "QUESTION_AS_META_NEWS" in codes


def test_detects_redundant_headline_in_body():
    evi = _make_evidence("evi:1", 1, "Спорткомплекс открыл набор")
    evidence = {"evi:1": evi}

    item = DigestEditorialItemDraft(
        headline="Спорткомплекс открыл бесплатный набор детей",
        body="Спорткомплекс открыл бесплатный набор детей. Занятия начнутся со следующей недели.",
        covered_story_ids=("story:1",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:soc",
                items=(item,),
            ),
        )
    )

    audit = audit_digest_prose_quality(draft, evidence)
    codes = [w.code for w in audit.warnings]
    assert "REDUNDANT_HEADLINE_IN_BODY" in codes


def test_audit_digest_prose_quality_compression_metrics():
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )

    evi = _make_evidence("evi:1", 1, "Отключение света")
    evidence = {"evi:1": evi}

    item_multi = DigestEditorialItemDraft(
        headline="Восстановление сетей на двух улицах",
        body="Свет вернули на Гагарина и Пушкина.",
        covered_story_ids=("story:1", "story:2"),
        cited_support_ids=("evi:1",),
    )
    item_single = DigestEditorialItemDraft(
        headline="Замена водовода",
        body="Работы завершены.",
        covered_story_ids=("story:3",),
        cited_support_ids=("evi:1",),
    )
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:util:0",
                items=(item_multi, item_single),
            ),
        )
    )

    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(
                CitySituationPresentationGroup(
                    group_id="sit:power",
                    group_kind="subject_status",
                    subject_key="power_supply",
                    subject_label="Электроснабжение",
                    state="UNAVAILABLE",
                    detail_lines=("Света нет в центре",),
                    source_refs=("evi:1",),
                    covered_story_ids=("story:4", "story:5"),
                    cited_support_ids=("evi:1",),
                ),
            ),
            covered_source_refs=(),
        ),
        story_presentations=(
            DigestStoryPresentation(story_id="story:1", mode="DETAIL_ONLY"),
            DigestStoryPresentation(story_id="story:2", mode="DETAIL_ONLY"),
            DigestStoryPresentation(story_id="story:3", mode="DETAIL_ONLY"),
            DigestStoryPresentation(story_id="story:4", mode="DASHBOARD_ONLY"),
            DigestStoryPresentation(story_id="story:5", mode="DASHBOARD_ONLY"),
        ),
    )

    audit = audit_digest_prose_quality(draft, evidence, presentation_plan=plan)

    assert audit.detail_item_count == 2
    assert audit.multi_story_item_count == 1
    assert audit.single_story_item_count == 1
    assert audit.dashboard_group_count == 1
    assert audit.compression_ratio == round(5 / 3, 2)
    assert audit.items_per_group == 2.0
    meta = audit.as_metadata()
    assert meta["multi_story_item_count"] == 1
    assert meta["compression_ratio"] == round(5 / 3, 2)


def test_crisis_day_digest_compression_and_readability_regression():
    import json
    from pathlib import Path

    from src.editorial_models import (
        EditorialAnalysis,
        PreparedBundle,
        StoryCard,
    )
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_coverage import build_digest_coverage_trace
    from src.publication.digest_narrative import (
        build_deterministic_digest_draft,
        plan_digest_narrative_blocks,
    )
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.editorial_adapter import FrozenEditorialInput
    from src.publication.renderers import (
        PublicationDigestRenderer,
        split_into_telegram_chunks,
    )

    fixture_path = Path(__file__).parent.parent / "fixtures" / "live_digest_compression_cases.json"
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)["crisis_day"]

    now = dt.datetime.fromisoformat(data["snapshot_at"])
    cards: list[StoryCard] = []
    sit_items: list[CitySituationItem] = []

    for c_dict in data["cards"]:
        rep_refs: list[str] = []
        for o in c_dict.get("operational_observations", []):
            rep_refs.extend(o["source_refs"])
            sit_items.append(
                CitySituationItem(
                    subject_key=o["subject_key"],
                    subject_label=o["subject_label"],
                    dimension=o["dimension"],
                    location="",
                    entity="",
                    state=o["state"],
                    detail=o["detail"],
                    source_refs=tuple(o["source_refs"]),
                    first_observed_at=now,
                    last_observed_at=now,
                    observation_count=1,
                    current_source_refs=tuple(o["source_refs"]),
                )
            )

        cards.append(
            StoryCard(
                id=c_dict["id"],
                topic=c_dict["topic"],
                summary=c_dict["summary"],
                importance=c_dict["importance"],
                rubric_id=c_dict["rubric_id"],
                tags=c_dict.get("tags", []),
                representative_source_refs=rep_refs,
            )
        )

    evidence_dict: dict[str, PublicationEvidence] = {}
    for eid, e_dict in data["evidence"].items():
        evidence_dict[eid] = PublicationEvidence(
            evidence_id=eid,
            story_id=e_dict["story_id"],
            text=e_dict["text"],
            source_text=e_dict["text"],
            kind=e_dict["kind"],
            publication_use=e_dict["publication_use"],
            fragment_id=1,
            source_ref=eid,
            source_id=1,
            source_item_id=1,
            source_role="official" if e_dict["kind"] == "established_fact" else "citizen",
            observed_at=now,
        )

    renderer = PublicationDigestRenderer(use_emojis=True)
    city_rollup = CitySituationRollup(items=tuple(sit_items))

    presentation_plan = build_digest_presentation_plan(
        cards=cards,
        city_situation=city_rollup,
        evidence=evidence_dict,
        max_city_situation_items=6,
        max_city_situation_details=2,
        max_city_situation_positive_items=2,
    )

    # 1. City Situation dashboard is bounded to at most 6 groups
    assert len(presentation_plan.city_situation.groups) <= 6

    # 2. Build deterministic digest draft
    deterministic_draft = build_deterministic_digest_draft(
        cards=cards,
        evidence=evidence_dict,
        rubrics=renderer.rubrics,
        presentation_plan=presentation_plan,
    )

    # 3. Quality audit shows multi-story grouping compression
    audit = audit_digest_prose_quality(
        deterministic_draft,
        evidence=evidence_dict,
        presentation_plan=presentation_plan,
    )
    assert audit.dashboard_group_count <= 6
    assert audit.multi_story_item_count >= 1
    assert audit.compression_ratio > 1.0

    # 4. Coverage trace is 100%
    detail_cards = [c for c in cards if c.id in presentation_plan.detail_story_ids]
    narrative_plan = plan_digest_narrative_blocks(
        cards=detail_cards,
        evidence=evidence_dict,
        rubrics=renderer.rubrics,
        presentation_plan=presentation_plan,
    )
    trace = build_digest_coverage_trace(
        presentation_plan,
        deterministic_draft,
        narrative_plan,
    )
    assert trace.story_coverage == 1.0

    # 5. Rendering fits within at most 2 Telegram chunks
    frozen = FrozenEditorialInput(
        analysis=EditorialAnalysis(cards=cards, city_situation=city_rollup),
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=len(cards), candidate_count=len(cards)
        ),
    )
    title, lead, body = renderer.render_grouped_digest(
        frozen,
        edition_name=data["edition"],
        snapshot_at=now,
        narrative_draft=deterministic_draft,
        presentation_plan=presentation_plan,
    )
    chunks = split_into_telegram_chunks(body)
    assert len(chunks) <= 2
