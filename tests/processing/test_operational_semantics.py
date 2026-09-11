from __future__ import annotations

import pytest

from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.processing.operational_semantics import (
    derive_operational_observations,
    normalize_service_state_evidence,
)

pytestmark = pytest.mark.unit


def _service_item(
    *,
    text: str,
    fid: int,
    subject_key: str,
    subject_label: str,
    state: str,
    expected_now: bool | None,
    basis: str,
    kind: str = "service_access",
    publication_use: str = "PUBLISH",
    effective_from: str | None = None,
    effective_until: str | None = None,
) -> EvidenceItemPayload:
    return EvidenceItemPayload(
        text=text,
        kind=kind,  # type: ignore[arg-type]
        publication_use=publication_use,  # type: ignore[arg-type]
        source_fragment_ids=(fid,),
        service_state=ServiceStatePayload(
            subject_key=subject_key,
            subject_label=subject_label,
            dimension="availability",
            state=state,
            expected_now=expected_now,
            basis=basis,  # type: ignore[arg-type]
            effective_from=effective_from,
            effective_until=effective_until,
        ),
    )


def test_valid_water_service_state_projects_exactly_once():
    payload = EventPayload(
        evidence_items=(
            _service_item(
                text="Water is absent on upper floors",
                fid=1,
                subject_key="water_supply",
                subject_label="Water supply",
                state="UNAVAILABLE",
                expected_now=True,
                basis="direct_failure",
            ),
        )
    )

    normalized, audit = normalize_service_state_evidence(payload)
    observations = derive_operational_observations(normalized)

    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert len(observations) == 1
    assert observations[0].subject_key == "water_supply"
    assert observations[0].detail == "Water is absent on upper floors"
    assert observations[0].source_fragment_ids == (1,)


def test_negative_state_without_expected_now_is_not_operational():
    payload = EventPayload(
        evidence_items=(
            _service_item(
                text="Residents say central heating is absent",
                fid=2,
                subject_key="heating",
                subject_label="Heating",
                state="UNAVAILABLE",
                expected_now=None,
                basis="direct_failure",
            ),
        )
    )

    normalized, audit = normalize_service_state_evidence(payload)

    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None
    assert derive_operational_observations(normalized) == ()


@pytest.mark.parametrize(
    ("state", "basis", "expected_now"),
    [
        ("AVAILABLE", "normal_operation", True),
        ("UNAVAILABLE", "direct_failure", True),
        ("DEGRADED", "degraded_access", True),
        ("RESTRICTED", "explicit_restriction", True),
    ],
)
def test_valid_state_basis_pairs_survive(state: str, basis: str, expected_now: bool | None):
    item = _service_item(
        text=f"Public transport status update for {state}",
        fid=10,
        subject_key="public_transport",
        subject_label="Транспорт",
        state=state,
        expected_now=expected_now,
        basis=basis,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


@pytest.mark.parametrize(
    ("state", "basis"),
    [
        ("AVAILABLE", "direct_failure"),
        ("UNAVAILABLE", "normal_operation"),
        ("DEGRADED", "scheduled_change"),
    ],
)
def test_incompatible_state_basis_is_rejected(state: str, basis: str):
    item = _service_item(
        text="Service mismatch text",
        fid=11,
        subject_key="water_supply",
        subject_label="Водоснабжение",
        state=state,
        expected_now=True if state != "AVAILABLE" else None,
        basis=basis,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_scheduled_requires_effective_from():
    item = _service_item(
        text="Scheduled maintenance",
        fid=12,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from=None,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_private_generator_use_is_demoted_not_dropped():
    item = _service_item(
        text="Residents run their household generators at night",
        fid=3,
        subject_key="backup_power",
        subject_label="Backup power",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    result = normalized.evidence_items[0]
    assert audit.rejected_count == 1
    assert result.kind == "community_report"
    assert result.publication_use == "PUBLISH"
    assert result.service_state is None
    assert result.text == item.text


def test_generator_mechanism_does_not_kill_explicit_water_outcome():
    item = _service_item(
        text="The building generator powers the pump, so water is available daily",
        fid=4,
        subject_key="water_supply",
        subject_label="Water supply",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


def test_provider_connectivity_evidence_cannot_project_as_city_power_supply():
    item = _service_item(
        text="Provider equipment is offline and internet connectivity is unavailable",
        fid=5,
        subject_key="power_supply",
        subject_label="Electricity",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )

    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_ambiguous_unrecognized_subject_is_not_guessed():
    item = _service_item(
        text="Service X is unavailable today",
        fid=6,
        subject_key="service_x",
        subject_label="Service X",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )

    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


def test_retail_commodity_sale_is_excluded_from_operational_observations():
    from src.processing.operational_semantics import derive_operational_observations

    item = _service_item(
        text="Вода на розлив по 3 ₽/литр в киоске на Восточном",
        fid=7,
        subject_key="water",
        subject_label="Вода на розлив",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    payload = EventPayload(evidence_items=(item,))
    observations = derive_operational_observations(payload)
    assert len(observations) == 0


def test_toponym_normalization_in_operational_observations():
    item = _service_item(
        text="В микрорайоне Гора вода появилась",
        fid=8,
        subject_key="water_supply",
        subject_label="Водоснабжение",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    payload = EventPayload(evidence_items=(item,))
    observations = derive_operational_observations(payload)
    assert len(observations) == 1
    assert "нагорной части города" in observations[0].detail
    assert "микрорайоне Гора" not in observations[0].detail


@pytest.mark.unit
def test_normalize_berdyansk_toponyms_jupiter():
    from src.processing.operational_semantics import normalize_berdyansk_toponyms

    t1 = "В районе Юпитер пока отсутствует оптоволокно."
    assert normalize_berdyansk_toponyms(t1) == "У провайдера «Юпитер» пока отсутствует оптоволокно."

    t2 = "В бердянском районе «Юпитер» пока нет оптоволоконного подключения"
    assert (
        normalize_berdyansk_toponyms(t2)
        == "У провайдера «Юпитер» пока нет оптоволоконного подключения"
    )

    t3 = "район Юпитер сообщает о работах"
    assert normalize_berdyansk_toponyms(t3) == "провайдер «Юпитер» сообщает о работах"


@pytest.mark.unit
def test_normalize_berdyansk_toponyms_plane_monument_and_lanterns():
    from src.processing.operational_semantics import normalize_berdyansk_toponyms

    t1 = "У «Літака» выброшены новые фонари, предназначавшиеся для установки — сообщают жители."
    assert (
        normalize_berdyansk_toponyms(t1)
        == "У памятника Самолёту выброшены новые фонари, предназначавшиеся для установки — сообщают жители."
    )

    t2 = "возле «Літака» лежат новые лихтари"
    assert normalize_berdyansk_toponyms(t2) == "у памятника Самолёту лежат новые фонари"

    t3 = "Окупаційна влада відзвітувала про ремонт біля пам'ятника «Літак»"
    assert (
        normalize_berdyansk_toponyms(t3)
        == "Окупаційна влада відзвітувала про ремонт возле памятника Самолёту"
    )


@pytest.mark.unit
def test_normalize_operational_location_and_entity_jupiter_and_plane():
    from src.processing.operational_semantics import normalize_operational_location_and_entity

    loc, ent = normalize_operational_location_and_entity("район Юпитер")
    assert ent == "Юпитер"
    assert loc == ""

    loc, ent = normalize_operational_location_and_entity("у Літака")
    assert "памятник Самолёту" in loc
    assert "Довганюка" in loc

    loc, ent = normalize_operational_location_and_entity("памятник Самолёт в селе Осипенко")
    assert "село Осипенко" in loc


def test_raw_grounding_rejects_hallucinated_service_family():
    # Model hallucinated power_supply from raw text with no service keywords
    raw_texts = {101: "А после 20го вырубят всё"}
    item = _service_item(
        text="А после 20го вырубят всё",
        fid=101,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )
    payload = EventPayload(evidence_items=(item,))
    normalized, audit = normalize_service_state_evidence(payload, raw_texts)

    assert audit.rejected_count == 1
    assert "ungrounded_service_family" in audit.rejection_reasons
    assert normalized.evidence_items[0].service_state is None
    assert normalized.evidence_items[0].kind == "community_report"
    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_raw_grounding_uses_source_fragment_texts_over_model_item_text():
    # Model hallucinated electricity into item.text, but raw source has no electricity
    raw_texts = {102: "А после 20го вырубят всё"}
    item = _service_item(
        text="Жители сообщают, что после 20-го электроэнергию отключат полностью",
        fid=102,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )
    payload = EventPayload(evidence_items=(item,))
    normalized, audit = normalize_service_state_evidence(payload, raw_texts)

    assert audit.rejected_count == 1
    assert "ungrounded_service_family" in audit.rejection_reasons
    assert normalized.evidence_items[0].service_state is None


def test_scheduled_requires_schedule_indicators_and_blocks_rumors():
    raw_texts_rumor = {103: "Свет после 20-го опять всем отрубят, вот увидите"}
    item_rumor = _service_item(
        text="Свет после 20-го опять всем отрубят, вот увидите",
        fid=103,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_rumor,)), raw_texts_rumor
    )
    assert audit.rejected_count == 1
    assert "unsupported_scheduled_change" in audit.rejection_reasons
    assert normalized.evidence_items[0].service_state is None

    # Legitimate schedule notice succeeds
    raw_texts_official = {
        104: "РЭС предупреждает: 20 сентября с 09:00 плановое отключение электроэнергии"
    }
    item_official = _service_item(
        text="РЭС предупреждает: 20 сентября с 09:00 плановое отключение электроэнергии",
        fid=104,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    normalized_off, audit_off = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_official,)), raw_texts_official
    )
    assert audit_off.accepted_count == 1
    assert audit_off.rejected_count == 0
    assert normalized_off.evidence_items[0].service_state is not None


def test_dependent_reply_inherits_family_from_parent_but_rejects_scheduled_without_schedule_indicators():
    # Context: Parent has "светится" (power), reply has "после 20го вырубят всё"
    # Even if parent gives power family grounding, banter reply without schedule keywords cannot be SCHEDULED
    combined_grounding = (
        '[in reply to: "К 17.09 весь город будет светится⚡️⚡️⚡️😇"] А после 20го вырубят всё'
    )
    raw_texts = {201: combined_grounding}
    item = _service_item(
        text="По словам жителя, после 20-го числа электроэнергию отключат полностью",
        fid=201,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,)), raw_texts
    )
    assert audit.rejected_count == 1
    assert "unsupported_scheduled_change" in audit.rejection_reasons
    assert normalized.evidence_items[0].service_state is None
    assert normalized.evidence_items[0].kind == "community_report"
    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_missing_raw_grounding_fails_closed_when_fragment_texts_provided():
    # When fragment_texts mapping is provided but referenced fragment ID is missing,
    # it must fail-closed: service_state=None, community_report / CONTEXT, reason="missing_raw_grounding"
    item = _service_item(
        text="В Бердянске нет воды из-за аварии на водоводе",
        fid=999,  # Not in fragment_texts
        subject_key="water_supply",
        subject_label="Водоснабжение",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )
    fragment_texts = {100: "Совершенно другой фрагмент"}
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,)), fragment_texts
    )
    assert audit.rejected_count == 1
    assert "missing_raw_grounding" in audit.rejection_reasons
    assert normalized.evidence_items[0].service_state is None
    assert normalized.evidence_items[0].kind == "community_report"
    assert normalized.evidence_items[0].publication_use == "CONTEXT"


def test_legacy_fallback_allowed_when_fragment_texts_is_none():
    # Legacy fallback to item.text is only permitted when fragment_texts is None
    item = _service_item(
        text="В Бердянске нет воды из-за аварии на водоводе",
        fid=101,
        subject_key="water_supply",
        subject_label="Водоснабжение",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,)), fragment_texts=None
    )
    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


def test_scheduled_three_part_proof_negative_and_positive_cases():
    # Negative Case 1: raw: "Жители сообщают, что сейчас нет света", model effective_from: 2026-09-20 => reject
    item_neg1 = _service_item(
        text="Жители сообщают, что сейчас нет света",
        fid=1,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    norm1, audit1 = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_neg1,)), {1: "Жители сообщают, что сейчас нет света"}
    )
    assert audit1.rejected_count == 1
    assert "unsupported_scheduled_change" in audit1.rejection_reasons
    assert norm1.evidence_items[0].service_state is None

    # Negative Case 2: raw: "Говорят, РЭС после 20-го выключит свет" => reject
    item_neg2 = _service_item(
        text="Говорят, РЭС после 20-го выключит свет",
        fid=2,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    norm2, audit2 = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_neg2,)), {2: "Говорят, РЭС после 20-го выключит свет"}
    )
    assert audit2.rejected_count == 1
    assert "unsupported_scheduled_change" in audit2.rejection_reasons
    assert norm2.evidence_items[0].service_state is None

    # Negative Case 3: raw: "РЭС сообщает о ремонтных работах", model effective_from: 2026-09-20 => reject (20 сентября missing)
    item_neg3 = _service_item(
        text="РЭС сообщает о ремонтных работах",
        fid=3,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20",
    )
    norm3, audit3 = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_neg3,)), {3: "РЭС сообщает о ремонтных работах"}
    )
    assert audit3.rejected_count == 1
    assert "unsupported_scheduled_change" in audit3.rejection_reasons
    assert norm3.evidence_items[0].service_state is None

    # Positive Case: "РЭС предупреждает: 20 сентября с 09:00 плановое отключение электроэнергии" => SCHEDULED accepted
    item_pos = _service_item(
        text="РЭС предупреждает: 20 сентября с 09:00 плановое отключение электроэнергии",
        fid=4,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20T09:00:00",
    )
    norm4, audit4 = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_pos,)),
        {4: "РЭС предупреждает: 20 сентября с 09:00 плановое отключение электроэнергии"},
    )
    assert audit4.accepted_count == 1
    assert audit4.rejected_count == 0
    assert norm4.evidence_items[0].service_state is not None
    assert norm4.evidence_items[0].service_state.effective_from == "2026-09-20T09:00:00"


def test_scheduled_temporal_grounding_strips_ungrounded_time_component_to_date_only():
    from src.processing.operational_semantics import _has_grounded_temporal_value

    # Direct function test: time present vs missing in text
    assert not _has_grounded_temporal_value("РЭС: 20 сентября плановые работы", "2026-09-20T09:00")
    assert _has_grounded_temporal_value(
        "РЭС: 20 сентября с 09:00 плановые работы", "2026-09-20T09:00"
    )

    # Normalization test: when date is grounded but time is ungrounded,
    # time component is stripped and date-only is preserved.
    item_ungrounded_time = _service_item(
        text="РЭС сообщает: 20 сентября плановое отключение электроэнергии",
        fid=10,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from="2026-09-20T09:00",
    )
    norm, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item_ungrounded_time,)),
        {10: "РЭС сообщает: 20 сентября плановое отключение электроэнергии"},
    )
    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert norm.evidence_items[0].service_state is not None
    assert norm.evidence_items[0].service_state.effective_from == "2026-09-20"


def test_has_grounded_time_value_strict_minute_and_clock_context():
    from src.processing.operational_semantics import _has_grounded_time_value

    # When minute != 0 (e.g. 09:30), bare hour references or durations must be rejected
    assert not _has_grounded_time_value("20 сентября в 9 часов", "2026-09-20T09:30")
    assert not _has_grounded_time_value("работы продлятся 9 часов", "2026-09-20T09:30")
    assert _has_grounded_time_value("отключение в 09:30", "2026-09-20T09:30")
    assert _has_grounded_time_value("отключение в 9.30", "2026-09-20T09:30")

    # When minute == 0 (e.g. 09:00), bare duration '9 часов' must be rejected
    assert not _has_grounded_time_value("работы продлятся 9 часов", "2026-09-20T09:00")
    assert _has_grounded_time_value("отключение в 9:00", "2026-09-20T09:00")
    assert _has_grounded_time_value("отключение с 9 утра", "2026-09-20T09:00")
    assert _has_grounded_time_value("отключение в 9 часов", "2026-09-20T09:00")


def test_has_grounded_temporal_value_may_calendar_matching():
    from src.processing.operational_semantics import _has_grounded_temporal_value

    # "20 мая" contains May (month 5), must reject projected date in September (month 9)
    assert not _has_grounded_temporal_value("20 мая плановые работы", "2026-09-20")
    assert not _has_grounded_temporal_value("20 травня планові роботи", "2026-09-20")

    # Matching month passes
    assert _has_grounded_temporal_value("20 сентября плановые работы", "2026-09-20")
    assert _has_grounded_temporal_value("20 вересня планові роботи", "2026-09-20")
