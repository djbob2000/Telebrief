"""Unit tests for pure temporal operational state resolution."""

from __future__ import annotations

import datetime as dt

from src.domain.event_payload import OperationalObservationPayload
from src.domain.operational_state import (
    resolve_operational_states,
)


def test_resolve_operational_states_empty():
    assert resolve_operational_states([]) == []


def test_resolve_operational_states_single_observation():
    t0 = dt.datetime(2026, 8, 29, 8, 0, tzinfo=dt.timezone.utc)
    obs = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Аварийное отключение",
        source_fragment_ids=(101,),
    )

    resolved = resolve_operational_states([(obs, t0, ["ref-1"])])
    assert len(resolved) == 1
    st = resolved[0]
    assert st.subject_key == "water_supply"
    assert st.location == "Колония"
    assert st.current_state == "UNAVAILABLE"
    assert st.detail == "Аварийное отключение"
    assert st.first_observed_at == t0
    assert st.last_observed_at == t0
    assert st.observation_count == 1
    assert st.source_refs == ("ref-1",)
    assert len(st.history) == 1


def test_resolve_operational_states_temporal_evolution():
    t0 = dt.datetime(2026, 8, 29, 8, 0, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2026, 8, 29, 11, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 8, 29, 14, 0, tzinfo=dt.timezone.utc)

    obs0 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Утренний порыв",
        source_fragment_ids=(101,),
    )
    obs1 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Ремонтные работы",
        source_fragment_ids=(102,),
    )
    obs2 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Колония",
        entity="водовод",
        state="AVAILABLE",
        detail="Водоснабжение восстановлено",
        source_fragment_ids=(103,),
    )

    # Pass in shuffled order to test chronological sorting
    items = [
        (obs1, t1, ["ref-2", "ref-common"]),
        (obs0, t0, ["ref-1", "ref-common"]),
        (obs2, t2, ["ref-3"]),
    ]

    resolved = resolve_operational_states(items)
    assert len(resolved) == 1
    st = resolved[0]
    assert st.current_state == "AVAILABLE"
    assert st.detail == "Водоснабжение восстановлено"
    assert st.first_observed_at == t0
    assert st.last_observed_at == t2
    assert st.observation_count == 3
    # Source refs should be unique union in order of chronological appearance
    assert st.source_refs == ("ref-1", "ref-common", "ref-2", "ref-3")
    assert len(st.history) == 3
    assert st.history[0].detail == "Утренний порыв"
    assert st.history[-1].detail == "Водоснабжение восстановлено"


def test_resolve_operational_states_multiple_entities_sorted_deterministically():
    t0 = dt.datetime(2026, 8, 29, 8, 0, tzinfo=dt.timezone.utc)

    obs_atm = OperationalObservationPayload(
        subject_key="banking_cash",
        subject_label="Банкоматы",
        dimension="availability",
        location="АКЗ",
        entity="Дельмар банкомат",
        state="AVAILABLE",
        detail="Выдает рубли",
        source_fragment_ids=(201,),
    )
    obs_power = OperationalObservationPayload(
        subject_key="power_supply",
        subject_label="Электросеть",
        dimension="availability",
        location="Центр",
        entity="сеть",
        state="UNAVAILABLE",
        detail="Отключение",
        source_fragment_ids=(301,),
    )

    resolved = resolve_operational_states(
        [
            (obs_power, t0, ["ref-p"]),
            (obs_atm, t0, ["ref-a"]),
        ]
    )

    assert len(resolved) == 2
    # banking_cash comes before power_supply alphabetically
    assert resolved[0].subject_key == "banking_cash"
    assert resolved[1].subject_key == "power_supply"


def test_resolve_operational_states_off_on_off_timeline():
    """OFF 09:00 -> ON 14:00 -> OFF 18:10 at 20:00 => UNAVAILABLE, history preserved."""
    t0 = dt.datetime(2026, 8, 29, 9, 0, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2026, 8, 29, 14, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 8, 29, 18, 10, tzinfo=dt.timezone.utc)
    snap = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)

    obs0 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="АКЗ",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Утренний порыв",
        source_fragment_ids=(101,),
    )
    obs1 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="АКЗ",
        entity="водовод",
        state="AVAILABLE",
        detail="Вода подана",
        source_fragment_ids=(102,),
    )
    obs2 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="АКЗ",
        entity="водовод",
        state="UNAVAILABLE",
        detail="Повторный порыв вечером",
        source_fragment_ids=(103,),
    )

    resolved = resolve_operational_states(
        [(obs0, t0, ["r0"]), (obs1, t1, ["r1"]), (obs2, t2, ["r2"])],
        snapshot_at=snap,
        conflict_window_minutes=90,
    )
    assert len(resolved) == 1
    st = resolved[0]
    assert st.current_state == "UNAVAILABLE"
    assert st.status == "UNAVAILABLE"
    assert st.detail == "Повторный порыв вечером"
    assert len(st.history) == 3
    assert len(st.resolved_history) == 3


def test_resolve_operational_states_conflicting_window():
    """ON 18:10 and OFF 18:12 with comparable evidence => CONFLICTING."""
    t0 = dt.datetime(2026, 8, 29, 18, 10, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2026, 8, 29, 18, 12, tzinfo=dt.timezone.utc)
    snap = dt.datetime(2026, 8, 29, 19, 0, tzinfo=dt.timezone.utc)

    obs0 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Центр",
        entity="сеть",
        state="AVAILABLE",
        detail="Вода есть на 3 этаже",
        source_fragment_ids=(201,),
    )
    obs1 = OperationalObservationPayload(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Центр",
        entity="сеть",
        state="UNAVAILABLE",
        detail="Краны сухие",
        source_fragment_ids=(202,),
    )

    resolved = resolve_operational_states(
        [(obs0, t0, ["r0"]), (obs1, t1, ["r1"])],
        snapshot_at=snap,
        conflict_window_minutes=90,
    )
    assert len(resolved) == 1
    st = resolved[0]
    assert st.current_state == "CONFLICTING"
    assert st.status == "CONFLICTING"
    assert len(st.current_observations) == 2


def test_resolve_operational_states_scheduled_future():
    """An outage announced on Aug 29 but effective Aug 30 08:00–17:00 at Aug 29 20:00 => SCHEDULED, not UNAVAILABLE."""
    announced_at = dt.datetime(2026, 8, 29, 15, 0, tzinfo=dt.timezone.utc)
    snap = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)

    obs = OperationalObservationPayload(
        subject_key="gas_supply",
        subject_label="Газоснабжение",
        dimension="availability",
        location="Коса",
        entity="ГРС",
        state="SCHEDULED",
        detail="Плановые работы на ГРС 30 августа",
        source_fragment_ids=(301,),
        effective_from="2026-08-30T08:00:00Z",
        effective_until="2026-08-30T17:00:00Z",
    )

    resolved = resolve_operational_states([(obs, announced_at, ["r-gas"])], snapshot_at=snap)
    assert len(resolved) == 1
    st = resolved[0]
    assert st.current_state == "SCHEDULED"
    assert st.status == "SCHEDULED"
    assert st.next_scheduled_change is not None
    assert st.next_scheduled_change.effective_from == dt.datetime(
        2026, 8, 30, 8, 0, tzinfo=dt.timezone.utc
    )


def test_resolve_operational_states_scheduled_active_during_window():
    """At Aug 30 09:00 the same scheduled record => UNAVAILABLE if no newer contradictory evidence exists."""
    announced_at = dt.datetime(2026, 8, 29, 15, 0, tzinfo=dt.timezone.utc)
    snap = dt.datetime(2026, 8, 30, 9, 0, tzinfo=dt.timezone.utc)

    obs = OperationalObservationPayload(
        subject_key="gas_supply",
        subject_label="Газоснабжение",
        dimension="availability",
        location="Коса",
        entity="ГРС",
        state="SCHEDULED",
        detail="Плановые работы на ГРС 30 августа",
        source_fragment_ids=(301,),
        effective_from="2026-08-30T08:00:00Z",
        effective_until="2026-08-30T17:00:00Z",
    )

    resolved = resolve_operational_states([(obs, announced_at, ["r-gas"])], snapshot_at=snap)
    assert len(resolved) == 1
    st = resolved[0]
    assert st.current_state == "UNAVAILABLE"
    assert st.status == "UNAVAILABLE"
