#!/usr/bin/env python3
"""Benchmark Event-First continuous authority processing across sharded workers."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import psycopg  # noqa: E402

from src.processing.edition_scope import (  # noqa: E402
    SCOPE_VERSION,
    resolve_edition_scope,
    scope_config_hash,
)
from src.processing.event_authority import EventAuthorityService  # noqa: E402
from src.processing.event_brief import EventBriefService  # noqa: E402
from src.processing.event_triage import TRIAGE_VERSION, StoryTriageService  # noqa: E402
from src.repositories.event_authority import EventAuthorityRepository  # noqa: E402
from src.repositories.event_clusters import EventClusterRepository  # noqa: E402
from src.repositories.event_processing_claims import EventProcessingClaimRepository  # noqa: E402
from src.repositories.event_retries import EventProcessingRetryRepository  # noqa: E402
from src.repositories.stories import StoryRepository  # noqa: E402


class FakeBenchmarkAI:
    """Mock AI provider for controlled delay and zero external calls."""

    def __init__(self, delay_seconds: float = 1.0) -> None:
        self.delay_seconds = delay_seconds
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
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        # Parse story IDs and fragments from prompt
        import re

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
                    "scope_reason": "synthetic benchmark local",
                    "scope_basis_fragment_ids": frags[:1] if frags else [],
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.95,
                    "reason": "synthetic benchmark valid",
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


async def setup_synthetic_data(
    conn: psycopg.AsyncConnection,
    *,
    story_count: int,
) -> tuple[int, list[int]]:
    """Create a synthetic benchmark edition, source, and N dirty story clusters."""
    await cleanup_synthetic_data(conn, [], None)
    now = dt.datetime.now(dt.timezone.utc)
    cur = await conn.execute(
        """
        INSERT INTO editions (slug, name, language, timezone)
        VALUES ('bench-edition', 'Benchmark Edition', 'ru', 'UTC')
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to insert/find benchmark edition")
    edition_id = int(row[0])

    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'bench-src', 'Bench Source')
        ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to insert/find benchmark source")
    source_id = int(row[0])

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
        VALUES (%s, 'message', 'bench-item-1', %s, %s)
        ON CONFLICT (source_id, external_id) DO UPDATE SET first_collected_at = EXCLUDED.first_collected_at
        RETURNING id
        """,
        (source_id, now, now),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to insert/find benchmark source item")
    item_id = int(row[0])

    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, collected_at, content_hash, text_content)
        VALUES (%s, 1, %s, 'bench-hash-1', 'Synthetic municipal benchmark text')
        ON CONFLICT (source_item_id, revision_no) DO UPDATE SET collected_at = EXCLUDED.collected_at
        RETURNING id
        """,
        (item_id, now),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to insert/find benchmark source revision")
    revision_id = int(row[0])

    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()
    created_story_ids: list[int] = []

    for i in range(story_count):
        sid = await story_repo.create_story_shell(
            conn, edition_id=edition_id, knowledge_source="event_first"
        )
        created_story_ids.append(sid)

        v_cur = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES (%s, '[1, 0]'::vector, 'bench-model', 2)
            ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET created_at = now()
            RETURNING id
            """,
            (f"bench-vhash-{sid}",),
        )
        v_row = await v_cur.fetchone()
        if v_row is None:
            raise RuntimeError("Failed to insert vector")
        vector_id = int(v_row[0])

        f_cur = await conn.execute(
            """
            INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate)
            VALUES (%s, %s, %s, %s, 'v1', TRUE)
            ON CONFLICT (source_item_revision_id, ordinal, fragmenter_version) DO UPDATE SET text_content = EXCLUDED.text_content
            RETURNING id
            """,
            (revision_id, i, f"Synthetic fragment for story {sid}", f"bench-fhash-{sid}"),
        )
        f_row = await f_cur.fetchone()
        if f_row is None:
            raise RuntimeError("Failed to insert fragment")
        frag_id = int(f_row[0])

        fe_cur = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (frag_id, vector_id),
        )
        fe_row = await fe_cur.fetchone()
        if fe_row is None:
            raise RuntimeError("Failed to insert embedding")
        emb_id = int(fe_row[0])

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
            model="bench-model",
            dimensions=2,
            fragment_count=1,
            unique_source_count=1,
            first_seen_at=now,
            last_seen_at=now,
            latest_assignment_id=aid,
            analysis_dirty=True,
        )

    return edition_id, created_story_ids


async def cleanup_synthetic_data(
    conn: psycopg.AsyncConnection,
    story_ids: list[int],
    edition_id: int | None,
) -> None:
    """Clean up synthetic benchmark rows in proper foreign key order."""
    if edition_id is None:
        cur = await conn.execute("SELECT id FROM editions WHERE slug = 'bench-edition'")
        row = await cur.fetchone()
        if row is not None:
            edition_id = int(row[0])

    if edition_id is not None:
        await conn.execute(
            "UPDATE stories SET current_revision_id = NULL WHERE edition_id = %s",
            (edition_id,),
        )
        await conn.execute(
            "DELETE FROM story_revisions WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)",
            (edition_id,),
        )
        await conn.execute(
            "DELETE FROM story_event_triage_decisions WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)",
            (edition_id,),
        )
        await conn.execute(
            "DELETE FROM story_edition_scope_decisions WHERE edition_id = %s",
            (edition_id,),
        )
        await conn.execute(
            "DELETE FROM story_cluster_state WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)",
            (edition_id,),
        )
        await conn.execute(
            "DELETE FROM story_fragments WHERE story_id IN (SELECT id FROM stories WHERE edition_id = %s)",
            (edition_id,),
        )
        await conn.execute("DELETE FROM stories WHERE edition_id = %s", (edition_id,))
        await conn.execute("DELETE FROM source_editions WHERE edition_id = %s", (edition_id,))

    await conn.execute(
        """
        DELETE FROM source_fragment_embeddings
        WHERE fragment_id IN (
            SELECT sf.id FROM source_fragments sf
            JOIN source_item_revisions sir ON sir.id = sf.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            WHERE s.external_id = 'bench-src'
        )
        """
    )
    await conn.execute(
        """
        DELETE FROM source_fragments
        WHERE source_item_revision_id IN (
            SELECT sir.id FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            WHERE s.external_id = 'bench-src'
        )
        """
    )
    await conn.execute(
        """
        DELETE FROM fragment_embedding_vectors
        WHERE normalized_hash LIKE 'bench-vhash-%'
        """
    )
    await conn.execute(
        """
        DELETE FROM source_item_revisions
        WHERE source_item_id IN (
            SELECT si.id FROM source_items si
            JOIN sources s ON s.id = si.source_id
            WHERE s.external_id = 'bench-src'
        )
        """
    )
    await conn.execute(
        """
        DELETE FROM source_items
        WHERE source_id IN (SELECT id FROM sources WHERE external_id = 'bench-src')
        """
    )
    if edition_id is not None:
        await conn.execute("DELETE FROM editions WHERE id = %s", (edition_id,))
    await conn.execute("DELETE FROM sources WHERE external_id = 'bench-src'")


class MinimalUnitOfWork:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def transaction(self):
        class _Tx:
            def __init__(self, url: str) -> None:
                self.url = url
                self.conn: psycopg.AsyncConnection | None = None

            async def __aenter__(self) -> psycopg.AsyncConnection:
                self.conn = await psycopg.AsyncConnection.connect(self.url)
                return self.conn

            async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
                if self.conn:
                    if exc_type is None:
                        await self.conn.commit()
                    else:
                        await self.conn.rollback()
                    await self.conn.close()

        return _Tx(self.db_url)


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    db_url = os.environ.get(args.database_url_from_env) or os.environ.get(
        "DATABASE_URL", "postgresql://telebrief:telebrief@localhost:5432/telebrief_test"
    )

    setup_conn = await psycopg.AsyncConnection.connect(db_url, autocommit=True)
    edition_id: int | None = None
    story_ids: list[int] = []
    try:
        print(f"Setting up {args.synthetic_stories} synthetic stories in DB...")
        t0_setup = time.perf_counter()
        edition_id, story_ids = await setup_synthetic_data(
            setup_conn,
            story_count=args.synthetic_stories,
        )
        setup_time = time.perf_counter() - t0_setup
        print(f"Setup completed in {setup_time:.2f}s.")

        uow = MinimalUnitOfWork(db_url)
        fake_ai = FakeBenchmarkAI(delay_seconds=args.provider_delay)

        cluster_repo = EventClusterRepository()
        authority_repo = EventAuthorityRepository()
        retry_repo = EventProcessingRetryRepository()
        claim_repo = EventProcessingClaimRepository()

        triage_service = StoryTriageService(
            ai_cascade=fake_ai,
            cluster_repo=cluster_repo,
            uow=uow,
            max_output_tokens=32768,
        )
        brief_service = EventBriefService(cluster_repo=cluster_repo)

        from dataclasses import replace

        from src.config_loader import EditionScopeConfig, load_config

        config = load_config()
        edition_scopes = dict(config.settings.edition_scopes)
        edition_scopes["bench-edition"] = EditionScopeConfig(
            name="Benchmark Edition",
            focus_places=("Benchmark",),
        )
        pipeline = replace(
            config.settings.event_pipeline,
            triage_batch_size=args.batch_size,
            triage_max_input_chars=48000,
            authority_shard_count=args.shards,
            authority_coordination_lease_seconds=60,
            event_processing_stage_lease_seconds=300,
            authority_provider_timeout_seconds=60,
            triage_excerpt_chars=320,
            triage_min_ignore_confidence=0.95,
            triage_max_output_tokens=32768,
            triage_split_max_extra_calls_per_cycle=4,
            triage_max_attempts_per_assignment=3,
            provider_retry_backoff_seconds=10,
            provider_retry_backoff_max_seconds=60,
        )
        settings = replace(
            config.settings,
            edition_scopes=edition_scopes,
            event_pipeline=pipeline,
        )
        config = replace(config, settings=settings)

        runtime = SimpleNamespace(uow=uow, config=config)

        service = EventAuthorityService(
            runtime,
            config,
            cluster_repo=cluster_repo,
            authority_repo=authority_repo,
            retry_repo=retry_repo,
            claim_repo=claim_repo,
            triage_service=triage_service,
            brief_service=brief_service,
        )

        async with uow.transaction() as conn:
            _slug, scope_config = await resolve_edition_scope(conn, config, edition_id)
        sc_hash = scope_config_hash(scope_config)

        # 1. Measure initial list_background_targets query performance
        async with uow.transaction() as conn:
            t0_query = time.perf_counter()
            initial_targets = await authority_repo.list_background_targets(
                conn,
                edition_id=edition_id,
                triage_version=TRIAGE_VERSION,
                scope_version=SCOPE_VERSION,
                scope_config_hash=sc_hash,
                now=dt.datetime.now(dt.timezone.utc),
                limit=args.batch_size,
                shard_index=0,
                shard_count=args.shards,
            )
            query_time_ms = (time.perf_counter() - t0_query) * 1000

        print(
            f"Initial selector query latency: {query_time_ms:.2f} ms ({len(initial_targets)} targets in batch)"
        )

        # 2. Sharded concurrent drain
        print(f"Running authority drain with {args.shards} shards concurrently...")
        t0_drain = time.perf_counter()

        async def shard_worker(shard_idx: int) -> int:
            worker_triaged = 0
            while True:
                async with uow.transaction() as conn:
                    targets = await authority_repo.list_background_targets(
                        conn,
                        edition_id=edition_id,
                        triage_version=TRIAGE_VERSION,
                        scope_version=SCOPE_VERSION,
                        scope_config_hash=sc_hash,
                        now=dt.datetime.now(dt.timezone.utc),
                        limit=args.batch_size,
                        shard_index=shard_idx,
                        shard_count=args.shards,
                    )
                if not targets:
                    break
                res = await service.process_batch(
                    targets,
                    mode="background",
                    coordination_scope=f"authority:{shard_idx}",
                )
                worker_triaged += res.stats.triaged
                if res.stats.triaged == 0 and res.stats.already_satisfied == 0:
                    # Avoid tight loop on busy contention
                    await asyncio.sleep(0.05)
            return worker_triaged

        worker_results = await asyncio.gather(*(shard_worker(s) for s in range(args.shards)))
        total_drain_time = time.perf_counter() - t0_drain

        # 3. Post-drain verifications
        async with uow.transaction() as conn:
            # Check for duplicate decisions
            dup_cur = await conn.execute(
                """
                SELECT story_id, COUNT(*)
                FROM story_event_triage_decisions
                WHERE story_id = ANY(%s)
                GROUP BY story_id
                HAVING COUNT(*) > 1
                """,
                (story_ids,),
            )
            dup_rows = await dup_cur.fetchall()
            duplicate_stories = [int(r[0]) for r in dup_rows]

            # Check final unsatisfied targets
            remaining_targets = await authority_repo.list_background_targets(
                conn,
                edition_id=edition_id,
                triage_version=TRIAGE_VERSION,
                scope_version=SCOPE_VERSION,
                scope_config_hash=sc_hash,
                now=dt.datetime.now(dt.timezone.utc),
                limit=args.synthetic_stories,
                shard_index=0,
                shard_count=1,
            )
            final_unsatisfied = len(remaining_targets)

        report = {
            "synthetic_stories": args.synthetic_stories,
            "shards": args.shards,
            "provider_delay_s": args.provider_delay,
            "wall_time_s": round(total_drain_time, 2),
            "provider_calls": fake_ai.call_count,
            "triaged_per_shard": worker_results,
            "total_triaged": sum(worker_results),
            "duplicate_story_decisions_count": len(duplicate_stories),
            "final_unsatisfied_count": final_unsatisfied,
            "initial_query_latency_ms": round(query_time_ms, 2),
            "throughput_stories_per_sec": round(
                args.synthetic_stories / max(0.001, total_drain_time), 2
            ),
        }

        print("\n=== Benchmark Results ===")
        print(json.dumps(report, indent=2))
        return report

    finally:
        if not args.keep_data:
            print("Cleaning up synthetic data...")
            await cleanup_synthetic_data(setup_conn, story_ids, edition_id)
        await setup_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Event-First authority drain.")
    parser.add_argument("--synthetic-stories", type=int, default=500)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--provider-delay", type=float, default=1.0)
    parser.add_argument(
        "--database-url-from-env",
        type=str,
        default="DATABASE_URL",
        help="Env var containing postgres connection url",
    )
    parser.add_argument("--real-provider", action="store_true", default=False)
    parser.add_argument("--keep-data", action="store_true", default=False)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()

    if args.real_provider:
        print("ERROR: --real-provider is disabled for automated benchmarks.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
