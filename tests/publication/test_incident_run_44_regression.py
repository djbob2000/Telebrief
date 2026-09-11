"""Acceptance regression test reproducing the Incident Run 44 dataset.

Verifies:
1. "Сервис снова доступен" is rejected due to lack of a concrete named service/entity.
2. "В районе Водоканала..." is rejected due to lack of an action/state predicate.
3. Power outage and restoration consolidate into a single City Situation 🟡 group.
4. Operational stories in City Situation are not duplicated into thematic rubrics.
5. Statistics footer is preserved.
"""

import datetime as dt

from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.editorial_models import EditorialAnalysis, PreparedBundle, StoryCard, StoryElement
from src.publication.city_situation import CitySituationItem, CitySituationRollup
from src.publication.digest_presentation import (
    build_digest_presentation_plan,
)
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.evidence import PublicationEvidence
from src.publication.renderers import PublicationDigestRenderer
from src.publication.story_quality import validate_story_publication_eligibility

_NOW = dt.datetime(2026, 9, 11, 16, 0, tzinfo=dt.timezone.utc)


def test_incident_run_44_acceptance_regression():
    # 1. Define the 4 raw incident story payloads
    # Story 1: Power outage in Nagornaya/Slobodka + 170V in center
    payload_1 = EventPayload(
        headline="Электричество пропало в нескольких районах",
        digest_summary="По сообщениям жителей, свет отключили в нагорной части города и на Слободке. В центре города зафиксировано низкое напряжение — около 170 В.",
        evidence_items=(
            EvidenceItemPayload(
                text="По сообщениям жителей, свет отключили в нагорной части города и на Слободке. В центре города зафиксировано низкое напряжение — около 170 В.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(101,),
                service_state=ServiceStatePayload(
                    subject_key="electricity",
                    subject_label="Электроснабжение",
                    dimension="power_supply",
                    state="UNAVAILABLE",
                    location="Нагорная часть, Слободка, Центр",
                ),
            ),
        ),
    )

    # Story 2: Power restored on Petrovskogo
    payload_2 = EventPayload(
        headline="Электроснабжение восстановлено на Петровского",
        digest_summary="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
        evidence_items=(
            EvidenceItemPayload(
                text="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(102,),
                service_state=ServiceStatePayload(
                    subject_key="electricity",
                    subject_label="Электроснабжение",
                    dimension="power_supply",
                    state="AVAILABLE",
                    location="ул. Петровского",
                ),
            ),
        ),
    )

    # Story 3 (Incident failure item): "Сервис снова доступен: проблемный сервис..."
    payload_3 = EventPayload(
        headline="Сервис снова доступен",
        digest_summary="Несколько горожан сообщают, что проблемный сервис восстановил работу после перебоев.",
        evidence_items=(
            EvidenceItemPayload(
                text="Несколько горожан сообщают, что проблемный сервис восстановил работу после перебоев.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(103,),
                service_state=ServiceStatePayload(
                    subject_key="service",
                    subject_label="Сервис",
                    dimension="access",
                    state="AVAILABLE",
                    entity="проблемный сервис",
                ),
            ),
        ),
    )

    # Story 4 (Incident failure item): "В районе Водоканала: жители обсуждают текущую ситуацию..."
    payload_4 = EventPayload(
        headline="В районе Водоканала",
        digest_summary="В соцсетях жители обсуждают текущую ситуацию на Пролетарской вблизи Водоканала. Подробности уточняются.",
        evidence_items=(
            EvidenceItemPayload(
                text="В соцсетях жители обсуждают текущую ситуацию на Пролетарской вблизи Водоканала. Подробности уточняются.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(104,),
            ),
        ),
    )

    # 2. Assert Story Quality Acceptance Criteria
    is_valid_1, reason_1 = validate_story_publication_eligibility(payload_1)
    assert is_valid_1 is True, "Legitimate power outage must be eligible"

    is_valid_2, reason_2 = validate_story_publication_eligibility(payload_2)
    assert is_valid_2 is True, "Legitimate power restoration must be eligible"

    # Acceptance assertion 1:
    # "Сервис снова доступен" -> не может появиться без названного service/entity
    is_valid_3, reason_3 = validate_story_publication_eligibility(payload_3)
    assert is_valid_3 is False
    assert reason_3 == "service_access_without_concrete_entity"

    # Acceptance assertion 2:
    # "В районе Водоканала..." -> не может стать digest item без действия/состояния/события
    is_valid_4, reason_4 = validate_story_publication_eligibility(payload_4)
    assert is_valid_4 is False
    assert reason_4 == "lacks_meaningful_predicate"

    # 3. Simulate adapter output with filtered candidate cards
    # Only eligible stories produce cards
    card_1 = StoryCard(
        id="story:1",
        topic="Электричество пропало в нескольких районах",
        importance="high",
        summary="По сообщениям жителей, свет отключили в нагорной части города и на Слободке. В центре города зафиксировано низкое напряжение — около 170 В.",
        rubric_id="infrastructure",
        story_kind="operational_status",
        hard_facts=[
            StoryElement(
                text="свет отключили в нагорной части города",
                source_refs=["telegram:101"],
            ),
            StoryElement(
                text="на Слободке тоже 0 по свету",
                source_refs=["telegram:102"],
            ),
            StoryElement(
                text="в центре зафиксировано низкое напряжение — около 170 В",
                source_refs=["telegram:103"],
            ),
        ],
    )
    card_2 = StoryCard(
        id="story:2",
        topic="Электроснабжение восстановлено на Петровского",
        importance="high",
        summary="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
        rubric_id="infrastructure",
        story_kind="operational_status",
        hard_facts=[
            StoryElement(
                text="электричество вернулось на улицу Петровского",
                source_refs=["telegram:104"],
            )
        ],
    )
    candidate_cards = [card_1, card_2]

    # City situation rollup from operational observations (4 granular facts)
    city_rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="UNAVAILABLE",
                location="Нагорная часть",
                entity="",
                detail="свет отключили в нагорной части города",
                source_refs=("telegram:101",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="UNAVAILABLE",
                location="Слободка",
                entity="",
                detail="на Слободке тоже 0 по свету",
                source_refs=("telegram:102",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="DEGRADED",
                location="Центр",
                entity="",
                detail="в центре города зафиксировано низкое напряжение около 170 В",
                source_refs=("telegram:103",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="AVAILABLE",
                location="ул. Петровского",
                entity="",
                detail="электричество вернулось на улицу Петровского",
                source_refs=("telegram:104",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
        )
    )

    evidence_map = {
        "story:1:evi:1": PublicationEvidence(
            evidence_id="story:1:evi:1",
            story_id=1,
            text="свет отключили в нагорной части города",
            source_text="свет отключили в нагорной части города",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=101,
            source_ref="telegram:101",
            source_id=1,
            source_item_id=101,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:1:evi:2": PublicationEvidence(
            evidence_id="story:1:evi:2",
            story_id=1,
            text="на Слободке тоже 0 по свету",
            source_text="на Слободке тоже 0 по свету",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=102,
            source_ref="telegram:102",
            source_id=1,
            source_item_id=102,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:1:evi:3": PublicationEvidence(
            evidence_id="story:1:evi:3",
            story_id=1,
            text="в центре города зафиксировано низкое напряжение около 170 В",
            source_text="в центре города зафиксировано низкое напряжение около 170 В",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=103,
            source_ref="telegram:103",
            source_id=1,
            source_item_id=103,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:2:evi:1": PublicationEvidence(
            evidence_id="story:2:evi:1",
            story_id=2,
            text="электричество вернулось на улицу Петровского",
            source_text="электричество вернулось на улицу Петровского",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=104,
            source_ref="telegram:104",
            source_id=1,
            source_item_id=104,
            source_role="primary",
            observed_at=_NOW,
        ),
    }

    # 4. Build presentation plan
    plan = build_digest_presentation_plan(
        cards=candidate_cards,
        city_situation=city_rollup,
        evidence=evidence_map,
    )

    # Verify City Situation group consolidation
    assert len(plan.city_situation.groups) == 1
    power_group = plan.city_situation.groups[0]
    assert power_group.subject_label == "Электроснабжение"
    # Geographic mixed state: different locations have different states -> MIXED, not CONFLICTING
    assert power_group.state == "MIXED"
    # Semantic fact preservation: all 4 material facts survive presentation planning
    assert len(power_group.all_detail_lines) == 4
    assert any("нагорн" in line.lower() for line in power_group.all_detail_lines)
    assert any("слободк" in line.lower() for line in power_group.all_detail_lines)
    assert any("170" in line for line in power_group.all_detail_lines)
    assert any("петровск" in line.lower() for line in power_group.all_detail_lines)

    # Verify story presentation modes: both operational stories without distinct drilldown are DASHBOARD_ONLY
    assert len(plan.story_presentations) == 2
    assert plan.story_presentations[0].mode == "DASHBOARD_ONLY"
    assert plan.story_presentations[1].mode == "DASHBOARD_ONLY"
    assert plan.detail_story_ids == ()

    # 5. Render deterministic digest
    frozen = FrozenEditorialInput(
        analysis=EditorialAnalysis(cards=candidate_cards, city_situation=city_rollup),
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=2, candidate_count=2
        ),
    )
    renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
    title, lead, body = renderer.render_grouped_digest(
        frozen,
        edition_name="Бердянск",
        snapshot_at=_NOW,
        presentation_plan=plan,
    )

    # 6. Final Body Assertions
    # A. Header
    assert "Дайджест: Бердянск · 11.09.2026" in title

    # B. City situation must be present with yellow dot CONFLICTING status
    assert "*🏙 Городская обстановка*" in body
    assert "свет отключили в нагорной части города" in body
    assert "электричество вернулось на улицу Петровского" in body

    # C. Omission assertions (incident flaws must NOT appear)
    assert "Сервис снова доступен" not in body
    assert "проблемный сервис" not in body
    assert "В районе Водоканала" not in body
    assert "обсуждают текущую ситуацию" not in body
    assert "Подробности уточняются" not in body

    # D. Deduplication assertion: electricity must NOT be repeated in thematic rubrics
    assert "• **Электричество пропало в нескольких районах**" not in body
    assert "• **Электроснабжение восстановлено на Петровского**" not in body

    # E. Statistics footer must be present
    assert "Статистика:" in body

    # 7. Test grounded single_call narrative synthesis for Run 44
    from src.publication.digest_coverage import build_digest_coverage_trace
    from src.publication.digest_narrative import (
        DigestNarrativeDraft,
        DigestNarrativePlan,
        DigestSituationItemDraft,
        validate_digest_narrative,
    )

    sit_item = DigestSituationItemDraft(
        group_id="situation:electricity",
        label="Электроснабжение",
        body="По сообщениям жителей, свет отключили в нагорной части города и на Слободке; в центре зафиксировано низкое напряжение около 170 В, а на улице Петровского электроснабжение уже восстановили.",
        cited_support_ids=("telegram:101", "telegram:102", "telegram:103", "telegram:104"),
    )
    narr_draft = DigestNarrativeDraft(blocks=(), situation_items=(sit_item,))

    support_texts = {
        "telegram:101": "свет отключили в нагорной части города",
        "telegram:102": "на Слободке тоже 0 по свету",
        "telegram:103": "в центре зафиксировано низкое напряжение около 170 В",
        "telegram:104": "электричество вернулось на улицу Петровского",
    }
    narr_plan = DigestNarrativePlan(blocks=())
    val_res = validate_digest_narrative(
        narr_draft,
        narr_plan,
        support_index=support_texts,
        situation_plan=plan.city_situation,
    )
    assert val_res.is_valid, f"Narrative draft must be valid: {val_res.violations}"

    # Render with narrative draft
    title_n, lead_n, body_n = renderer.render_grouped_digest(
        frozen,
        edition_name="Бердянск",
        snapshot_at=_NOW,
        presentation_plan=plan,
        narrative_draft=narr_draft,
    )
    assert (
        "• 🟡 **Электроснабжение**: По сообщениям жителей, свет отключили в нагорной части города и на Слободке; в центре зафиксировано низкое напряжение около 170 В, а на улице Петровского электроснабжение уже восстановили."
        in body_n
    )
    assert "• **Электричество пропало в нескольких районах**" not in body_n
    assert "• **Электроснабжение восстановлено на Петровского**" not in body_n

    # Coverage trace
    cov_trace = build_digest_coverage_trace(plan, narr_draft, narr_plan)
    assert cov_trace.story_coverage == 1.0
