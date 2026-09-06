# Unified Service-State Truth & Operational Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dual LLM-authored service-state representation with one canonical `service_access.service_state` evidence model, derive City Situation deterministically from it, remove scan-first digest lead duplication, and harden broad-regional scope without adding another LLM stage or any database migration.

**Architecture:** Gate v7 and Analysis v6 emit structured service state only inside `EvidenceItemPayload`; they no longer emit an independent `operational_observations` array. A deterministic semantic normalizer validates service-state structure, current expectation, high-confidence coping false positives, and subject-family conflicts; the publication adapter then derives existing `OperationalObservationPayload` objects internally so the current temporal resolver and City Situation rollup can remain unchanged. Regional scope gains an explicit fragment-basis trace plus a narrow broad-region guard. Because this branch has not shipped, this is a branch-level contract cutover with fresh semantic regeneration, not a database migration/backfill project.

**Tech Stack:** Python 3.14, dataclasses, PostgreSQL JSONB story revisions, pytest, existing Event-First Gate/Analysis ProviderCascade, existing temporal operational-state resolver, existing digest presentation planner/renderer.

**Spec:** `docs/superpowers/specs/2026-08-31-unified-service-state-truth-operational-semantics-design.md`

## Global Constraints

- Baseline branch: `multisource-roadmap` at or after `b339c5c74b2d7e313dd21e245ab3c267fd1849dc`.
- This branch has **not** been deployed to production.
- **Do not create a database migration for this plan.**
- Do not backfill old local/test EventPayload JSON.
- Gate v7 / Analysis v6 are the semantic freshness boundary; acceptance runs must regenerate current Event-First semantics.
- `service_access.service_state` is the only LLM-authored source of operational truth in v7/v6.
- Gate v7 / Analysis v6 must not request or accept a second LLM-authored `operational_observations` contract.
- `OperationalObservationPayload` may remain as an internal derived temporal-resolution type.
- `expected_now` must be nullable and must never default to `True`.
- Do not reject a service state merely because its evidence mentions a generator, battery, solar panel, well, router, or other workaround mechanism.
- High-confidence coping rejection requires a resident/private coping pattern **without an explicit external service outcome**.
- Invalid service-state projection must not delete otherwise legitimate PUBLISH evidence or the Story.
- No official/two-source corroboration requirement.
- No city-specific or provider-specific production hardcoding.
- Existing City Situation temporal conflict resolution remains authoritative after service-state derivation.
- Existing subject-coherent digest presentation remains authoritative after City Situation rollup.
- No new generative LLM stage, reviewer, repair call, or retry loop.
- Digest narrative remains at most one writer call.
- Article behavior is out of scope except that shared EventPayload parsing must continue to work.

---

## File map

### Create

- `src/domain/service_state.py` — service-state dataclass, enum literals, structural parsing/validation helpers, generic family detection.
- `tests/domain/test_service_state.py` — domain contract tests.
- `tests/fixtures/unified_service_state_golden.json` — generic end-to-end semantic regression fixture.
- `docs/superpowers/specs/2026-08-31-unified-service-state-truth-operational-semantics-design.md` — canonical design spec.

### Modify

- `src/domain/event_payload.py` — attach optional `service_state` to evidence; stop treating `EventPayload.operational_observations` as canonical v7/v6 payload data.
- `src/processing/operational_semantics.py` — replace fragment-overlap self-validation with service-state normalization + derived projection.
- `src/processing/event_triage.py` — Gate v7 wire contract, scope basis fragment IDs, broad-region guard, recent subject hints from service-state evidence.
- `src/processing/event_analysis.py` — Analysis v6 wire contract and shared service-state normalizer.
- `src/processing/edition_scope.py` — narrow deterministic broad-regional-summary guard.
- `src/publication/event_editorial_adapter.py` — derive operational observations from service-state evidence instead of reading a parallel array.
- `src/publication/renderers.py` — grouped/channel digest returns empty lead.
- `tests/processing/test_operational_semantics.py`
- `tests/processing/test_event_triage.py`
- `tests/processing/test_event_analysis.py`
- `tests/processing/test_edition_scope.py` if present; otherwise create it.
- `tests/publication/test_event_editorial_adapter.py` if present; otherwise use the closest existing adapter test module.
- `tests/publication/test_renderers.py`
- `tests/integration/test_city_situation_golden.py`
- `tests/integration/test_publication_quality_golden.py`
- `tests/fixtures/berdyansk_city_situation_golden.json`
- `tests/scripts/test_benchmark_publication_quality.py`
- `AGENTS.md`

### Explicitly do not create

- No `migrations/0024_*.sql` or any other migration for this work.
- No one-off backfill script for old operational arrays.

---

### Task 1: Introduce the canonical `ServiceStatePayload` domain model

**Files:**
- Create: `src/domain/service_state.py`
- Create: `tests/domain/test_service_state.py`
- Modify: `src/domain/event_payload.py`

**Interfaces:**
- Produces:

```python
ServiceStateBasis = Literal[
    "normal_operation",
    "direct_failure",
    "degraded_access",
    "explicit_restriction",
    "scheduled_change",
]


@dataclass(frozen=True)
class ServiceStatePayload:
    subject_key: str
    subject_label: str
    dimension: str
    state: str
    location: str = ""
    entity: str = ""
    expected_now: bool | None = None
    basis: ServiceStateBasis = "normal_operation"
    effective_from: str | None = None
    effective_until: str | None = None

    def to_dict(self) -> dict[str, Any]: ...

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServiceStatePayload": ...
```

- Extends:

```python
@dataclass(frozen=True)
class EvidenceItemPayload:
    ...
    service_state: ServiceStatePayload | None = None
```

- Later tasks consume `EvidenceItemPayload.service_state`.

- [ ] **Step 1: Write failing `ServiceStatePayload` parsing tests**

Create `tests/domain/test_service_state.py`:

```python
import pytest

from src.domain.service_state import ServiceStatePayload


def test_service_state_round_trips_current_water_outage():
    raw = {
        "subject_key": "water_supply",
        "subject_label": "Водоснабжение",
        "dimension": "availability",
        "state": "UNAVAILABLE",
        "location": "верхние этажи",
        "entity": "",
        "expected_now": True,
        "basis": "direct_failure",
    }

    state = ServiceStatePayload.from_dict(raw)

    assert state.subject_key == "water_supply"
    assert state.state == "UNAVAILABLE"
    assert state.expected_now is True
    assert state.basis == "direct_failure"
    assert state.to_dict() == raw


def test_expected_now_is_not_implicitly_true():
    state = ServiceStatePayload.from_dict(
        {
            "subject_key": "heating",
            "subject_label": "Отопление",
            "dimension": "availability",
            "state": "UNKNOWN",
            "basis": "direct_failure",
        }
    )

    assert state.expected_now is None


def test_scheduled_state_requires_valid_timestamp_when_provided():
    with pytest.raises(ValueError, match="effective_from"):
        ServiceStatePayload.from_dict(
            {
                "subject_key": "transport",
                "subject_label": "Транспорт",
                "dimension": "availability",
                "state": "SCHEDULED",
                "basis": "scheduled_change",
                "expected_now": False,
                "effective_from": "not-a-date",
            }
        )
```

- [ ] **Step 2: Run the tests to verify RED**

```bash
pytest tests/domain/test_service_state.py -q --no-cov
```

Expected: FAIL because `src.domain.service_state` does not exist.

- [ ] **Step 3: Implement the model with strict enum validation**

Create `src/domain/service_state.py` with:

```python
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

SERVICE_STATE_VALUES = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "DEGRADED", "RESTRICTED", "UNKNOWN", "SCHEDULED"}
)

SERVICE_STATE_BASES = frozenset(
    {
        "normal_operation",
        "direct_failure",
        "degraded_access",
        "explicit_restriction",
        "scheduled_change",
    }
)

ServiceStateBasis = Literal[
    "normal_operation",
    "direct_failure",
    "degraded_access",
    "explicit_restriction",
    "scheduled_change",
]


def _parse_optional_iso(value: Any, *, field_name: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    return text


@dataclass(frozen=True)
class ServiceStatePayload:
    subject_key: str
    subject_label: str
    dimension: str
    state: str
    location: str = ""
    entity: str = ""
    expected_now: bool | None = None
    basis: ServiceStateBasis = "normal_operation"
    effective_from: str | None = None
    effective_until: str | None = None

    def __post_init__(self) -> None:
        if not self.subject_key.strip():
            raise ValueError("service_state.subject_key cannot be empty")
        if not self.subject_label.strip():
            raise ValueError("service_state.subject_label cannot be empty")
        if not self.dimension.strip():
            raise ValueError("service_state.dimension cannot be empty")
        if self.state.upper() not in SERVICE_STATE_VALUES:
            raise ValueError(f"invalid service_state.state: {self.state}")
        if self.basis not in SERVICE_STATE_BASES:
            raise ValueError(f"invalid service_state.basis: {self.basis}")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "subject_key": self.subject_key,
            "subject_label": self.subject_label,
            "dimension": self.dimension,
            "state": self.state.upper(),
            "location": self.location,
            "entity": self.entity,
            "expected_now": self.expected_now,
            "basis": self.basis,
        }
        if self.effective_from is not None:
            data["effective_from"] = self.effective_from
        if self.effective_until is not None:
            data["effective_until"] = self.effective_until
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServiceStatePayload":
        raw_expected = data.get("expected_now")
        if raw_expected is not None and not isinstance(raw_expected, bool):
            raise ValueError("service_state.expected_now must be boolean or null")

        raw_basis = str(data.get("basis", "normal_operation")).strip()
        if raw_basis not in SERVICE_STATE_BASES:
            raise ValueError(f"invalid service_state.basis: {raw_basis}")

        return cls(
            subject_key=str(data.get("subject_key", "")).strip(),
            subject_label=str(data.get("subject_label", "")).strip(),
            dimension=str(data.get("dimension", "availability")).strip(),
            state=str(data.get("state", "")).strip().upper(),
            location=str(data.get("location", "")).strip(),
            entity=str(data.get("entity", "")).strip(),
            expected_now=raw_expected,
            basis=cast(ServiceStateBasis, raw_basis),
            effective_from=_parse_optional_iso(
                data.get("effective_from"), field_name="effective_from"
            ),
            effective_until=_parse_optional_iso(
                data.get("effective_until"), field_name="effective_until"
            ),
        )
```

Also validate `effective_until >= effective_from` when both exist.

- [ ] **Step 4: Add failing EvidenceItem service-state round-trip tests**

Add to the same test file:

```python
from src.domain.event_payload import EvidenceItemPayload


def test_evidence_item_round_trips_nested_service_state():
    item = EvidenceItemPayload.from_dict(
        {
            "text": "Water does not reach upper floors",
            "kind": "service_access",
            "publication_use": "PUBLISH",
            "source_fragment_ids": [11],
            "service_state": {
                "subject_key": "water_supply",
                "subject_label": "Водоснабжение",
                "dimension": "availability",
                "state": "UNAVAILABLE",
                "location": "upper floors",
                "entity": "",
                "expected_now": True,
                "basis": "direct_failure",
            },
        }
    )

    assert item.service_state is not None
    assert item.service_state.subject_key == "water_supply"
    assert item.to_dict()["service_state"]["state"] == "UNAVAILABLE"
```

- [ ] **Step 5: Extend `EvidenceItemPayload`**

In `src/domain/event_payload.py`:

```python
from src.domain.service_state import ServiceStatePayload
```

Add:

```python
service_state: ServiceStatePayload | None = None
```

`to_dict()`:

```python
payload = {
    "text": self.text,
    "kind": self.kind,
    "publication_use": self.publication_use,
    "source_fragment_ids": list(self.source_fragment_ids),
}
if self.service_state is not None:
    payload["service_state"] = self.service_state.to_dict()
return payload
```

`from_dict()`:

```python
raw_service_state = data.get("service_state")
service_state = (
    ServiceStatePayload.from_dict(raw_service_state)
    if isinstance(raw_service_state, Mapping)
    else None
)
```

Pass `service_state=service_state` to the constructor.

- [ ] **Step 6: Keep raw schema parsing permissive; strict semantics belong to Task 2**

Do **not** make `EvidenceItemPayload.from_dict()` itself reject every old `service_access` item missing `service_state`. The generic parser is shared by stored local revisions and tests. Task 2 owns the v7/v6 canonical semantic normalization.

- [ ] **Step 7: Run domain tests**

```bash
pytest tests/domain/test_service_state.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 8: Run EventPayload regression tests**

```bash
pytest tests/processing/test_event_triage.py tests/processing/test_event_analysis.py -q --no-cov
```

Expected: existing tests may fail only where they assert the old output schema; record those exact failures for Tasks 3-4 rather than restoring the old contract.

- [ ] **Step 9: Commit**

```bash
git add src/domain/service_state.py src/domain/event_payload.py tests/domain/test_service_state.py
git commit -m "feat(events): add canonical service state evidence model"
```

---

### Task 2: Replace fragment-overlap self-validation with canonical service-state normalization

**Files:**
- Modify: `src/processing/operational_semantics.py`
- Modify: `tests/processing/test_operational_semantics.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ServiceStateAudit:
    accepted_count: int
    rejected_count: int
    rejected_evidence_indexes: tuple[int, ...]
    rejection_reasons: tuple[str, ...]


def normalize_service_state_evidence(
    payload: EventPayload,
) -> tuple[EventPayload, ServiceStateAudit]: ...


def derive_operational_observations(
    payload: EventPayload,
) -> tuple[OperationalObservationPayload, ...]: ...
```

- Retires `normalize_operational_payload()` from v7/v6 processing. Remove it after all call sites migrate.

- [ ] **Step 1: Replace old tests with failing canonical-state tests**

In `tests/processing/test_operational_semantics.py`, add helpers:

```python
from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.processing.operational_semantics import (
    derive_operational_observations,
    normalize_service_state_evidence,
)


def _service_item(
    *,
    text: str,
    fid: int,
    subject_key: str,
    subject_label: str,
    state: str,
    expected_now: bool | None,
    basis: str,
):
    return EvidenceItemPayload(
        text=text,
        kind="service_access",
        publication_use="PUBLISH",
        source_fragment_ids=(fid,),
        service_state=ServiceStatePayload(
            subject_key=subject_key,
            subject_label=subject_label,
            dimension="availability",
            state=state,
            expected_now=expected_now,
            basis=basis,
        ),
    )
```

Add:

```python
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
```

- [ ] **Step 2: Add state/basis compatibility tests**

```python
@pytest.mark.parametrize(
    ("state", "basis", "expected_now"),
    [
        ("AVAILABLE", "normal_operation", True),
        ("UNAVAILABLE", "direct_failure", True),
        ("DEGRADED", "degraded_access", True),
        ("RESTRICTED", "explicit_restriction", True),
    ],
)
def test_valid_state_basis_pairs_survive(state, basis, expected_now):
    ...


@pytest.mark.parametrize(
    ("state", "basis"),
    [
        ("AVAILABLE", "direct_failure"),
        ("UNAVAILABLE", "normal_operation"),
        ("DEGRADED", "scheduled_change"),
    ],
)
def test_incompatible_state_basis_is_rejected(state, basis):
    ...
```

- [ ] **Step 3: Add high-confidence coping false-positive tests**

```python
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
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,))
    )

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
    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,))
    )

    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None
```

- [ ] **Step 4: Add generic subject-family conflict tests**

```python
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

    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,))
    )

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

    normalized, audit = normalize_service_state_evidence(
        EventPayload(evidence_items=(item,))
    )

    assert audit.rejected_count == 0
```

- [ ] **Step 5: Verify RED**

```bash
pytest tests/processing/test_operational_semantics.py -q --no-cov
```

Expected: FAIL because the new interfaces do not exist.

- [ ] **Step 6: Implement structural state/basis validation**

In `src/processing/operational_semantics.py` define:

```python
_ALLOWED_BASIS_BY_STATE: dict[str, frozenset[str]] = {
    "AVAILABLE": frozenset({"normal_operation"}),
    "UNAVAILABLE": frozenset({"direct_failure", "explicit_restriction"}),
    "DEGRADED": frozenset({"direct_failure", "degraded_access"}),
    "RESTRICTED": frozenset({"degraded_access", "explicit_restriction"}),
    "SCHEDULED": frozenset({"scheduled_change"}),
    "UNKNOWN": frozenset(
        {
            "normal_operation",
            "direct_failure",
            "degraded_access",
            "explicit_restriction",
            "scheduled_change",
        }
    ),
}
```

Validation rules:

```python
if state in {"UNAVAILABLE", "DEGRADED", "RESTRICTED"} and state.expected_now is not True:
    reject("negative_state_not_expected_now")

if state.state == "SCHEDULED" and state.effective_from is None:
    reject("scheduled_without_effective_from")

if state.basis not in _ALLOWED_BASIS_BY_STATE[state.state]:
    reject("state_basis_mismatch")
```

- [ ] **Step 7: Implement generic high-confidence coping detector**

Use normalized/stemmed token sets, not raw substring-only matching. Keep the detector intentionally narrow:

```python
_PRIVATE_ACTOR_STEMS = {...}      # resident, neighbor, household, private equivalents
_COPING_ACTION_STEMS = {...}      # use, buy, pool money, charge, stock, connect equivalents
_COPING_RESOURCE_STEMS = {...}    # generator, battery, powerbank, solar, private well equivalents
_SERVICE_OUTCOME_STEMS = {...}    # water, internet, bank, transport, lift, gas, heat, delivery, etc.
```

Define:

```python
def _is_high_confidence_private_coping(text: str) -> bool:
    tokens = _semantic_tokens(text)
    return (
        _has_any(tokens, _PRIVATE_ACTOR_STEMS)
        and _has_any(tokens, _COPING_ACTION_STEMS)
        and _has_any(tokens, _COPING_RESOURCE_STEMS)
        and not _has_any(tokens, _SERVICE_OUTCOME_STEMS)
    )
```

Do not include city names or provider names.

- [ ] **Step 8: Implement generic service-family conflict detection**

Create a small generic map:

```python
_SERVICE_FAMILY_STEMS: dict[str, frozenset[str]] = {
    "power": frozenset({...}),
    "water": frozenset({...}),
    "gas": frozenset({...}),
    "heating": frozenset({...}),
    "lift": frozenset({...}),
    "telecom": frozenset({...}),
    "banking": frozenset({...}),
    "transport": frozenset({...}),
    "logistics": frozenset({...}),
    "municipal": frozenset({...}),
}
```

Define:

```python
def _detect_service_families(text: str) -> frozenset[str]: ...
```

Reject only if:

```python
subject_families and evidence_families and subject_families.isdisjoint(evidence_families)
```

Do not “repair” to another subject automatically.

- [ ] **Step 9: Demote invalid service projection while preserving evidence**

For high-confidence coping false positive:

```python
replace(item, kind="community_report", service_state=None)
```

For structural/state/subject mismatch:

```python
replace(item, service_state=None)
```

Do not change `publication_use` or text.

- [ ] **Step 10: Implement deterministic observation derivation**

```python
def derive_operational_observations(
    payload: EventPayload,
) -> tuple[OperationalObservationPayload, ...]:
    observations: list[OperationalObservationPayload] = []
    for item in payload.evidence_items:
        state = item.service_state
        if (
            item.kind != "service_access"
            or item.publication_use != "PUBLISH"
            or state is None
        ):
            continue
        observations.append(
            OperationalObservationPayload(
                subject_key=state.subject_key,
                subject_label=state.subject_label,
                dimension=state.dimension,
                location=state.location,
                entity=state.entity,
                state=state.state,
                detail=item.text,
                source_fragment_ids=item.source_fragment_ids,
                effective_from=state.effective_from,
                effective_until=state.effective_until,
            )
        )
    return tuple(observations)
```

- [ ] **Step 11: Verify GREEN**

```bash
pytest tests/processing/test_operational_semantics.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add src/processing/operational_semantics.py tests/processing/test_operational_semantics.py
git commit -m "feat(events): derive operations from canonical service evidence"
```

---

### Task 3: Cut Gate v7 over to the unified service-state wire contract

**Files:**
- Modify: `src/processing/event_triage.py`
- Modify: `tests/processing/test_event_triage.py`

**Interfaces:**
- Consumes:
  - `normalize_service_state_evidence(payload)`
- Produces:

```python
TRIAGE_VERSION = "v7"

@dataclass(frozen=True)
class StoryGateResult:
    ...
    scope_basis_fragment_ids: tuple[int, ...]
```

- New Gate JSON has `evidence_items[*].service_state` and no `operational_observations` field.

- [ ] **Step 1: Add failing Gate v7 schema tests**

```python
def test_gate_v7_uses_unified_service_state_contract():
    assert TRIAGE_VERSION == "v7"
    prompt = _GATE_V2_SYSTEM_PROMPT

    assert '"service_state"' in prompt
    assert '"expected_now"' in prompt
    assert '"basis"' in prompt
    assert '"scope_basis_fragment_ids"' in prompt
    assert '"operational_observations"' not in prompt
```

- [ ] **Step 2: Add a fake Gate output regression for water auto-projection readiness**

The fake model returns:

```json
{
  "kind": "service_access",
  "publication_use": "PUBLISH",
  "text": "Water is absent on upper floors",
  "source_fragment_ids": [101],
  "service_state": {
    "subject_key": "water_supply",
    "subject_label": "Water supply",
    "dimension": "availability",
    "state": "UNAVAILABLE",
    "location": "upper floors",
    "entity": "",
    "expected_now": true,
    "basis": "direct_failure"
  }
}
```

Assert:

```python
assert result.brief_payload is not None
item = result.brief_payload.evidence_items[0]
assert item.service_state is not None
assert item.service_state.subject_key == "water_supply"
```

- [ ] **Step 3: Add a Gate coping false-positive regression**

Fake Gate returns `service_access.service_state` for household generator use.

Assert after normalization:

```python
item = result.brief_payload.evidence_items[0]
assert item.kind == "community_report"
assert item.service_state is None
assert result.retention == "KEEP"
```

- [ ] **Step 4: Verify RED**

```bash
pytest tests/processing/test_event_triage.py -q --no-cov
```

- [ ] **Step 5: Bump Gate version**

```python
TRIAGE_VERSION = "v7"
```

- [ ] **Step 6: Rewrite the Gate output contract**

Remove every instruction that requires or describes `operational_observations`.

Add these exact invariants:

```text
SERVICE-STATE CONTRACT:
- Operational service truth exists only inside a PUBLISH evidence item with kind=service_access and a non-null service_state object.
- Do not output an operational_observations array.
- service_state describes the external resident-facing service outcome, not the resident workaround mechanism.
- A generator, battery, private well, neighbor collection, charging action, VPN choice, fuel burden, or other coping action is not itself a resident-facing service state.
- If a workaround causes an explicitly stated service outcome, keep the coping action as separate evidence and attach service_state only to the evidence sentence that states the water/internet/banking/transport/etc. outcome.
- For UNAVAILABLE, DEGRADED, or RESTRICTED, expected_now MUST be true and the excerpts must establish that the service is expected to operate now or explicitly describe a current failure/restriction.
- Do not infer expected_now from the calendar or general season knowledge.
- For SCHEDULED, basis must be scheduled_change and effective_from is required.
- Valid basis values: normal_operation, direct_failure, degraded_access, explicit_restriction, scheduled_change.
```

- [ ] **Step 7: Replace the JSON example**

Use:

```json
"evidence_items": [
  {
    "text": "Fact or service access detail",
    "kind": "service_access",
    "publication_use": "PUBLISH",
    "source_fragment_ids": [101],
    "service_state": {
      "subject_key": "water_supply",
      "subject_label": "Водоснабжение",
      "dimension": "availability",
      "state": "UNAVAILABLE",
      "location": "",
      "entity": "",
      "expected_now": true,
      "basis": "direct_failure",
      "effective_from": null,
      "effective_until": null
    }
  }
]
```

Non-service evidence uses `"service_state": null` or omits the field.

- [ ] **Step 8: Add scope basis IDs to Gate result JSON**

Add:

```json
"scope_basis_fragment_ids": [101]
```

Prompt rule:

```text
LOCAL or DIRECT_IMPACT MUST cite one or more exact scope_basis_fragment_ids from the Story excerpts that establish the local occurrence or concrete local consequence.
```

- [ ] **Step 9: Parse and validate scope basis IDs**

Inside the per-Story result loop:

```python
raw_scope_basis = item.get("scope_basis_fragment_ids", [])
scope_basis_ids = tuple(
    int(x)
    for x in raw_scope_basis
    if isinstance(x, (int, str)) and str(x).isdigit()
)

if set(scope_basis_ids) - allowed_fids:
    deferred_ids.append(s.story_id)
    continue

if scope in {"LOCAL", "DIRECT_IMPACT"} and not scope_basis_ids:
    deferred_ids.append(s.story_id)
    continue
```

Add the field to `StoryGateResult` and cached-result reconstruction. Because no schema migration is being added, cached v7 results reconstruct the field as `()` only if scope is OUT_OF_SCOPE/UNCERTAIN; current v7 in-scope results must come from fresh processing.

- [ ] **Step 10: Replace operational normalization call**

Use:

```python
brief_payload = normalize_question_evidence(
    parse_event_payload(raw_brief, allowed_fragment_ids=allowed_fids)
)
brief_payload, service_audit = normalize_service_state_evidence(brief_payload)
```

Delete `normalize_operational_payload()` use from v7 processing.

- [ ] **Step 11: Make strict v7 service shape explicit after normalization**

Add a helper in `operational_semantics.py` or Gate-local validation:

```python
def has_unstructured_publish_service_access(payload: EventPayload) -> bool:
    return any(
        item.kind == "service_access"
        and item.publication_use == "PUBLISH"
        and item.service_state is None
        for item in payload.evidence_items
    )
```

Do **not** DROP the Story when this occurs. Log the semantic defect and preserve the evidence for thematic publication. City Situation simply receives no state from that item.

- [ ] **Step 12: Run Gate tests**

```bash
pytest tests/processing/test_event_triage.py tests/processing/test_operational_semantics.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add src/processing/event_triage.py tests/processing/test_event_triage.py
git commit -m "feat(gate): unify service state evidence in v7"
```

---

### Task 4: Add a narrow deterministic broad-region scope guard

**Files:**
- Modify: `src/processing/edition_scope.py`
- Modify: `src/processing/event_triage.py`
- Test: `tests/processing/test_edition_scope.py`
- Modify: `tests/processing/test_event_triage.py`

**Interfaces:**
- Produces:

```python
def broad_region_without_focus_impact(
    *,
    basis_texts: Sequence[str],
    scope: EditionScopeConfig,
    geo_context: EditionGeographyContext | None,
) -> bool: ...
```

- [ ] **Step 1: Write failing generic geography tests**

```python
from src.config_loader import EditionScopeConfig
from src.domain.edition_geography import EditionGeographyContext
from src.processing.edition_scope import broad_region_without_focus_impact


def test_regionwide_totals_without_focus_anchor_are_detected():
    scope = EditionScopeConfig(
        name="Example City",
        focus_places=("Example City", "Example District"),
        direct_impact_only=True,
        notes=(),
    )
    geo = EditionGeographyContext(
        edition_slug="example",
        edition_name="Example City",
        target_locations=("Example City",),
        district_locations=("Example District",),
        region_name="Example Region",
    )

    assert broad_region_without_focus_impact(
        basis_texts=("Across Example Region, 200 incidents were recorded",),
        scope=scope,
        geo_context=geo,
    )


def test_region_story_with_explicit_focus_consequence_is_not_detected():
    ...
    assert not broad_region_without_focus_impact(
        basis_texts=(
            "Across Example Region restrictions were introduced; in Example City buses stopped",
        ),
        scope=scope,
        geo_context=geo,
    )
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/processing/test_edition_scope.py -q --no-cov
```

- [ ] **Step 3: Implement normalized configured-place matching**

Use only configured names from `EditionScopeConfig` and `EditionGeographyContext`.

```python
def _norm_geo_text(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())
```

Build focus anchors from:

```python
scope.focus_places
+ geo_context.target_locations
+ geo_context.district_locations
```

Build region anchors from `geo_context.region_name` when present.

Return `True` only when:

```text
at least one basis fragment clearly references the broad region
AND no basis fragment contains a configured focus/district anchor
```

Do not use source membership.

- [ ] **Step 4: Wire the guard into Gate v7 after scope basis parsing**

Resolve `geo_context` once for the edition in `triage_stories_batch()` using the existing edition slug/name lookup pattern or an existing helper.

Construct:

```python
basis_texts = tuple(
    fragment_text_by_id[fid]
    for fid in scope_basis_ids
    if fid in fragment_text_by_id
)
```

If:

```python
scope in {"LOCAL", "DIRECT_IMPACT"}
and broad_region_without_focus_impact(...)
```

normalize:

```python
scope = "OUT_OF_SCOPE"
scope_confidence = max(scope_confidence, 0.95)
scope_reason = "Broad regional summary without explicit configured focus-area consequence"
```

The existing OUT_OF_SCOPE normalization then produces DROP+NONE.

- [ ] **Step 5: Add a Gate integration-style unit regression**

Fake LLM returns LOCAL for a broad regional incident-total fragment with that fragment in `scope_basis_fragment_ids`.

Assert final Gate result:

```python
assert result.scope == "OUT_OF_SCOPE"
assert result.retention == "DROP"
assert result.brief_payload is None
```

Then add the counter-case with an explicit focus-place consequence and assert it remains DIRECT_IMPACT/KEEP.

- [ ] **Step 6: Run scope tests**

```bash
pytest tests/processing/test_edition_scope.py tests/processing/test_event_triage.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/processing/edition_scope.py src/processing/event_triage.py tests/processing/test_edition_scope.py tests/processing/test_event_triage.py
git commit -m "fix(scope): reject broad regional summaries without local impact"
```

---

### Task 5: Cut Analysis v6 over to the same unified contract

**Files:**
- Modify: `src/processing/event_analysis.py`
- Modify: `tests/processing/test_event_analysis.py`

**Interfaces:**
- Consumes:
  - `normalize_service_state_evidence()`
- Produces:

```python
ANALYSIS_VERSION = "v6"
```

- Analysis output has `evidence_items[*].service_state` and no LLM-authored `operational_observations`.

- [ ] **Step 1: Write failing version/schema tests**

```python
def test_analysis_v6_uses_unified_service_state_contract():
    assert ANALYSIS_VERSION == "v6"
    prompt = _EVENT_ANALYSIS_SYSTEM_PROMPT
    assert '"service_state"' in prompt
    assert '"expected_now"' in prompt
    assert '"basis"' in prompt
    assert '"operational_observations"' not in prompt
```

- [ ] **Step 2: Add rich-analysis water service test**

Fake provider output includes valid water `service_access.service_state`.

After `analyze_story()` persistence, load the revision payload and assert:

```python
payload = EventAnalysisPayload.from_dict(revision.event_payload)
service_items = [
    item for item in payload.evidence_items
    if item.kind == "service_access"
]
assert service_items[0].service_state is not None
assert service_items[0].service_state.subject_key == "water_supply"
```

- [ ] **Step 3: Add rich-analysis coping demotion test**

Fake provider marks household generator use as service access.

Assert stored normalized evidence:

```python
assert payload.evidence_items[0].kind == "community_report"
assert payload.evidence_items[0].service_state is None
```

- [ ] **Step 4: Verify RED**

```bash
pytest tests/processing/test_event_analysis.py -q --no-cov
```

- [ ] **Step 5: Bump version and rewrite prompt**

```python
ANALYSIS_VERSION = "v6"
```

Use the same service-state rules as Gate v7. Do not maintain a divergent second definition.

Also add:

```text
Broad regional totals or incidents that do not state a concrete consequence inside the edition focus area must not be promoted into local key_facts, service_access, or digest_summary merely because they share the edition's region.
```

- [ ] **Step 6: Remove `operational_observations` from Analysis JSON example**

Move all operational fields under `evidence_items[*].service_state`.

- [ ] **Step 7: Normalize before hashing/persistence**

Use:

```python
parsed_payload = ensure_keep_publishability(
    normalize_question_evidence(EventAnalysisPayload.from_dict(parsed)),
    default="brief",
)
payload, service_audit = normalize_service_state_evidence(parsed_payload)
```

Compute `content_hash` from this normalized payload.

- [ ] **Step 8: Run Analysis tests**

```bash
pytest tests/processing/test_event_analysis.py tests/processing/test_operational_semantics.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/processing/event_analysis.py tests/processing/test_event_analysis.py
git commit -m "feat(analysis): use unified service state contract in v6"
```

---

### Task 6: Remove the old operational array from new canonical EventPayload serialization

**Files:**
- Modify: `src/domain/event_payload.py`
- Modify: `tests/domain/test_service_state.py`
- Modify: `tests/processing/test_event_triage.py`
- Modify: `tests/processing/test_event_analysis.py`

**Interfaces:**
- New v7/v6 canonical serialized EventPayload contains only `evidence_items[*].service_state` for operational semantics.
- `OperationalObservationPayload` remains importable for internal temporal resolution.

- [ ] **Step 1: Write failing serialization regression**

```python
def test_new_event_payload_serialization_has_no_parallel_operational_array():
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text="Water is unavailable",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(1,),
                service_state=ServiceStatePayload(
                    subject_key="water_supply",
                    subject_label="Water",
                    dimension="availability",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        )
    )

    data = payload.to_dict()
    assert "operational_observations" not in data
    assert data["evidence_items"][0]["service_state"]["state"] == "UNAVAILABLE"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/domain/test_service_state.py -q --no-cov
```

Expected: FAIL because current `EventPayload.to_dict()` serializes `operational_observations`.

- [ ] **Step 3: Remove `operational_observations` from the canonical EventPayload field set**

Preferred clean branch-level cutover:

```python
@dataclass(frozen=True)
class EventPayload:
    ...
    evidence_items: tuple[EvidenceItemPayload, ...] = ()
    ...
```

Remove the public `operational_observations` dataclass field from `EventPayload` and remove its serialization/parsing loop.

Keep `OperationalObservationPayload` defined in the module or move it to `src/domain/operational_state.py` only if doing so does not create circular imports. YAGNI: prefer leaving the internal type where it is for this plan.

- [ ] **Step 4: Remove question-normalizer observation cleanup**

`normalize_question_evidence()` should only canonicalize question evidence to CONTEXT; there is no parallel observation array left to filter.

Simplify to:

```python
def normalize_question_evidence(payload: EventPayload) -> EventPayload:
    canonical_items = tuple(
        replace(item, publication_use="CONTEXT", service_state=None)
        if item.kind == "resident_question"
        else item
        for item in payload.evidence_items
    )
    return replace(payload, evidence_items=canonical_items)
```

- [ ] **Step 5: Update Gate/Analysis fixtures that instantiate `EventPayload(operational_observations=...)`**

Replace each with PUBLISH `service_access` evidence carrying `service_state`.

Do not add a legacy conversion helper just to keep stale tests green.

- [ ] **Step 6: Run domain + processing tests**

```bash
pytest tests/domain/test_service_state.py tests/processing/test_event_triage.py tests/processing/test_event_analysis.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/domain/event_payload.py tests/domain/test_service_state.py tests/processing/test_event_triage.py tests/processing/test_event_analysis.py
git commit -m "refactor(events): remove parallel operational payload truth"
```

---

### Task 7: Derive City Situation directly from canonical service evidence in `EventEditorialAdapter`

**Files:**
- Modify: `src/publication/event_editorial_adapter.py`
- Test: `tests/publication/test_event_editorial_adapter.py` or nearest existing adapter unit module
- Modify: `tests/integration/test_city_situation_golden.py`

**Interfaces:**
- Consumes:

```python
derive_operational_observations(payload)
```

- Produces the same existing input type expected by:

```python
resolve_operational_states(
    observations_with_time: Sequence[
        tuple[OperationalObservationPayload, datetime, Sequence[str]]
    ]
)
```

- [ ] **Step 1: Add an adapter-level water auto-projection test**

Build a stored EventPayload containing only:

```text
PUBLISH service_access
+ valid water service_state
+ source fragment IDs
```

Do not include any old operational array.

After adapter execution assert:

```python
rollup = frozen.analysis.city_situation
assert rollup is not None
water = next(item for item in rollup.items if item.subject_key == "water_supply")
assert water.state == "UNAVAILABLE"
```

This test proves “water cannot be forgotten after valid service-state extraction.”

- [ ] **Step 2: Add a coping exclusion adapter test**

Stored normalized payload contains:

```text
PUBLISH community_report: residents use generator
service_state = None
```

Assert:

```python
assert all(
    item.subject_key != "backup_power"
    for item in frozen.analysis.city_situation.items
)
assert any(
    "generator" in element.text.casefold()
    for card in frozen.analysis.cards
    for element in card.community_observations
)
```

Coping remains in thematic StoryCard evidence.

- [ ] **Step 3: Verify RED**

```bash
pytest tests/publication/test_event_editorial_adapter.py -q --no-cov
```

If the repository has no such file, create it under `tests/publication/` and run that path.

- [ ] **Step 4: Derive observations at adapter read time**

Import:

```python
from src.processing.operational_semantics import derive_operational_observations
```

Replace:

```python
if payload and payload.operational_observations:
    for obs in payload.operational_observations:
```

with:

```python
derived_observations = (
    derive_operational_observations(payload)
    if payload is not None
    else ()
)

for obs in derived_observations:
    ...
```

Keep existing fragment-ID -> source-ref/timestamp mapping.

- [ ] **Step 5: Update pure-operational Story detection**

Replace array-presence checks with:

```python
has_service_states = bool(derived_observations)
```

And define non-operational PUBLISH evidence as:

```python
has_non_op_evidence = any(
    item.publication_use == "PUBLISH"
    and not (
        item.kind == "service_access"
        and item.service_state is not None
    )
    for item in payload.evidence_items
)
```

Do not refer to the nonexistent `utility_status` evidence kind.

- [ ] **Step 6: Keep service evidence in StoryCard useful details**

The existing `_evidence_story_element(kind="service_access") -> useful_details` behavior remains.

This is important: a Story can be represented in City Situation and still provide DRILL_DOWN details below.

- [ ] **Step 7: Keep temporal resolver unchanged**

Do not rewrite `src/domain/operational_state.py` in this task. Feed it the newly derived observations and preserve its conflict/effective-time semantics.

- [ ] **Step 8: Run adapter and City Situation tests**

```bash
pytest tests/publication/test_event_editorial_adapter.py tests/integration/test_city_situation_golden.py -q --no-cov -n 0
```

Expected: PASS after Task 9 updates the old golden fixture contract if needed. During this task, focused adapter unit tests must pass.

- [ ] **Step 9: Commit**

```bash
git add src/publication/event_editorial_adapter.py tests/publication/test_event_editorial_adapter.py
git commit -m "feat(publication): derive city situation from service evidence"
```

---

### Task 8: Update recent subject hints to read canonical service-state evidence

**Files:**
- Modify: `src/processing/event_triage.py`
- Modify: `tests/processing/test_event_triage.py`

**Interfaces:**
- `_load_recent_subject_hints()` continues returning:

```python
list[tuple[str, str]]
```

but reads `brief_payload.evidence_items[*].service_state` instead of `brief_payload.operational_observations`.

- [ ] **Step 1: Add a failing DB/query regression**

Insert a v7 triage decision whose JSON contains:

```json
{
  "evidence_items": [
    {
      "kind": "service_access",
      "publication_use": "PUBLISH",
      "service_state": {
        "subject_key": "water_supply",
        "subject_label": "Water supply"
      }
    }
  ]
}
```

Assert:

```python
hints = await service._load_recent_subject_hints(conn, edition.id)
assert ("water_supply", "Water supply") in hints
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/processing/test_event_triage.py -q --no-cov
```

- [ ] **Step 3: Replace JSONB query**

Use nested expansion:

```sql
SELECT DISTINCT
    evi->'service_state'->>'subject_key' AS subject_key,
    evi->'service_state'->>'subject_label' AS subject_label
FROM story_event_triage_decisions setd
JOIN story_edition_scope_decisions sesd
  ON ...
CROSS JOIN LATERAL jsonb_array_elements(
    COALESCE(setd.brief_payload->'evidence_items', '[]'::jsonb)
) AS evi
WHERE sesd.edition_id = %s
  AND sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')
  AND setd.retention = 'KEEP'
  AND setd.triage_version = %s
  AND evi->>'kind' = 'service_access'
  AND evi->>'publication_use' = 'PUBLISH'
  AND evi->'service_state' IS NOT NULL
  AND evi->'service_state'->>'subject_key' IS NOT NULL
ORDER BY subject_key
LIMIT %s
```

No migration is required because `brief_payload` is already JSONB.

- [ ] **Step 4: Run Gate tests**

```bash
pytest tests/processing/test_event_triage.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/processing/event_triage.py tests/processing/test_event_triage.py
git commit -m "fix(gate): read subject hints from canonical service states"
```

---

### Task 9: Rewrite the City Situation golden oracle around canonical service-state evidence

**Files:**
- Create: `tests/fixtures/unified_service_state_golden.json`
- Modify: `tests/fixtures/berdyansk_city_situation_golden.json`
- Modify: `tests/integration/test_city_situation_golden.py`
- Modify: `tests/integration/test_publication_quality_golden.py`

**Interfaces:**
- Fixture no longer contains `expected_observations` as an independent semantic truth.
- Each KEEP case carries expected evidence items including nested `service_state` when operational.

- [ ] **Step 1: Create a generic semantic golden fixture**

`tests/fixtures/unified_service_state_golden.json` must include these cases:

```json
{
  "cases": [
    {
      "id": "current_power_outage",
      "expected_dashboard_subject": "power_supply"
    },
    {
      "id": "upper_floor_water_outage_from_community",
      "expected_dashboard_subject": "water_supply"
    },
    {
      "id": "household_generator_coping",
      "expected_dashboard_subject": null,
      "expected_thematic": true
    },
    {
      "id": "future_heating_concern_without_current_expectation",
      "expected_dashboard_subject": null,
      "expected_thematic": true
    },
    {
      "id": "provider_connectivity_not_city_power",
      "expected_dashboard_subject": "internet_connectivity"
    },
    {
      "id": "regionwide_incident_totals_without_local_impact",
      "expected_scope": "OUT_OF_SCOPE"
    }
  ]
}
```

The concrete fragment texts must be generic and must exercise the actual parser/normalizer. Do not place these fixture examples in production prompts.

- [ ] **Step 2: Rewrite Berdyansk City Situation mock Gate output construction**

Current `test_city_situation_golden.py` constructs `operational_observations` from fixture expectations. Replace it so each expected operational case creates:

```json
{
  "text": "...",
  "kind": "service_access",
  "publication_use": "PUBLISH",
  "source_fragment_ids": [...],
  "service_state": {
    "subject_key": "...",
    "subject_label": "...",
    "dimension": "availability",
    "state": "...",
    "location": "...",
    "entity": "...",
    "expected_now": true,
    "basis": "direct_failure"
  }
}
```

Use the correct basis/state pair for available/restricted/scheduled cases.

- [ ] **Step 3: Add required `scope_basis_fragment_ids` to mocked Gate outputs**

For every LOCAL/DIRECT_IMPACT case, map the fixture's designated local-basis fragment IDs to DB fragment IDs and include them in the mock result.

- [ ] **Step 4: Assert no parallel operational array is persisted**

After brief persistence:

```python
assert "operational_observations" not in revision.event_payload
```

and:

```python
assert any(
    item.get("service_state")
    for item in revision.event_payload.get("evidence_items", [])
)
```

for operational cases.

- [ ] **Step 5: Preserve temporal water timeline coverage**

Represent morning unavailable, afternoon available, evening unavailable as three separate `service_access` evidence items / Story revisions or fixture fragments according to the existing test structure, each with its own service state and provenance.

Final assertion remains:

```python
assert w_item.state == "UNAVAILABLE"
assert w_item.observation_count == 3
```

This proves the cutover does not break temporal resolution.

- [ ] **Step 6: Add explicit regressions for the recent live failures**

Assert:

```python
subjects = {item.subject_key for item in editorial_input.analysis.city_situation.items}
assert "water_supply" in subjects
assert "backup_power" not in subjects
```

Add a normalized heating concern fixture where `expected_now` is absent/false and assert heating does not appear in City Situation.

- [ ] **Step 7: Run the integration oracles**

```bash
TELEBRIEF_TEST_DATABASE_URL="postgresql://telebrief:telebrief@localhost:5432/telebrief_test" \
pytest tests/integration/test_city_situation_golden.py tests/integration/test_publication_quality_golden.py -q --no-cov -n 0
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/fixtures/unified_service_state_golden.json tests/fixtures/berdyansk_city_situation_golden.json tests/integration/test_city_situation_golden.py tests/integration/test_publication_quality_golden.py
git commit -m "test(events): move city situation golden to unified service states"
```

---

### Task 10: Remove duplicate scan-first digest lead

**Files:**
- Modify: `src/publication/renderers.py`
- Modify: `tests/publication/test_renderers.py`
- Modify: `tests/integration/test_city_situation_golden.py`
- Modify: `tests/publication/test_generation.py`

**Interfaces:**
- `PublicationDigestRenderer.render_grouped_digest()` continues returning `(title, lead, body)` for compatibility.
- For grouped/channel digest paths, `lead == ""`.
- Article lead generation is untouched.

- [ ] **Step 1: Write failing renderer test**

```python
def test_grouped_digest_has_empty_publication_lead():
    renderer = PublicationDigestRenderer()
    title, lead, body = renderer.render_grouped_digest(
        frozen_input,
        edition_name="Example City",
        snapshot_at=SNAPSHOT,
    )

    assert title.startswith("Дайджест")
    assert lead == ""
    assert "Городская обстановка" in body
```

- [ ] **Step 2: Add channel compatibility test**

```python
def test_channel_digest_also_does_not_emit_external_lead():
    _, lead, _ = renderer.render_channel_digest(...)
    assert lead == ""
```

- [ ] **Step 3: Verify RED**

```bash
pytest tests/publication/test_renderers.py -q --no-cov
```

Expected: FAIL because current renderer returns `cards[0].summary`.

- [ ] **Step 4: Make the minimal renderer change**

Replace:

```python
lead = cards[0].summary if cards else ""
```

with:

```python
lead = ""
```

Do not alter the body ordering or narrative draft rendering.

- [ ] **Step 5: Assert publication persistence also stores empty digest lead**

In `tests/publication/test_generation.py`, run a grouped Event-First digest generation and assert:

```python
assert publication.lead == ""
```

This verifies downstream surfaces cannot reconstruct the duplicate blockquote from the stored digest lead.

- [ ] **Step 6: Run renderer/generation tests**

```bash
pytest tests/publication/test_renderers.py tests/publication/test_generation.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/publication/renderers.py tests/publication/test_renderers.py tests/publication/test_generation.py tests/integration/test_city_situation_golden.py
git commit -m "fix(digest): remove redundant scan-first lead"
```

---

### Task 11: Update benchmark observability for unified service-state semantics

**Files:**
- Modify: `scripts/benchmark_publication_quality.py`
- Modify: `tests/scripts/test_benchmark_publication_quality.py`

**Interfaces:**
- Adds diagnostic metrics; does not alter publication eligibility.

New metrics:

```text
service_state_evidence_count
service_state_projection_count
service_state_rejected_count
city_situation_subject_count
legacy_operational_array_count
regional_scope_guard_drop_count
```

- [ ] **Step 1: Add failing benchmark metric assertions**

```python
assert "service_state_evidence_count" in metrics
assert "service_state_projection_count" in metrics
assert "legacy_operational_array_count" in metrics
assert "regional_scope_guard_drop_count" in metrics
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -q --no-cov
```

- [ ] **Step 3: Count canonical service states from EventPayload evidence**

Use only:

```python
item.kind == "service_access"
and item.publication_use == "PUBLISH"
and item.service_state is not None
```

- [ ] **Step 4: Count derived projection parity**

For normalized v7/v6 payloads assert diagnostically:

```text
service_state_projection_count == service_state_evidence_count
```

Do not make the script crash on stale old revisions; instead report them under `legacy_operational_array_count` and label the run semantically stale.

- [ ] **Step 5: Make new-run golden expectation strict**

For v7/v6 benchmark fixtures:

```python
assert metrics["legacy_operational_array_count"] == 0
```

- [ ] **Step 6: Run benchmark tests**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/benchmark_publication_quality.py tests/scripts/test_benchmark_publication_quality.py
git commit -m "feat(bench): audit unified service state projections"
```

---

### Task 12: Update `AGENTS.md` and perform fresh-semantic acceptance verification

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Durable product/engineering contract for future agents.

- [ ] **Step 1: Replace the old operational-array guidance**

Add this canonical rule:

```text
Unified service-state truth:
- For Gate v7 / Analysis v6 and later, the only LLM-authored operational truth is `service_access.service_state` inside PUBLISH evidence.
- Do not reintroduce an independent LLM-authored `operational_observations` array.
- `OperationalObservationPayload` is an internal derived temporal-resolution representation only.
- Every validated `service_access.service_state` is deterministically projected to City Situation processing.
```

- [ ] **Step 2: Add the expectation/coping rule**

```text
- `expected_now` never defaults to true.
- UNAVAILABLE/DEGRADED/RESTRICTED require explicit current expectation/failure semantics.
- Household coping is thematic evidence, not a service state by itself.
- Do not keyword-ban generators/batteries/etc.; a workaround may coexist with a real explicitly supported water/internet/etc. service outcome.
```

- [ ] **Step 3: Add subject-alignment rule**

```text
- Deterministic subject-family checks may reject a high-confidence mismatch but must not invent or auto-repair a new subject.
- Never hardcode provider names such as a specific ISP into generic production semantics.
```

- [ ] **Step 4: Add scope rule**

```text
- LOCAL/DIRECT_IMPACT Gate decisions require exact scope-basis fragment IDs.
- Broad regional totals without configured focus-area anchor or explicit focus-area consequence are OUT_OF_SCOPE.
- Same oblast/region is never sufficient by itself.
```

- [ ] **Step 5: Add digest lead rule**

```text
- Scan-first grouped/channel digest publication lead is empty. Reader order is Title -> City Situation -> thematic blocks -> statistics.
- Do not add a duplicate blockquote summary above City Situation.
```

- [ ] **Step 6: Add no-migration note for this cutover**

Document historically:

```text
This v7/v6 cutover occurred before production deployment of the branch. It intentionally used a clean semantic contract change and fresh regeneration rather than a DB migration/backfill of the old operational array.
```

- [ ] **Step 7: Run focused domain/processing tests**

```bash
pytest \
  tests/domain/test_service_state.py \
  tests/processing/test_operational_semantics.py \
  tests/processing/test_edition_scope.py \
  tests/processing/test_event_triage.py \
  tests/processing/test_event_analysis.py \
  -q --no-cov
```

Expected: PASS.

- [ ] **Step 8: Run publication tests**

```bash
pytest \
  tests/publication/test_event_editorial_adapter.py \
  tests/publication/test_digest_presentation.py \
  tests/publication/test_renderers.py \
  tests/publication/test_generation.py \
  -q --no-cov
```

Expected: PASS. If the adapter tests live in a differently named existing module, use that actual path and update this plan during implementation rather than duplicating test modules.

- [ ] **Step 9: Run Postgres integration oracles**

```bash
TELEBRIEF_TEST_DATABASE_URL="postgresql://telebrief:telebrief@localhost:5432/telebrief_test" \
pytest \
  tests/integration/test_city_situation_golden.py \
  tests/integration/test_publication_quality_golden.py \
  -q --no-cov -n 0
```

Expected: PASS.

- [ ] **Step 10: Run benchmark tests**

```bash
pytest tests/scripts/test_benchmark_publication_quality.py -q --no-cov
```

Expected: PASS.

- [ ] **Step 11: Run the current non-legacy suite**

```bash
TELEBRIEF_TEST_DATABASE_URL="postgresql://telebrief:telebrief@localhost:5432/telebrief_test" \
pytest tests/domain tests/processing tests/publication tests/config tests/integration tests/scripts \
  -q --no-cov -n 0
```

Expected: PASS.

- [ ] **Step 12: Run canonical lint/type commands from the repository**

Read the current `Makefile`/`AGENTS.md` and execute the exact configured lint/type targets. Do not invent a new toolchain.

Expected: PASS.

- [ ] **Step 13: Regenerate fresh v7/v6 semantics before judging live quality**

Because old local EventPayload revisions use the retired dual-array contract, do not evaluate them as v7/v6 output.

Use the existing Event-First processing/backfill mechanism to re-run current Stories so current revisions are produced under:

```text
TRIAGE_VERSION = v7
ANALYSIS_VERSION = v6
```

Verify there is no reliance on old `operational_observations` JSON in the selected revisions.

- [ ] **Step 14: Run the real current digest path**

For a current smoke comparison, use the existing script:

```bash
python scripts/compare_digest_approaches.py
```

Treat it as a live-output smoke test, **not** as a same-frozen-input causal A/B benchmark.

- [ ] **Step 15: Run the existing publication benchmark**

```bash
python scripts/benchmark_publication_quality.py --hours 24 --edition berdyansk
```

Verify the run is based on fresh v7/v6 semantic payloads before interpreting the metrics.

- [ ] **Step 16: Manual acceptance checklist on the generated digest**

Check:

```text
[ ] No duplicate blockquote lead above City Situation.
[ ] Water appears in City Situation when a valid water service_state exists.
[ ] Household/private generator use does not appear as a service subject by itself.
[ ] Generator-powered water/internet can still create the supported water/internet service outcome.
[ ] Heating/future-cold concern does not become UNAVAILABLE without expected_now=true and valid current-failure basis.
[ ] Provider connectivity evidence is not mislabeled as city power supply.
[ ] Region-wide military/incident totals without explicit local impact are absent.
[ ] Thematic digest still contains useful coping/adaptation microdetails.
[ ] City Situation remains scan-first and subject-coherent.
[ ] No extra generative LLM call was introduced.
```

- [ ] **Step 17: Commit documentation**

```bash
git add AGENTS.md docs/superpowers/specs/2026-08-31-unified-service-state-truth-operational-semantics-design.md docs/superpowers/plans/2026-08-31-unified-service-state-truth-operational-semantics.md
git commit -m "docs: codify unified service state truth"
```

---

## Self-review

### 1. Spec coverage

- Single canonical operational source: Tasks 1, 2, 3, 5, 6.
- No dual LLM arrays: Tasks 3, 5, 6.
- Direct service-state -> City Situation projection: Tasks 2, 7, 9.
- Coping false positives without keyword overreach: Task 2.
- Seasonal/current expectation: Tasks 1, 2, 3, 5, 9.
- Subject alignment without provider hardcoding: Task 2.
- Temporal behavior retained: Tasks 7, 9.
- Regional scope tightening: Tasks 3, 4, 5, 9.
- Digest lead removal: Task 10.
- No migrations/backfills: Global Constraints + Tasks 6 and 12.
- Fresh semantic regeneration: Task 12.
- Documentation contract: Task 12.

### 2. Placeholder scan

The plan contains no unfinished implementation markers or unspecified placeholder steps.

### 3. Type consistency

Canonical names used throughout:

```text
ServiceStatePayload
ServiceStateBasis
ServiceStateAudit
normalize_service_state_evidence
derive_operational_observations
scope_basis_fragment_ids
broad_region_without_focus_impact
TRIAGE_VERSION = "v7"
ANALYSIS_VERSION = "v6"
```

### 4. Architecture sanity check

The final data flow must be:

```text
source fragments
    ↓
Gate v7 / Analysis v6
    ↓
EvidenceItemPayload
    ├── normal evidence
    └── service_access + ServiceStatePayload   ← one semantic truth
                   ↓
        deterministic normalization
                   ↓
        derived OperationalObservationPayload  ← internal only
                   ↓
        existing temporal resolver
                   ↓
        CitySituationRollup
                   ↓
        existing DigestPresentationPlan
                   ↓
Title -> City Situation -> thematic city-life short read -> statistics
```

There must be no second LLM-authored operational array anywhere in the v7/v6 path.
