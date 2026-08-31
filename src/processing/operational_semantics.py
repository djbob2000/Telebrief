from __future__ import annotations

from dataclasses import dataclass, replace

from src.domain.event_payload import EventPayload


@dataclass(frozen=True)
class OperationalPayloadAudit:
    dropped_observation_count: int = 0
    dropped_observation_subject_keys: tuple[str, ...] = ()
    uncovered_service_access_fragment_ids: tuple[int, ...] = ()


def normalize_operational_payload(
    payload: EventPayload,
) -> tuple[EventPayload, OperationalPayloadAudit]:
    if not payload.evidence_items:
        return payload, OperationalPayloadAudit()

    service_items = tuple(
        item
        for item in payload.evidence_items
        if item.publication_use == "PUBLISH" and item.kind == "service_access"
    )

    service_fragment_sets = tuple(set(item.source_fragment_ids) for item in service_items)

    kept = []
    dropped_subjects: list[str] = []
    covered_service_indexes: set[int] = set()

    for obs in payload.operational_observations:
        obs_ids = set(obs.source_fragment_ids)

        matches = {
            index
            for index, service_ids in enumerate(service_fragment_sets)
            if obs_ids & service_ids
        }

        if matches:
            kept.append(obs)
            covered_service_indexes.update(matches)
        else:
            dropped_subjects.append(obs.subject_key)

    uncovered: set[int] = set()

    for index, item in enumerate(service_items):
        if index not in covered_service_indexes:
            uncovered.update(item.source_fragment_ids)

    normalized = replace(
        payload,
        operational_observations=tuple(kept),
    )

    return normalized, OperationalPayloadAudit(
        dropped_observation_count=len(dropped_subjects),
        dropped_observation_subject_keys=tuple(dropped_subjects),
        uncovered_service_access_fragment_ids=tuple(sorted(uncovered)),
    )
