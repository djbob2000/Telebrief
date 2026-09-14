"""End-to-end multi-shard scale verification for Event-First continuous authority."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest

from src.config_loader import EditionScopeConfig, load_config
from src.processing.edition_scope import SCOPE_VERSION, resolve_edition_scope, scope_config_hash
from src.processing.event_authority import EventAuthorityService
from src.processing.event_brief import EventBriefService
from src.processing.event_triage import TRIAGE_VERSION, StoryTriageService
from src.repositories.event_authority import EventAuthorityRepository
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.event_processing_claims import EventProcessingClaimRepository
from src.repositories.event_retries import EventProcessingRetryRepository
from src.repositories.stories import StoryRepository


class FakeScaleAI:
    """Deterministic mock provider for scale testing with zero external calls."""

    def __init__(self) -> None:
        self.call_count = 0

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        json_mode: bool = True,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        self.call_count += 1
        story_blocks = re.split(r"Story #(\d+)", prompt)
        results = []
        for i in range(1, len(story_blocks), 2):
            sid = int(story_blocks[i])
            block_text = story_blocks[i + 1] if i + 1 < len(story_blocks) else ""
            frags = [int(m) for m in re.findall(r"frag=(\d+)", block_text)]
            results.append(
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_confidence": 0.95,
                    "scope_reason": "synthetic scale local",
                    "scope_basis_fragment_ids": frags[:1] if frags else [],
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.95,
                    "reason": "synthetic scale valid",
                    "subject_key": "power_supply",
                    "subject_label": "Электроснабжение",
                    "brief_payload": {
                        "summary": "Synthetic municipal repair in progress",
                        "publishability": "brief",
                        "thematic_rubric": "utilities",
                        "evidence_items": [
                            {
                                "text": "Synthetic municipal repair in progress",
                                "kind": "established_fact",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": frags[:1] if frags else [],
                            }
                        ],
                    },
                }
            )
        return json.dumps({"results": results})


async def _seed_scale_stories(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    count: int = 200,
) -> list[int]:
    now = dt.datetime.now(dt.timezone.utc)
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'scale-src', 'Scale Source')
        ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """
    )
    source_id = int((await cur.fetchone())[0])  # type: ignore[index]

    await conn.execute(
        """
        INSERT INTO source_editions (source_id, edition_id)
        VALUES (%s, %s)
        ON CONFLICT (source_id, edition_id) DO NOTHING
        """,
        (source_id, edition_id),
    )

    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
        VALUES (%s, 'message', 'scale-item-1', %s, %s)
        ON CONFLICT (source_id, external_id) DO UPDATE SET first_collected_at = EXCLUDED.first_collected_at
        RETURNING id
        """,
        (source_id, now, now),
    )
    item_id = int((await cur.fetchone())[0])  # type: ignore[index]

    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, collected_at, content_hash, text_content)
        VALUES (%s, 1, %s, 'scale-hash-1', 'Synthetic municipal scale text')
        ON CONFLICT (source_item_id, revision_no) DO UPDATE SET collected_at = EXCLUDED.collected_at
        RETURNING id
        """,
        (item_id, now),
    )
    revision_id = int((await cur.fetchone())[0])  # type: ignore[index]

    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()
    created_story_ids: list[int] = []

    for i in range(count):
        sid = await story_repo.create_story_shell(
            conn, edition_id=edition_id, knowledge_source="event_first"
        )
        created_story_ids.append(sid)

        v_cur = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES (%s, '[1, 0]'::vector, 'scale-model', 2)
            ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET created_at = now()
            RETURNING id
            """,
            (f"scale-vhash-{sid}",),
        )
        vector_id = int((await v_cur.fetchone())[0])  # type: ignore[index]

        f_cur = await conn.execute(
            """
            INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate)
            VALUES (%s, %s, %s, %s, 'v1', TRUE)
            ON CONFLICT (source_item_revision_id, ordinal, fragmenter_version) DO UPDATE SET text_content = EXCLUDED.text_content
            RETURNING id
            """,
            (revision_id, i, f"Synthetic fragment for story {sid}", f"scale-fhash-{sid}"),
        )
        frag_id = int((await f_cur.fetchone())[0])  # type: ignore[index]

        fe_cur = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (frag_id, vector_id),
        )
        emb_id = int((await fe_cur.fetchone())[0])  # type: ignore[index]

        aid = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=sid,
            fragment_id=frag_id,
            fragment_embedding_id=emb_id,
            assignment_kind="new_story",
        )

        await cluster_repo.upsert_cluster_state(
            conn,
            story_id=sid,
            centroid=[1.0, 0.0],
            model="scale-model",
            dimensions=2,
            fragment_count=1,
            unique_source_count=1,
            first_seen_at=now,
            last_seen_at=now,
            latest_assignment_id=aid,
            analysis_dirty=True,
        )

    return created_story_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_shard_authority_scale_drain(conn, uow, edition):
    story_count = 200
    shard_count = 4

    story_ids = await _seed_scale_stories(conn, edition.id, count=story_count)
    assert len(story_ids) == story_count

    fake_ai = FakeScaleAI()
    base_config = load_config()

    edition_scopes = dict(base_config.settings.edition_scopes)
    edition_scopes[edition.slug] = EditionScopeConfig(
        name=edition.name,
        focus_places=(edition.name,),
        direct_impact_only=True,
        notes=(),
    )
    pipeline = replace(
        base_config.settings.event_pipeline,
        authority_shard_count=shard_count,
        triage_batch_size=10,
        active_window_hours=72,
    )
    settings = replace(
        base_config.settings,
        edition_scopes=edition_scopes,
        event_pipeline=pipeline,
    )
    config = replace(base_config, settings=settings)
    runtime = SimpleNamespace(uow=uow, config=config)

    cluster_repo = EventClusterRepository()
    authority_repo = EventAuthorityRepository()
    claim_repo = EventProcessingClaimRepository()
    retry_repo = EventProcessingRetryRepository()

    triage_service = StoryTriageService(
        fake_ai,
        cluster_repo=cluster_repo,
        model="mock-model",
        uow=uow,
        max_output_tokens=pipeline.triage_max_output_tokens,
        reasoning_effort=pipeline.triage_reasoning_effort,
    )
    brief_service = EventBriefService(cluster_repo=cluster_repo)

    authority_service = EventAuthorityService(
        runtime,
        config,
        cluster_repo=cluster_repo,
        authority_repo=authority_repo,
        retry_repo=retry_repo,
        claim_repo=claim_repo,
        triage_service=triage_service,
        brief_service=brief_service,
    )

    async with uow.transaction() as tx:
        _slug, scope_config = await resolve_edition_scope(tx, config, edition.id)
    sc_hash = scope_config_hash(scope_config)

    async def shard_worker(shard_id: int) -> int:
        triaged_count = 0
        while True:
            async with uow.transaction() as tx:
                targets = await authority_repo.list_background_targets(
                    tx,
                    edition_id=edition.id,
                    triage_version=TRIAGE_VERSION,
                    scope_version=SCOPE_VERSION,
                    scope_config_hash=sc_hash,
                    now=dt.datetime.now(dt.timezone.utc),
                    limit=10,
                    active_window_hours=72,
                    shard_index=shard_id,
                    shard_count=shard_count,
                )
            if not targets:
                break
            result = await authority_service.process_batch(
                targets,
                mode="background",
                coordination_scope=f"scale-test:{edition.id}:{shard_id}",
            )
            triaged_count += result.stats.triaged
        return triaged_count

    # 1. Run 4 shard workers concurrently
    shard_results = await asyncio.gather(*(shard_worker(i) for i in range(shard_count)))
    total_triaged = sum(shard_results)
    assert total_triaged == story_count
    for shard_idx, count in enumerate(shard_results):
        assert count > 0, f"Shard {shard_idx} did not process any stories"

    # 2. Verify all 200 stories received exactly one scope and triage decision
    cur = await conn.execute(
        "SELECT count(*) FROM story_edition_scope_decisions WHERE edition_id = %s",
        (edition.id,),
    )
    scope_decisions_count = int((await cur.fetchone())[0])
    assert scope_decisions_count == story_count

    cur = await conn.execute(
        "SELECT count(*) FROM story_event_triage_decisions WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)",
        (edition.id,),
    )
    triage_decisions_count = int((await cur.fetchone())[0])
    assert triage_decisions_count == story_count

    # 3. Verify 0 duplicate decisions recorded
    cur = await conn.execute(
        """
        SELECT story_id, count(*)
        FROM story_edition_scope_decisions
        WHERE edition_id = %s
        GROUP BY story_id
        HAVING count(*) > 1
        """,
        (edition.id,),
    )
    duplicate_scope = await cur.fetchall()
    assert duplicate_scope == [], f"Found duplicate scope decisions: {duplicate_scope}"

    cur = await conn.execute(
        """
        SELECT story_id, count(*)
        FROM story_event_triage_decisions
        WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)
        GROUP BY story_id
        HAVING count(*) > 1
        """,
        (edition.id,),
    )
    duplicate_triage = await cur.fetchall()
    assert duplicate_triage == [], f"Found duplicate triage decisions: {duplicate_triage}"

    # 4. Verify analysis_dirty cleared for all stories
    cur = await conn.execute(
        """
        SELECT count(*) FROM story_cluster_state
        WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)
        AND analysis_dirty = TRUE
        """,
        (edition.id,),
    )
    dirty_count = int((await cur.fetchone())[0])
    assert dirty_count == 0

    # 5. Verify gap count reaches 0
    remaining_targets = await authority_repo.list_background_targets(
        conn,
        edition_id=edition.id,
        triage_version=TRIAGE_VERSION,
        scope_version=SCOPE_VERSION,
        scope_config_hash=sc_hash,
        now=dt.datetime.now(dt.timezone.utc),
        limit=10,
        active_window_hours=72,
    )
    assert len(remaining_targets) == 0

    # 6. Idempotency: second drain run performs 0 provider calls and completes in < 150ms
    calls_before = fake_ai.call_count
    start_second = time.perf_counter()
    second_results = await asyncio.gather(*(shard_worker(i) for i in range(shard_count)))
    second_duration = time.perf_counter() - start_second

    assert fake_ai.call_count == calls_before, "Second run made unexpected provider calls"
    assert sum(second_results) == 0
    assert second_duration < 0.15, f"Second drain took {second_duration:.4f}s (expected < 0.15s)"
