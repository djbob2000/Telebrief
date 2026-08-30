"""Comprehensive breakdown of all validation issues for the rejected draft."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.article_claim_support import (
    assess_claim_against_supports,
    extract_content_stems,
)
from src.publication.article_claims import find_unsupported_claims, stem_word
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_validator import validate_article_draft
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.repository import PublicationRepository
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
            WHERE kind = 'writer' AND status = 'failed'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        if not row:
            print("No failed writer attempt found.")
            return

        run_id = row[1]
        metadata = row[5] or {}
        draft_dict = metadata.get("raw_draft")
        if not draft_dict:
            print("No raw_draft found in metadata.")
            return

        selected_inputs = await repo.load_sealed_inputs(conn, run_id)
        adapter = EventEditorialAdapter(uow=uow, repo=repo)
        editorial_input = await adapter.adapt_inputs_on(conn, run_id, inputs=selected_inputs)
        ctx = getattr(editorial_input.analysis, "article_context", None)
        if ctx is None:
            print("article_context is None")
            return

        draft = StructuredArticleDraft.from_dict(draft_dict)
        editorial_config = getattr(config.settings, "publication_editorial", None)
        val_result = validate_article_draft(draft, ctx, editorial_config)

        print("=" * 100)
        print(f"📊 FULL DRAFT & VALIDATION DIAGNOSTIC AUDIT (Run {run_id}, Attempt {row[0]})")
        print(f"Total validation issues: {len(val_result.issues)}")
        print("=" * 100)

        # Print full draft structure
        print("\n--- [DRAFT TEXT] ---")
        print(f"# {draft.title}")
        print(f"\n{draft.lead}")
        for s in draft.sections:
            print(f"\n## {s.heading}")
            for p in s.paragraphs:
                print(f"\n{p.text}")

        print("\n" + "=" * 100)
        print("🔍 CLAIM ATOM EVALUATION BREAKDOWN (BY UNIT)")
        print("=" * 100)

        units = []
        units.append(("TITLE", "title", draft.title, draft.title_support_ids, draft.title_claims))
        units.append(("LEAD", "lead", draft.lead, draft.lead_support_ids, draft.lead_claims))
        p_idx = 1
        for s_idx, sec in enumerate(draft.sections, start=1):
            units.append((f"H{s_idx:03d}", "heading", sec.heading, sec.heading_support_ids, sec.heading_claims))
            for p in sec.paragraphs:
                units.append((f"P{p_idx:03d}", "paragraph", p.text, p.cited_support_ids, p.claims))
                p_idx += 1

        for unit_id, unit_type, text, cited_ids, claims in units:
            print(f"\n================================================================================")
            print(f">>> UNIT: {unit_id} ({unit_type})")
            print(f"    Text: {text}")
            print(f"    Cited Support IDs: {cited_ids}")
            print(f"================================================================================")
            for c_idx, c in enumerate(claims, start=1):
                c_supports = [ctx.support_by_id[sid] for sid in c.cited_support_ids if sid in ctx.support_by_id]
                ass = assess_claim_against_supports(c.text, c_supports, min_content_coverage=0.70)

                claim_stems = extract_content_stems(c.text)
                supp_texts = [t for s in c_supports for t in (s.text, s.source_text) if t]
                supp_stems = set()
                for st in supp_texts:
                    supp_stems.update(extract_content_stems(st))

                matched = claim_stems.intersection(supp_stems)
                missing = claim_stems - supp_stems
                cov = len(matched) / len(claim_stems) if claim_stems else 1.0

                concrete = find_unsupported_claims(c.text, supp_texts)

                status_icon = "✅ PASS" if ass.supported else "❌ FAIL"
                print(f"  Claim [{c_idx}]: {status_icon} | Coverage: {cov:.1%} (matched {len(matched)}/{len(claim_stems)})")
                print(f"    Text: '{c.text}'")
                print(f"    Supports cited: {c.cited_support_ids}")
                print(f"    Missing stems: {sorted(missing)}")
                print(f"    Matched stems: {sorted(matched)}")
                if concrete:
                    print(f"    Concrete violations: {concrete}")
                for s in c_supports:
                    print(f"      -> Support {s.support_id} [kind={s.evidence_kind}]: fact='{s.text}' | source='{s.source_text}'")



if __name__ == "__main__":
    asyncio.run(main())
