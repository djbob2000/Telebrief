"""Export a deterministic frozen publication regression case snapshot from a run."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import AsyncConnection

from scripts.publication_regression import source_fingerprint


def stable_fixture_fragment_id(
    platform: str,
    source_external_id: str,
    item_external_id: str,
    text: str,
) -> str:
    """Compute a stable fixture fragment identity based on platform, source, item, and text."""
    normalized_text = " ".join(text.casefold().split())
    raw = "\0".join(
        [platform.casefold(), source_external_id.casefold(), str(item_external_id), normalized_text]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"frag:{digest}"


def _extract_frag_id_from_support_id(support_id: str) -> int | None:
    m = re.search(r":frag:(\d+)", support_id)
    if m:
        return int(m.group(1))
    return None


async def export_source_corpus_for_window(
    conn: AsyncConnection,
    *,
    edition_slug: str | None = None,
    edition_id: int | None = None,
    window_start: dt.datetime | str,
    window_end: dt.datetime | str,
) -> list[dict[str, Any]]:
    """Export raw edition-bound source fragments inside the lookback window."""
    if isinstance(window_start, str):
        window_start = dt.datetime.fromisoformat(window_start)
    if isinstance(window_end, str):
        window_end = dt.datetime.fromisoformat(window_end)

    if edition_id is None and edition_slug is not None:
        cur = await conn.execute("SELECT id FROM editions WHERE slug = %s", (edition_slug,))
        row = await cur.fetchone()
        if row is not None:
            edition_id = row[0]
        else:
            raise ValueError(f"Edition with slug '{edition_slug}' not found")

    cur = await conn.execute(
        """
        SELECT
            f.id,
            f.text_content,
            f.is_candidate,
            f.drop_reason,
            s.platform,
            s.external_id,
            si.external_id,
            COALESCE(si.published_at, si.first_collected_at, f.created_at) AS observed_at
        FROM source_fragments f
        JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
        JOIN source_items si ON si.id = sir.source_item_id
        JOIN sources s ON s.id = si.source_id
        JOIN source_editions se ON se.source_id = s.id
        WHERE se.edition_id = %s
          AND COALESCE(si.published_at, si.first_collected_at, f.created_at) >= %s
          AND COALESCE(si.published_at, si.first_collected_at, f.created_at) <= %s
        ORDER BY observed_at ASC, f.id ASC
        """,
        (edition_id, window_start, window_end),
    )
    rows = await cur.fetchall()
    corpus: list[dict[str, Any]] = []
    for r in rows:
        fid, text, is_candidate, drop_reason, platform, s_ext_id, si_ext_id, obs_at = r
        fixture_frag_id = stable_fixture_fragment_id(platform, s_ext_id, si_ext_id, text)
        corpus.append(
            {
                "internal_fragment_id": fid,
                "fixture_fragment_id": fixture_frag_id,
                "source_fingerprint": source_fingerprint(text),
                "text": text,
                "observed_at": obs_at.isoformat() if hasattr(obs_at, "isoformat") else str(obs_at),
                "is_candidate": is_candidate,
                "drop_reason": drop_reason,
            }
        )
    return corpus


async def _resolve_fragment_identities(
    conn: AsyncConnection,
    fragment_ids: Sequence[int],
    cache: dict[int, tuple[str, str]],
) -> None:
    missing = [fid for fid in set(fragment_ids) if fid not in cache]
    if not missing:
        return
    cur = await conn.execute(
        """
        SELECT f.id, f.text_content, s.platform, s.external_id, si.external_id
        FROM source_fragments f
        JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
        JOIN source_items si ON si.id = sir.source_item_id
        JOIN sources s ON s.id = si.source_id
        WHERE f.id = ANY(%s)
        """,
        (missing,),
    )
    rows = await cur.fetchall()
    for fid, text, platform, s_ext, si_ext in rows:
        fix_id = stable_fixture_fragment_id(platform, s_ext, si_ext, text)
        fp = source_fingerprint(text)
        cache[fid] = (fix_id, fp)


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
    window_start: str | None = None,
    window_end: str | None = None,
    source_fingerprints: list[str] | None = None,
    source_refs: list[str] | None = None,
    source_fragment_ids: list[str] | None = None,
    evidence_fingerprints: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    evidence_fragment_ids: list[str] | None = None,
    candidate_refs: list[str] | None = None,
    candidate_fragment_ids: list[str] | None = None,
    candidate_fingerprints: list[str] | None = None,
    sealed_refs: list[str] | None = None,
    sealed_fragment_ids: list[str] | None = None,
    plan_refs: list[str] | None = None,
    plan_fragment_ids: list[str] | None = None,
    final_trace_supports: list[str] | None = None,
    final_trace_refs: list[str] | None = None,
    final_trace_fragment_ids: list[str] | None = None,
    final_trace_units: list[dict[str, Any]] | None = None,
    audit_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Pure builder for frozen publication regression export payloads."""
    payload: dict[str, Any] = {
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
    if window_start is not None:
        payload["window_start"] = window_start
    if window_end is not None:
        payload["window_end"] = window_end
    if source_fingerprints is not None:
        payload["source_fingerprints"] = source_fingerprints
    if source_refs is not None:
        payload["source_refs"] = source_refs
    if source_fragment_ids is not None:
        payload["source_fragment_ids"] = source_fragment_ids
    if evidence_fingerprints is not None:
        payload["evidence_fingerprints"] = evidence_fingerprints
    if evidence_refs is not None:
        payload["evidence_refs"] = evidence_refs
    if evidence_fragment_ids is not None:
        payload["evidence_fragment_ids"] = evidence_fragment_ids
    if candidate_refs is not None:
        payload["candidate_refs"] = candidate_refs
    if candidate_fragment_ids is not None:
        payload["candidate_fragment_ids"] = candidate_fragment_ids
    if candidate_fingerprints is not None:
        payload["candidate_fingerprints"] = candidate_fingerprints
    if sealed_refs is not None:
        payload["sealed_refs"] = sealed_refs
    if sealed_fragment_ids is not None:
        payload["sealed_fragment_ids"] = sealed_fragment_ids
    if plan_refs is not None:
        payload["plan_refs"] = plan_refs
    if plan_fragment_ids is not None:
        payload["plan_fragment_ids"] = plan_fragment_ids
    if final_trace_supports is not None:
        payload["final_trace_supports"] = final_trace_supports
    if final_trace_refs is not None:
        payload["final_trace_refs"] = final_trace_refs
    if final_trace_fragment_ids is not None:
        payload["final_trace_fragment_ids"] = final_trace_fragment_ids
    if final_trace_units is not None:
        payload["final_trace_units"] = final_trace_units
    if audit_errors is not None:
        payload["audit_errors"] = audit_errors
    return payload


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

    edition_id = row[1]
    pub_type = row[2]
    snapshot_at = row[4]
    elig_pol_id = row[5]

    # Lookback window calculation from eligibility policy
    lookback_hours = 24
    if elig_pol_id:
        cur = await conn.execute(
            "SELECT config FROM eligibility_policy_versions WHERE id = %s", (elig_pol_id,)
        )
        epv_row = await cur.fetchone()
        if epv_row and epv_row[0]:
            cfg = epv_row[0] if isinstance(epv_row[0], dict) else json.loads(epv_row[0])
            lookback_hours = int(cfg.get("lookback_hours", 24))

    window_end = snapshot_at or dt.datetime.now(dt.timezone.utc)
    window_start = window_end - dt.timedelta(hours=lookback_hours)

    # 2. Raw source corpus for window
    source_corpus = await export_source_corpus_for_window(
        conn,
        edition_id=edition_id,
        window_start=window_start,
        window_end=window_end,
    )
    fid_cache: dict[int, tuple[str, str]] = {
        r["internal_fragment_id"]: (r["fixture_fragment_id"], r["source_fingerprint"])
        for r in source_corpus
    }
    source_fingerprints = [r["source_fingerprint"] for r in source_corpus]
    source_fragment_ids = [r["fixture_fragment_id"] for r in source_corpus]
    source_refs: list[str] = []

    # 3. Evidence / Knowledge stage: all PUBLISH evidence visible at snapshot_at
    cur = await conn.execute(
        """
        SELECT DISTINCT ON (s.id) s.id, sr.id, sr.event_payload, sr.semantic_text
        FROM stories s
        JOIN story_revisions sr ON sr.story_id = s.id
        WHERE s.edition_id = %s
          AND sr.created_at <= %s
        ORDER BY s.id, sr.created_at DESC, sr.id DESC
        """,
        (edition_id, window_end),
    )
    all_story_rev_rows = await cur.fetchall()

    needed_fids: list[int] = []
    parsed_story_payloads: list[dict[str, Any]] = []
    for sid, srid, ep_raw, sem_text in all_story_rev_rows:
        ep = ep_raw if isinstance(ep_raw, dict) else (json.loads(ep_raw) if ep_raw else {})
        parsed_story_payloads.append(ep)
        for item in ep.get("evidence_items", []):
            if item.get("publication_use") == "PUBLISH" and item.get("kind") != "resident_question":
                needed_fids.extend(item.get("source_fragment_ids", []))

    await _resolve_fragment_identities(conn, needed_fids, fid_cache)

    publish_evidence: list[dict[str, Any]] = []
    evidence_fragment_ids: set[str] = set()
    evidence_fingerprints: set[str] = set()
    evidence_refs: set[str] = set()

    for ep in parsed_story_payloads:
        for item in ep.get("evidence_items", []):
            if item.get("publication_use") == "PUBLISH" and item.get("kind") != "resident_question":
                publish_evidence.append(item)
                if item.get("evidence_id"):
                    evidence_refs.add(item["evidence_id"])
                for fid in item.get("source_fragment_ids", []):
                    if fid in fid_cache:
                        fix_id, fp = fid_cache[fid]
                        evidence_fragment_ids.add(fix_id)
                        evidence_fingerprints.add(fp)

    # 4. Candidates stage
    cur = await conn.execute(
        """
        SELECT pc.id, pc.story_id, pc.story_revision_id, pc.deterministic_rank, pc.snapshot_features, sr.event_payload
        FROM publication_candidates pc
        JOIN story_revisions sr ON sr.id = pc.story_revision_id
        WHERE pc.publication_run_id = %s
        ORDER BY pc.deterministic_rank ASC
        """,
        (run_id,),
    )
    cand_rows = await cur.fetchall()

    candidates: list[dict[str, Any]] = []
    candidate_refs: set[str] = set()
    candidate_fragment_ids: set[str] = set()
    candidate_fingerprints: set[str] = set()
    cand_needed_fids: list[int] = []

    for r in cand_rows:
        cand_id, sid, srid, drank, sfeat, ep_raw = r
        ep = ep_raw if isinstance(ep_raw, dict) else (json.loads(ep_raw) if ep_raw else {})
        cand_dict = {
            "candidate_id": cand_id,
            "story_id": f"story:{sid}",
            "numeric_story_id": sid,
            "story_revision_id": srid,
            "deterministic_rank": drank,
            "snapshot_features": sfeat if isinstance(sfeat, dict) else (json.loads(sfeat) if sfeat else {}),
        }
        candidates.append(cand_dict)
        for item in ep.get("evidence_items", []):
            if item.get("publication_use") == "PUBLISH" and item.get("kind") != "resident_question":
                cand_needed_fids.extend(item.get("source_fragment_ids", []))
                if item.get("evidence_id"):
                    candidate_refs.add(item["evidence_id"])

    await _resolve_fragment_identities(conn, cand_needed_fids, fid_cache)
    for r in cand_rows:
        ep_raw = r[5]
        ep = ep_raw if isinstance(ep_raw, dict) else (json.loads(ep_raw) if ep_raw else {})
        for item in ep.get("evidence_items", []):
            if item.get("publication_use") == "PUBLISH" and item.get("kind") != "resident_question":
                for fid in item.get("source_fragment_ids", []):
                    if fid in fid_cache:
                        fix_id, fp = fid_cache[fid]
                        candidate_fragment_ids.add(fix_id)
                        candidate_fingerprints.add(fp)

    # 5. Sealed stage
    cur = await conn.execute(
        """
        SELECT pi.story_id, pi.story_revision_id, sr.event_payload
        FROM publication_inputs pi
        JOIN story_revisions sr ON sr.id = pi.story_revision_id
        WHERE pi.publication_run_id = %s
        """,
        (run_id,),
    )
    inp_rows = await cur.fetchall()
    sealed_story_ids = [f"story:{r[0]}" for r in inp_rows]

    cur = await conn.execute(
        """
        SELECT DISTINCT pif.fragment_id
        FROM publication_input_fragments pif
        JOIN publication_inputs pi ON pi.id = pif.publication_input_id
        WHERE pi.publication_run_id = %s
        """,
        (run_id,),
    )
    pif_rows = await cur.fetchall()
    sealed_fids = [r[0] for r in pif_rows]
    await _resolve_fragment_identities(conn, sealed_fids, fid_cache)

    sealed_fragment_ids: set[str] = set()
    sealed_fingerprints: set[str] = set()
    sealed_refs: set[str] = set()

    for fid in sealed_fids:
        if fid in fid_cache:
            fix_id, fp = fid_cache[fid]
            sealed_fragment_ids.add(fix_id)
            sealed_fingerprints.add(fp)

    if not sealed_fids:
        # Fallback to evidence fragment ids in sealed story revisions
        for r in inp_rows:
            ep_raw = r[2]
            ep = ep_raw if isinstance(ep_raw, dict) else (json.loads(ep_raw) if ep_raw else {})
            for item in ep.get("evidence_items", []):
                if item.get("publication_use") == "PUBLISH" and item.get("kind") != "resident_question":
                    if item.get("evidence_id"):
                        sealed_refs.add(item["evidence_id"])
                    for fid in item.get("source_fragment_ids", []):
                        if fid in fid_cache:
                            fix_id, fp = fid_cache[fid]
                            sealed_fragment_ids.add(fix_id)
                            sealed_fingerprints.add(fp)

    # 6. Plan and Final stages from publication metadata
    cur = await conn.execute(
        """
        SELECT id, publication_type, title, lead, body, metadata
        FROM publications
        WHERE publication_run_id = %s
        """,
        (run_id,),
    )
    pub_row = await cur.fetchone()

    audit_errors: list[str] = []
    article_plan_story_ids: list[str] = []
    digest_plan_story_ids: list[str] = []
    plan_refs: set[str] = set()
    plan_fragment_ids: set[str] = set()
    article_claim_trace: list[dict[str, Any]] = []
    digest_coverage_trace: list[dict[str, Any]] = []
    final_trace_supports: set[str] = set()
    final_trace_refs: set[str] = set()
    final_trace_fragment_ids: set[str] = set()
    final_trace_units: list[dict[str, Any]] = []

    if pub_row is not None:
        meta = pub_row[5] if isinstance(pub_row[5], dict) else (json.loads(pub_row[5]) if pub_row[5] else {})

        # Plan stage
        if pub_type in ("digest", "digest_grouped", "digest_channel"):
            dpp = meta.get("digest_presentation_plan")
            if dpp and isinstance(dpp, dict):
                digest_plan_story_ids = list(dpp.get("story_ids", []))
                all_plan_support_ids: list[str] = []
                for s in dpp.get("stories", []):
                    all_plan_support_ids.extend(s.get("detail_support_ids", []))
                for g in dpp.get("city_situation_groups", []):
                    all_plan_support_ids.extend(g.get("cited_support_ids", []))

                plan_fids: list[int] = []
                for sup_id in all_plan_support_ids:
                    plan_refs.add(sup_id)
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None:
                        plan_fids.append(fid)
                await _resolve_fragment_identities(conn, plan_fids, fid_cache)
                for fid in plan_fids:
                    if fid in fid_cache:
                        fix_id, _ = fid_cache[fid]
                        plan_fragment_ids.add(fix_id)
            else:
                audit_errors.append("MISSING_DIGEST_PRESENTATION_PLAN_METADATA")

        elif pub_type in ("article", "article_city_life"):
            acp = meta.get("article_coverage_plan")
            if acp and isinstance(acp, dict):
                article_plan_story_ids = list(acp.get("story_ids", []))
            else:
                article_plan_story_ids = list(sealed_story_ids)

        # Final trace stage
        if "digest_coverage_trace" in meta:
            dct = meta["digest_coverage_trace"]
            trace_stories = dct.get("stories", []) if isinstance(dct, dict) else (dct if isinstance(dct, list) else [])
            digest_coverage_trace = trace_stories

            trace_fids: list[int] = []
            for ts in trace_stories:
                for sup_id in list(ts.get("dashboard_support_ids", [])) + list(ts.get("detail_support_ids", [])):
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None:
                        trace_fids.append(fid)
            await _resolve_fragment_identities(conn, trace_fids, fid_cache)

            for ts in trace_stories:
                dash_texts = ts.get("dashboard_texts", [])
                detail_texts = ts.get("detail_texts", [])
                dash_sups = ts.get("dashboard_support_ids", [])
                detail_sups = ts.get("detail_support_ids", [])

                dash_fids_set: list[str] = []
                dash_fps_set: list[str] = []
                for sup_id in dash_sups:
                    final_trace_supports.add(sup_id)
                    final_trace_refs.add(sup_id)
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None and fid in fid_cache:
                        fix_id, fp = fid_cache[fid]
                        dash_fids_set.append(fix_id)
                        dash_fps_set.append(fp)
                        final_trace_fragment_ids.add(fix_id)

                detail_fids_set: list[str] = []
                detail_fps_set: list[str] = []
                for sup_id in detail_sups:
                    final_trace_supports.add(sup_id)
                    final_trace_refs.add(sup_id)
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None and fid in fid_cache:
                        fix_id, fp = fid_cache[fid]
                        detail_fids_set.append(fix_id)
                        detail_fps_set.append(fp)
                        final_trace_fragment_ids.add(fix_id)

                for dt_text in dash_texts:
                    final_trace_units.append(
                        {
                            "text": dt_text,
                            "fixture_fragment_ids": dash_fids_set,
                            "source_fingerprints": dash_fps_set,
                            "source_refs": list(dash_sups),
                        }
                    )
                for det_text in detail_texts:
                    final_trace_units.append(
                        {
                            "text": det_text,
                            "fixture_fragment_ids": detail_fids_set,
                            "source_fingerprints": detail_fps_set,
                            "source_refs": list(detail_sups),
                        }
                    )

        if "article_claim_trace" in meta:
            act = meta["article_claim_trace"]
            units = act.get("units", []) if isinstance(act, dict) else (act if isinstance(act, list) else [])
            article_claim_trace = units
            trace_fids = []
            for u in units:
                for sup_id in u.get("support_ids", []):
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None:
                        trace_fids.append(fid)
            await _resolve_fragment_identities(conn, trace_fids, fid_cache)

            for u in units:
                text = u.get("assertion_text") or u.get("text", "")
                sups = u.get("support_ids", [])
                u_fids: list[str] = []
                u_fps: list[str] = []
                for sup_id in sups:
                    final_trace_supports.add(sup_id)
                    final_trace_refs.add(sup_id)
                    fid = _extract_frag_id_from_support_id(sup_id)
                    if fid is not None and fid in fid_cache:
                        fix_id, fp = fid_cache[fid]
                        u_fids.append(fix_id)
                        u_fps.append(fp)
                        final_trace_fragment_ids.add(fix_id)
                final_trace_units.append(
                    {
                        "text": text,
                        "fixture_fragment_ids": u_fids,
                        "source_fingerprints": u_fps,
                        "source_refs": list(sups),
                    }
                )

    return build_export_payload(
        run=run_dict,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        source_corpus=source_corpus,
        source_fingerprints=source_fingerprints,
        source_refs=source_refs,
        source_fragment_ids=source_fragment_ids,
        evidence_fingerprints=list(evidence_fingerprints),
        evidence_refs=list(evidence_refs),
        evidence_fragment_ids=list(evidence_fragment_ids),
        publish_evidence=publish_evidence,
        candidate_refs=list(candidate_refs),
        candidate_fragment_ids=list(candidate_fragment_ids),
        candidate_fingerprints=list(candidate_fingerprints),
        candidates=candidates,
        sealed_story_ids=sealed_story_ids,
        sealed_refs=list(sealed_refs),
        sealed_fragment_ids=list(sealed_fragment_ids),
        article_plan_story_ids=article_plan_story_ids,
        digest_plan_story_ids=digest_plan_story_ids,
        plan_refs=list(plan_refs),
        plan_fragment_ids=list(plan_fragment_ids),
        article_claim_trace=article_claim_trace,
        digest_coverage_trace=digest_coverage_trace,
        final_trace_supports=list(final_trace_supports),
        final_trace_refs=list(final_trace_refs),
        final_trace_fragment_ids=list(final_trace_fragment_ids),
        final_trace_units=final_trace_units,
        audit_errors=audit_errors if audit_errors else None,
    )


async def main_async(args: argparse.Namespace) -> None:
    db_url = (
        args.database_url
        or os.environ.get("DATABASE_URL")
        or os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
        or "postgresql://localhost:5432/telebrief"
    )
    async with await AsyncConnection.connect(db_url) as conn:
        if args.source_only:
            if not args.edition:
                raise ValueError("--edition is required for --source-only")
            if not args.window_start or not args.window_end:
                raise ValueError("--window-start and --window-end are required for --source-only")
            corpus = await export_source_corpus_for_window(
                conn,
                edition_slug=args.edition,
                window_start=args.window_start,
                window_end=args.window_end,
            )
            payload = {
                "edition": args.edition,
                "window_start": args.window_start,
                "window_end": args.window_end,
                "source_corpus": corpus,
            }
        else:
            if args.run_id is None:
                raise ValueError("--run-id is required unless --source-only is used")
            payload = await export_publication_case(conn, args.run_id)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Exported publication case to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export frozen publication regression case snapshot")
    parser.add_argument("--run-id", type=int, default=None, help="Publication run ID")
    parser.add_argument("--source-only", action="store_true", help="Export source corpus only")
    parser.add_argument("--edition", type=str, default=None, help="Edition slug")
    parser.add_argument("--window-start", type=str, default=None, help="Window start ISO timestamp")
    parser.add_argument("--window-end", type=str, default=None, help="Window end ISO timestamp")
    parser.add_argument("--database-url", type=str, default=None, help="Database connection URL")
    parser.add_argument("--output", type=str, required=True, help="Output JSON path")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
