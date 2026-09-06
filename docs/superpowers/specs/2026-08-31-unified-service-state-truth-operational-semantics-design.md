# Unified Service-State Truth & Operational Semantics Design

**Status:** Approved direction, refined against the current `multisource-roadmap` implementation.

**Baseline:** `multisource-roadmap` at commit `b339c5c74b2d7e313dd21e245ab3c267fd1849dc` or later.

**Production status:** This branch has not been deployed to production. No database migration or compatibility migration is required for this design. Existing local/test Event-First semantic payloads may be regenerated after the Gate/Analysis version bump.

---

## 1. Problem

The current Event-First payload carries two independently generated semantic structures:

```text
EvidenceItemPayload(kind="service_access", ...)
+
OperationalObservationPayload(...)
```

The current deterministic normalizer only verifies overlapping fragment IDs. That prevents some unsupported projections, but it does not prove that:

- the `service_access` classification itself is correct;
- the operational subject matches the evidence meaning;
- a household coping action is really an external resident-facing service;
- a negative state is expected to be operational now;
- every real service-access fact receives an operational projection.

This permits two symmetric failures:

```text
coping report -> LLM marks service_access -> LLM emits operational observation
              -> same fragment IDs -> accepted

real water outage -> LLM emits service_access but forgets operational observation
                  -> dashboard loses water
```

The solution is one canonical semantic object, not another prompt patch.

---

## 2. Canonical model

For Gate v7 / Analysis v6, the only authoritative source of operational service truth is a structured `service_state` embedded in a PUBLISH `service_access` evidence item.

```python
@dataclass(frozen=True)
class ServiceStatePayload:
    subject_key: str
    subject_label: str
    dimension: str
    state: Literal[
        "AVAILABLE",
        "UNAVAILABLE",
        "DEGRADED",
        "RESTRICTED",
        "UNKNOWN",
        "SCHEDULED",
    ]
    location: str = ""
    entity: str = ""
    expected_now: bool | None = None
    basis: Literal[
        "normal_operation",
        "direct_failure",
        "degraded_access",
        "explicit_restriction",
        "scheduled_change",
    ] = "normal_operation"
    effective_from: str | None = None
    effective_until: str | None = None


@dataclass(frozen=True)
class EvidenceItemPayload:
    text: str
    kind: EvidenceKind
    publication_use: PublicationUse
    source_fragment_ids: tuple[int, ...]
    service_state: ServiceStatePayload | None = None
```

`EvidenceItemPayload.text` remains the factual human-readable evidence text. `service_state` contains only the structured operational projection of that same evidence. There is no duplicated free-text `detail` field inside `ServiceStatePayload`; derived operational observations use `EvidenceItemPayload.text` as their detail.

### Why `expected_now` is nullable

It must **not** default to `True`. A missing value cannot silently promote a seasonal or future concern to a current outage.

Rules:

```text
AVAILABLE
  basis = normal_operation
  expected_now = True or None

UNAVAILABLE / DEGRADED / RESTRICTED
  expected_now MUST be True
  basis must describe current failure/access/restriction

SCHEDULED
  basis = scheduled_change
  effective_from REQUIRED
  expected_now = False or None

UNKNOWN
  may be retained as uncertain service evidence,
  but should not be used to claim a concrete failure mechanism
```

---

## 3. New wire contract: no LLM-generated `operational_observations`

Gate v7 and Analysis v6 must stop asking the model to return an independent `operational_observations` array.

New payload shape:

```json
{
  "evidence_items": [
    {
      "text": "Water does not reach upper floors",
      "kind": "service_access",
      "publication_use": "PUBLISH",
      "source_fragment_ids": [101],
      "service_state": {
        "subject_key": "water_supply",
        "subject_label": "Водоснабжение",
        "dimension": "availability",
        "state": "UNAVAILABLE",
        "location": "верхние этажи",
        "entity": "",
        "expected_now": true,
        "basis": "direct_failure"
      }
    }
  ]
}
```

A coping item stays ordinary evidence:

```json
{
  "text": "Residents pooled money for a generator to run the pump",
  "kind": "community_report",
  "publication_use": "PUBLISH",
  "source_fragment_ids": [102],
  "service_state": null
}
```

The old `OperationalObservationPayload` type may remain as an **internal derived temporal-resolution type** because the existing resolver is good and already handles timestamps/conflicts. It is no longer an LLM output contract or a persisted second semantic truth.

---

## 4. Deterministic validation

Create a strict normalization boundary:

```python
@dataclass(frozen=True)
class ServiceStateAudit:
    accepted_count: int
    rejected_count: int
    rejected_evidence_indexes: tuple[int, ...]
    rejection_reasons: tuple[str, ...]


def normalize_service_state_evidence(
    payload: EventPayload,
) -> tuple[EventPayload, ServiceStateAudit]:
    ...
```

### 4.1 Structural invariants

For new v7/v6 payloads:

- `kind == "service_access"` + `publication_use == "PUBLISH"` requires a `service_state`.
- non-`service_access` items must not carry `service_state`.
- `subject_key`, `subject_label`, `dimension`, and `state` are required when `service_state` exists.
- negative current states require `expected_now is True`.
- `SCHEDULED` requires `basis == "scheduled_change"` and `effective_from`.
- effective windows must parse and be chronologically valid.

### 4.2 Basis/state compatibility

Allowed combinations:

```text
normal_operation       -> AVAILABLE

direct_failure         -> UNAVAILABLE | DEGRADED

degraded_access        -> DEGRADED | RESTRICTED

explicit_restriction   -> RESTRICTED | UNAVAILABLE

scheduled_change       -> SCHEDULED
```

Reject incompatible combinations from operational projection.

### 4.3 High-confidence coping defense

Do **not** hard-block individual words such as `generator`, `battery`, `solar`, or `well`. Those words can appear in legitimate service-outcome evidence.

Instead use a narrow generic high-confidence detector that requires the shape:

```text
private/resident actor
+
private coping action/resource
+
no explicit external service outcome
```

Examples to demote from service state:

```text
Residents use generators at night.
Neighbors bought a powerbank.
Residents stock fuel for generators.
```

Examples that remain operational when the evidence explicitly states the external service outcome:

```text
The building generator powers the water pump; water is available daily.
Provider equipment is powered by a generator; internet service remains available.
```

When a high-confidence coping false positive is detected:

- remove `service_state`;
- demote `kind` from `service_access` to `community_report`;
- preserve `publication_use`, text, and provenance;
- never drop the Story solely for this correction.

Use existing generic normalization/stemming helpers where available. No city/provider-specific aliases.

### 4.4 Subject alignment

Use a narrow generic conflict detector, not a fixed city taxonomy.

Detect broad service families from:

- `subject_key + subject_label`;
- evidence text.

Generic families may include power, water, gas, heating, lift, telecom/internet, banking/payments, transport, logistics/delivery, municipal services.

Rule:

```text
if both sides have a confident recognized family
and the families are disjoint
-> reject the service_state projection
```

If evidence is ambiguous or no family is confidently recognized, do not invent a correction.

This specifically prevents a provider-equipment statement from becoming `power_supply` merely because the equipment itself has no electricity. If the evidence explicitly says internet/connectivity is unavailable, the telecom service state is valid.

---

## 5. Direct derived projection

Create:

```python
def derive_operational_observations(
    payload: EventPayload,
) -> tuple[OperationalObservationPayload, ...]:
    ...
```

It deterministically maps every validated PUBLISH `service_access.service_state` to one `OperationalObservationPayload`:

```text
subject fields <- service_state
state          <- service_state.state
detail         <- evidence.text
fragment IDs   <- evidence.source_fragment_ids
effective time <- service_state effective fields
```

Therefore:

```text
valid water service_state
-> always reaches temporal resolver

no valid service_state
-> cannot reach City Situation
```

There is no separate projection completeness problem anymore.

---

## 6. EventEditorialAdapter

The adapter must derive operational observations from canonical evidence rather than reading `payload.operational_observations`.

For each derived observation:

- map fragment IDs to frozen source refs;
- map each fragment to its observed timestamp;
- feed the existing `resolve_operational_states()` unchanged;
- feed the resulting states to existing `build_city_situation_rollup()` unchanged;
- continue exposing `service_access` evidence as useful detail in StoryCards.

Pure-operational Story classification must be based on validated `service_access.service_state`, not the presence of the legacy array.

The existing temporal resolver and CitySituationRollup are retained because they already solve a different problem: resolving repeated service-state observations over time.

---

## 7. Gate v7

Bump:

```text
TRIAGE_VERSION = "v7"
```

The Gate prompt must:

- remove `operational_observations` from the JSON schema;
- require `service_state` only on PUBLISH `service_access` evidence;
- explicitly separate external service outcome from resident coping;
- require `expected_now`/`basis` semantics;
- require exact source fragment provenance for the evidence item.

The Gate parser must run:

```text
parse_event_payload
-> normalize_question_evidence
-> normalize_service_state_evidence
-> ensure_keep_publishability
```

A rejected service state does not automatically DROP the Story.

---

## 8. Analysis v6

Bump:

```text
ANALYSIS_VERSION = "v6"
```

Use the same canonical service-state schema and the same deterministic normalizer as Gate v7.

Rich analysis may refine evidence wording and service metadata, but it must not reintroduce an independent operational array.

---

## 9. Geographic scope hardening

The current scope contract already says same-region relevance is insufficient. The new design adds a verifiable basis trace so this is testable rather than prompt-only.

Gate v7 result adds:

```json
"scope_basis_fragment_ids": [101]
```

Rules:

- LOCAL or DIRECT_IMPACT requires at least one allowed basis fragment.
- OUT_OF_SCOPE/UNCERTAIN may have an empty basis list.
- basis IDs must belong to the Story excerpts.

Add a narrow deterministic guard for **broad regional summaries**:

```text
if candidate scope is LOCAL or DIRECT_IMPACT
and all cited scope-basis fragments are broad-region summaries
and none contains a configured focus-place/district anchor
or an explicit concrete consequence inside the focus area
-> normalize to OUT_OF_SCOPE
```

The guard uses edition configuration/geography context only. No Berdyansk-specific production code.

Do not turn this into a universal lexical locality resolver. Its purpose is specifically to stop region-wide military/infrastructure totals from entering a local digest without an explicit local consequence.

Analysis v6 must also be instructed not to promote broad regional totals into local event evidence after a Story has passed Gate.

No database schema migration is required. `scope_basis_fragment_ids` is a Gate-response validation/audit field; persistence may continue using the existing scope class/confidence/reason columns. Include the fragment IDs in structured debug/audit metadata where the current run metadata supports it, but do not add a migration solely for this feature.

---

## 10. Digest lead

`PublicationDigestRenderer.render_grouped_digest()` currently returns:

```python
lead = cards[0].summary if cards else ""
```

For scan-first grouped/channel digests this creates a separate publication lead that can be rendered as an extra summary outside the body by downstream surfaces.

Change grouped digest behavior to:

```python
lead = ""
```

Expected digest order:

```text
Title
-> City Situation
-> thematic rubric blocks
-> statistics
```

Articles keep their independent article lead behavior.

---

## 11. No migration / freshness policy

Because the branch has not shipped:

- do not create a database migration;
- do not backfill old local EventPayload JSON;
- do not preserve the old LLM operational array as a second authoritative path;
- bump Gate/Analysis versions and regenerate semantic revisions for clean tests/live acceptance.

Old local revisions that only contain `operational_observations` are stale semantic artifacts. They may still parse if convenient, but they are not valid evidence for evaluating v7/v6 City Situation quality.

---

## 12. Acceptance criteria

1. Gate v7 / Analysis v6 LLM output has no independent `operational_observations` array.
2. Every City Situation observation derives from a validated `service_access.service_state` evidence item.
3. Every validated service-state evidence item is automatically projected to the temporal resolver.
4. Household coping without an explicit external service outcome cannot become City Situation.
5. A generator mentioned as the mechanism for a real water/internet outcome does not cause the real service state to be discarded.
6. Negative current states cannot default `expected_now=True`.
7. Heating absence/future concern cannot become a current outage without valid expected-now/basis semantics.
8. High-confidence subject-family conflict removes the invalid operational projection without deleting useful evidence.
9. Water/service-access evidence cannot be silently omitted from City Situation after validation.
10. Regional aggregate summaries without explicit focus-area consequence are OUT_OF_SCOPE.
11. Scan-first grouped digest returns an empty publication lead.
12. Existing temporal conflict resolution and subject-coherent presentation remain intact.
13. No additional generative LLM call is introduced.
14. No city/provider-specific operational aliases are hardcoded in generic production code.
15. No database migration is added for this branch-only contract change.
