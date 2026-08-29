"""Tests for RepresentativeEvidenceSampler (MMR + diversity)."""

from __future__ import annotations

import datetime as dt

import pytest

from src.domain.event_pipeline import SourceFragment
from src.processing.evidence_sampling import (
    FragmentWithContext,
    RepresentativeEvidenceSampler,
    SampledFragment,
)


@pytest.mark.unit
def test_evidence_sampler_small_cluster():
    now = dt.datetime.now(dt.timezone.utc)
    sampler = RepresentativeEvidenceSampler()

    f1 = SourceFragment(1, 10, 0, "Water off on AKZ", "h1", "v1", True, None, now)
    f2 = SourceFragment(2, 11, 0, "Repairs in progress", "h2", "v1", True, None, now)

    ctxs = [
        FragmentWithContext(f1, [1.0, 0.0], 100, "Chat", "community", now),
        FragmentWithContext(f2, [0.9, 0.1], 101, "Vodokanal", "official", now),
    ]

    sampled = sampler.sample_fragments(ctxs, centroid=[1.0, 0.0], limit=16)
    assert len(sampled) == 2
    assert isinstance(sampled[0], SampledFragment)


@pytest.mark.unit
def test_evidence_sampler_prioritizes_official_and_diverse_sources():
    now = dt.datetime.now(dt.timezone.utc)
    sampler = RepresentativeEvidenceSampler()

    # 1 official fragment, 5 identical community fragments from source 1, 1 distinct community from source 2
    f_off = SourceFragment(1, 10, 0, "Official announcement", "h_off", "v1", True, None, now)
    ctx_off = FragmentWithContext(f_off, [0.8, 0.6], 100, "City Hall", "official", now)

    ctx_comm_1 = [
        FragmentWithContext(
            SourceFragment(
                i, 20 + i, 0, f"Same chat complaint {i}", f"h_c1_{i}", "v1", True, None, now
            ),
            [1.0, 0.0],
            200,
            "Chat 1",
            "community",
            now,
        )
        for i in range(2, 7)
    ]

    f_comm_2 = SourceFragment(
        8, 30, 0, "Distinct observation from another neighborhood", "h_c2", "v1", True, None, now
    )
    ctx_comm_2 = FragmentWithContext(f_comm_2, [0.0, 1.0], 300, "Chat 2", "community", now)

    all_ctxs = [ctx_off] + ctx_comm_1 + [ctx_comm_2]

    # Sample top 3
    sampled = sampler.sample_fragments(all_ctxs, centroid=[0.7, 0.7], limit=3)
    assert len(sampled) == 3

    # Official source must be included
    sampled_ids = [s.fragment_id for s in sampled]
    assert 1 in sampled_ids
    # Distinct source Chat 2 should be included over duplicate Chat 1 fragments
    assert 8 in sampled_ids
