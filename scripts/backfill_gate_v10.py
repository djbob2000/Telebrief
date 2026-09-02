"""Targeted Gate v10 backfill script with quiet-window bypass and stagnation guard."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config, load_database_config
from src.jobs.event_processing import coalesce_dirty_stories_task
from src.publication.policies import PublicationPolicyService
from src.publication.repository import PublicationRepository
from src.runtime import clear_runtime, install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill_gate_v10")


async def run_gate_v10_backfill(
    *,
    edition_id: int,
    snapshot_at: dt.datetime | None = None,
    max_rounds: int = 50,
    dry_run: bool = False,
    infrastructure: Any = None,
) -> dict[str, Any]:
    """Execute targeted Gate v10 backfill for in-window stories lacking authoritative decisions."""
    owns_infra = infrastructure is None
    if owns_infra:
        db_config = load_database_config(require_enabled=True)
        full_config = load_config()
        infra = await build_infrastructure(db_config)
        object.__setattr__(infra, "config", full_config)
        install_runtime(infra)
    else:
        infra = infrastructure
        full_config = getattr(infra, "config", None) or load_config()
        install_runtime(infra)

    if snapshot_at is None:
        snapshot_at = dt.datetime.now(dt.timezone.utc)

    try:
        policy_service = PublicationPolicyService()
        repo = PublicationRepository()

        async with infra.uow.transaction() as conn:
            policy_set = await policy_service.ensure_current(
                conn,
                edition_id=edition_id,
                publication_type="digest_grouped",
                config=full_config,
            )
            policy_id = policy_set.eligibility.id

            gap_story_ids = await repo.find_authority_gap_story_ids(
                conn,
                edition_id=edition_id,
                snapshot_at=snapshot_at,
                eligibility_policy_id=policy_id,
            )

        initial_gap = len(gap_story_ids)
        logger.info(
            "Found %d stories in authority gap for edition %d (policy %d)",
            initial_gap,
            edition_id,
            policy_id,
        )

        if dry_run:
            logger.info("Dry run requested; stopping without modification.")
            return {
                "edition_id": edition_id,
                "initial_gap": initial_gap,
                "final_gap": initial_gap,
                "rounds": 0,
                "dry_run": True,
            }

        if not gap_story_ids:
            logger.info("Authority gap is already 0. Nothing to backfill.")
            return {
                "edition_id": edition_id,
                "initial_gap": 0,
                "final_gap": 0,
                "rounds": 0,
                "dry_run": False,
            }

        # Mark authority gap stories as dirty in story_cluster_state
        async with infra.uow.transaction() as conn:
            await conn.execute(
                """
                UPDATE story_cluster_state
                SET analysis_dirty = TRUE
                WHERE story_id = ANY(%s)
                """,
                (gap_story_ids,),
            )
        logger.info("Marked %d gap stories as analysis_dirty", len(gap_story_ids))

        # Drain loop with quiet-window bypass and stagnation guard
        rounds = 0
        prev_gap_count = initial_gap

        while gap_story_ids and rounds < max_rounds:
            rounds += 1
            logger.info(
                "Starting backfill round %d/%d (remaining gap=%d)",
                rounds,
                max_rounds,
                len(gap_story_ids),
            )

            coalesce_stats = await coalesce_dirty_stories_task.func(
                edition_id=edition_id,
                force_settled=True,
            )
            logger.info("Coalesce round %d stats: %s", rounds, coalesce_stats)

            async with infra.uow.transaction() as conn:
                gap_story_ids = await repo.find_authority_gap_story_ids(
                    conn,
                    edition_id=edition_id,
                    snapshot_at=snapshot_at,
                    eligibility_policy_id=policy_id,
                )

            current_gap_count = len(gap_story_ids)
            logger.info(
                "Remaining authority gap after round %d: %d (previously %d)",
                rounds,
                current_gap_count,
                prev_gap_count,
            )

            if current_gap_count >= prev_gap_count and current_gap_count > 0:
                raise RuntimeError(
                    f"Backfill stagnation detected at round {rounds}: "
                    f"authority gap did not decrease ({current_gap_count} remaining: {gap_story_ids[:10]})"
                )

            prev_gap_count = current_gap_count

        async with infra.uow.transaction() as conn:
            final_gap = await repo.count_authority_gap(
                conn,
                edition_id=edition_id,
                snapshot_at=snapshot_at,
                eligibility_policy_id=policy_id,
            )

        if final_gap > 0:
            raise RuntimeError(
                f"Backfill reached max rounds ({max_rounds}) with remaining authority gap: {final_gap}"
            )

        logger.info(
            "Gate v10 backfill successfully completed in %d rounds! Authority gap: 0",
            rounds,
        )
        return {
            "edition_id": edition_id,
            "initial_gap": initial_gap,
            "final_gap": 0,
            "rounds": rounds,
            "dry_run": False,
        }

    finally:
        clear_runtime(infra)
        if owns_infra:
            await infra.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Targeted Gate v10 backfill script")
    parser.add_argument("--edition-id", type=int, required=True, help="Edition ID to backfill")
    parser.add_argument(
        "--snapshot-at",
        type=str,
        default=None,
        help="ISO timestamp for snapshot reference (default: now)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=50,
        help="Maximum coalesce iterations before failing (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count authority gap without modifying database",
    )

    args = parser.parse_args()
    snapshot_at = dt.datetime.fromisoformat(args.snapshot_at) if args.snapshot_at else None

    asyncio.run(
        run_gate_v10_backfill(
            edition_id=args.edition_id,
            snapshot_at=snapshot_at,
            max_rounds=args.max_rounds,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
