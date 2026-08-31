"""Export a deterministic frozen publication regression case snapshot from a run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import AsyncConnection

from scripts.publication_regression import source_fingerprint


def build_export_payload(
    *,
    run: dict[str, Any],
    source_corpus: list[dict[str, Any]],
    publish_evidence: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    sealed_story_ids: list[str],
    article_plan_story_ids: list[str],
    digest_plan_story_ids: list[str],
    article_claim_trace: list[dict[str, Any]],
    digest_coverage_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure builder for frozen publication regression export payloads."""
    return {
        "run": run,
        "source_corpus": source_corpus,
        "publish_evidence": publish_evidence,
        "candidates": candidates,
        "sealed_story_ids": sealed_story_ids,
        "article_plan_story_ids": article_plan_story_ids,
        "digest_plan_story_ids": digest_plan_story_ids,
        "article_claim_trace": article_claim_trace,
        "digest_coverage_trace": digest_coverage_trace,
    }


async def export_publication_case(conn: AsyncConnection, run_id: int) -> dict[str, Any]:
    """Export a complete frozen snapshot of a publication run from the database."""
    # 1. Publication run
    cur = await conn.execute(
        """
        SELECT id, edition_id, publication_type, request_key, snapshot_at,
               eligibility_policy_id, selection_policy_id, writer_policy_id,
               status, error_kind, metadata, created_at
        FROM publication_runs
        WHERE id = %s
        """,
        (run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"Publication run {run_id} not found")

    run_dict = {
        "id": row[0],
        "edition_id": row[1],
        "publication_type": row[2],
        "request_key": row[3],
        "snapshot_at": row[4].isoformat() if row[4] else None,
        "eligibility_policy_id": row[5],
        "selection_policy_id": row[6],
        "writer_policy_id": row[7],
        "status": row[8],
        "error_kind": row[9],
        "metadata": row[10] if isinstance(row[10], dict) else (json.loads(row[10]) if row[10] else {}),
        "created_at": row[11].isoformat() if row[11] else None,
    }

    # 2. Candidates and sealed inputs
    cur = await conn.execute(
        """
        SELECT id, story_id, story_revision_id, deterministic_rank, snapshot_features
        FROM publication_candidates
        WHERE publication_run_id = %s
        ORDER BY deterministic_rank ASC
        """,
        (run_id,),
    )
    cand_rows = await cur.fetchall()

    candidates: list[dict[str, Any]] = []
    story_ids: list[int] = []

    for r in cand_rows:
        sid_str = f"story:{r[1]}"
        snap_feats = r[4] if isinstance(r[4], dict) else (json.loads(r[4]) if r[4] else {})
        cand_dict = {
            "candidate_id": r[0],
            "story_id": sid_str,
            "numeric_story_id": r[1],
            "story_revision_id": r[2],
            "deterministic_rank": r[3],
            "snapshot_features": snap_feats,
        }
        candidates.append(cand_dict)
        if r[1] not in story_ids:
            story_ids.append(r[1])

    cur = await conn.execute(
        """
        SELECT story_id FROM publication_inputs
        WHERE publication_run_id = %s
        """,
        (run_id,),
    )
    inp_rows = await cur.fetchall()
    sealed_story_ids: list[str] = [f"story:{r[0]}" for r in inp_rows]
    for r in inp_rows:
        if r[0] not in story_ids:
            story_ids.append(r[0])

    # 3. Source corpus & evidence from active revisions
    source_corpus: list[dict[str, Any]] = []
    publish_evidence: list[dict[str, Any]] = []

    if story_ids:
        cur = await conn.execute(
            """
            SELECT s.id, sr.event_payload, sr.semantic_text
            FROM stories s
            JOIN story_revisions sr ON s.current_revision_id = sr.id
            WHERE s.id = ANY(%s)
            """,
            (story_ids,),
        )
        rev_rows = await cur.fetchall()
        for sid, ep_raw, sem_text in rev_rows:
            ep = ep_raw if isinstance(ep_raw, dict) else (json.loads(ep_raw) if ep_raw else {})
            ev_items = ep.get("evidence_items", [])
            for evi in ev_items:
                if evi.get("publication_use") == "PUBLISH":
                    publish_evidence.append(evi)
                text = (evi.get("text") or evi.get("source_text") or "").strip()
                if text:
                    for fid in evi.get("source_fragment_ids", []):
                        source_corpus.append(
                            {
                                "fixture_fragment_id": str(fid),
                                "source_ref": evi.get("evidence_id"),
                                "source_fingerprint": source_fingerprint(text),
                                "text": text,
                            }
                        )
            if sem_text:
                source_corpus.append(
                    {
                        "fixture_fragment_id": f"story:{sid}:summary",
                        "source_ref": f"story:{sid}",
                        "source_fingerprint": source_fingerprint(sem_text),
                        "text": sem_text,
                    }
                )

    # 4. Publication and traces
    cur = await conn.execute(
        """
        SELECT id, publication_type, title, lead, body, metadata
        FROM publications
        WHERE publication_run_id = %s
        """,
        (run_id,),
    )
    pub_row = await cur.fetchone()
    article_claim_trace: list[dict[str, Any]] = []
    digest_coverage_trace: list[dict[str, Any]] = []
    article_plan_story_ids: list[str] = []
    digest_plan_story_ids: list[str] = []

    if pub_row is not None:
        meta = pub_row[5] if isinstance(pub_row[5], dict) else (json.loads(pub_row[5]) if pub_row[5] else {})
        if "article_claim_trace" in meta:
            act = meta["article_claim_trace"]
            if isinstance(act, dict) and "units" in act:
                article_claim_trace = act["units"]
            elif isinstance(act, list):
                article_claim_trace = act
        if "digest_coverage_trace" in meta:
            dct = meta["digest_coverage_trace"]
            if isinstance(dct, dict) and "stories" in dct:
                digest_coverage_trace = dct["stories"]
            elif isinstance(dct, list):
                digest_coverage_trace = dct

    if not article_plan_story_ids:
        article_plan_story_ids = list(sealed_story_ids)
    if not digest_plan_story_ids:
        digest_plan_story_ids = list(sealed_story_ids)

    return build_export_payload(
        run=run_dict,
        source_corpus=source_corpus,
        publish_evidence=publish_evidence,
        candidates=candidates,
        sealed_story_ids=sealed_story_ids,
        article_plan_story_ids=article_plan_story_ids,
        digest_plan_story_ids=digest_plan_story_ids,
        article_claim_trace=article_claim_trace,
        digest_coverage_trace=digest_coverage_trace,
    )


async def main_async(args: argparse.Namespace) -> None:
    db_url = (
        args.database_url
        or os.environ.get("DATABASE_URL")
        or os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
        or "postgresql://telebrief:telebrief@localhost:5432/telebrief_test"
    )
    async with await AsyncConnection.connect(db_url) as conn:
        payload = await export_publication_case(conn, args.run_id)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Exported publication case for run {args.run_id} to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export frozen publication regression case snapshot")
    parser.add_argument("--run-id", type=int, required=True, help="Publication run ID")
    parser.add_argument("--database-url", type=str, default=None, help="Database connection URL")
    parser.add_argument("--output", type=str, required=True, help="Output JSON path")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
