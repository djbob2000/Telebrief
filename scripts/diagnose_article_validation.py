"""Diagnostic script to inspect the last rejected article draft and analyze each validation issue."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config
from src.publication.article_claim_support import assess_claim_against_supports
from src.publication.article_context import build_article_editorial_context
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_validator import (
    find_unsupported_claims,
    validate_article_draft,
)
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.repository import PublicationRepository
from src.bootstrap import build_infrastructure
from src.runtime import install_runtime



async def main():
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    repo = PublicationRepository()

    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT id, publication_run_id, kind, status, error_kind, metadata
            FROM publication_generation_attempts
            ORDER BY id DESC
            LIMIT 10
            """
        )
        rows = await cur.fetchall()
        if not rows:
            print("No attempts found in publication_generation_attempts.")
            return

        for r in rows:
            print(f"Attempt ID={r[0]}, Run ID={r[1]}, Kind={r[2]}, Status={r[3]}, Error={r[4]}")

        row = rows[0]
        run_id = row[1]
        metadata = row[5] or {}

        print(f"================================================================================")
        print(f"🔍 INSPECTING REJECTED ARTICLE ATTEMPT (Attempt ID: {row[0]}, Run ID: {run_id})")
        print(f"Status: {row[3]} | Error Kind: {row[4]}")
        print(f"================================================================================\n")

        # Load run, selected inputs, and reconstruct context
        selected_inputs = await repo.load_sealed_inputs(conn, run_id)
        adapter = EventEditorialAdapter(uow=uow, repo=repo)
        editorial_input = await adapter.adapt_inputs_on(conn, run_id, inputs=selected_inputs)
        ctx = getattr(editorial_input.analysis, "article_context", None)
        if ctx is None:
            print("article_context is None in editorial_input.analysis")
            return



        draft_dict = metadata.get("raw_draft") or metadata.get("draft")
        if not draft_dict and "raw_response" in metadata:
            raw_resp = metadata["raw_response"]
            # try parse json
            try:
                cleaned = raw_resp.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
                    cleaned = cleaned.removesuffix("```").strip()
                draft_dict = json.loads(cleaned)
            except Exception as e:
                print(f"Could not parse raw_response: {e}")

        if not draft_dict:
            print("Could not find draft dictionary in attempt metadata. Keys available:", list(metadata.keys()))
            if "validation_issues" in metadata:
                print("\nSaved Validation Issues:")
                for iss in metadata["validation_issues"]:
                    print(" -", iss)
            return

        draft = StructuredArticleDraft.from_dict(draft_dict)

        editorial_config = getattr(config.settings, "publication_editorial", None)
        if editorial_config is None:
            from src.config_loader import PublicationEditorialConfig
            editorial_config = PublicationEditorialConfig()

        val_result = validate_article_draft(draft, ctx, editorial_config)

        print(f"Validation Result: is_valid={val_result.is_valid}")
        print(f"Total Issues: {len(val_result.issues)}\n")

        print("=" * 80)
        print("📝 GENERATED DRAFT CONTENT")
        print("=" * 80)
        print(f"TITLE: {draft.title}")
        print(f"  Title Support IDs: {draft.title_support_ids}")
        print(f"  Title Claims: {[c.text for c in draft.title_claims]}")
        print(f"\nLEAD: {draft.lead}")
        print(f"  Lead Support IDs: {draft.lead_support_ids}")
        print(f"  Lead Claims: {[c.text for c in draft.lead_claims]}")

        for s_idx, sec in enumerate(draft.sections, start=1):
            print(f"\nSECTION {s_idx}: {sec.heading}")
            print(f"  Heading Support IDs: {sec.heading_support_ids}")
            print(f"  Heading Claims: {[c.text for c in sec.heading_claims]}")
            for p_idx, para in enumerate(sec.paragraphs, start=1):
                print(f"  P{p_idx}: {para.text}")
                print(f"    Cited Support IDs: {para.cited_support_ids}")
                print(f"    Claims:")
                for c in para.claims:
                    print(f"      - Text: '{c.text}' | Supports: {c.cited_support_ids}")

        print("\n" + "=" * 80)
        print("🔬 DETAILED ISSUE-BY-ISSUE DIAGNOSTIC TRACE")
        print("=" * 80)

        for idx, iss in enumerate(val_result.issues, start=1):
            print(f"\n[{idx}] {iss.code} on Unit: {iss.unit_id} (Blocking: {iss.blocking}, Severity: {iss.severity})")
            print(f"    Message: {iss.message}")
            if iss.support_ids:
                print(f"    Cited Support IDs: {iss.support_ids}")
                c_sups = [ctx.support_by_id[sid] for sid in iss.support_ids if sid in ctx.support_by_id]
                for supp in c_sups:
                    print(f"      Support [{supp.support_id}]: text='{supp.text}'")
                    print(f"                                   source='{supp.source_text}'")
                    print(f"                                   kind='{supp.evidence_kind}', use='{supp.publication_use}'")
                for sid in iss.support_ids:
                    if sid not in ctx.support_by_id:
                        print(f"      Support [{sid}]: UNKNOWN ID")

            if iss.unsupported_claims:
                print(f"    Concrete Unsupported Claims: {iss.unsupported_claims}")


if __name__ == "__main__":
    asyncio.run(main())
