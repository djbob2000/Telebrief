"""Test Gate V2 on a live batch of 128 real stories from PostgreSQL using DeepSeek-V4-Flash."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import psycopg
from src.ai_providers import create_provider
from src.config_loader import load_config
from src.processing.event_triage import _GATE_V2_SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_batch_128")


def build_gate_prompt(stories: list[tuple[int, str]]) -> str:
    lines = [
        "GEOGRAPHIC FOCUS CONTRACT FOR BERDYANSK:",
        "- Edition Focus Area: Berdyansk and immediate settlements of Berdyansk district (Osypenko, Novovasylivka, Azovske, Dmytrivka, Troyany, Andrivka, Berestove).",
        "- Events outside this focus area are OUT_OF_SCOPE unless direct concrete consequence in Berdyansk is explicitly stated.",
        "",
        "Stories for Gate V2 triage and brief synthesis:",
    ]
    for sid, text in stories:
        clean_txt = " ".join(text.split())[:350]
        lines.append(f"Story #{sid} (fragments=1, sources=1):\n- [frag={sid} time=2026-09-01T12:00:00 source=telegram] {clean_txt}\n")
    return "\n".join(lines)


async def main():
    cfg = load_config()
    model = "deepseek/deepseek-v4-flash-0731:floor"
    provider = create_provider(
        "openrouter",
        logger=logger,
        openrouter_api_key=cfg.openrouter_api_key,
        openrouter_base_url="https://openrouter.ai/api/v1",
    )

    print("=" * 80)
    print("📥 Loading 128 real stories from PostgreSQL database...")
    print("=" * 80)

    stories: list[tuple[int, str]] = []
    async with await psycopg.AsyncConnection.connect("dbname=telebrief user=air") as conn:
        cur = await conn.execute("""
            SELECT DISTINCT sfrag.story_id, sf.text_content
            FROM story_fragments sfrag
            JOIN source_fragments sf ON sf.id = sfrag.fragment_id
            WHERE sf.text_content IS NOT NULL AND length(trim(sf.text_content)) > 3
            ORDER BY sfrag.story_id DESC
            LIMIT 128
        """)
        async for sid, txt in cur:
            stories.append((sid, txt))

    if len(stories) < 128:
        print(f"Warning: Only found {len(stories)} stories in DB, topping up from benchmark...")
        from scripts.benchmark_gate_models import BENCHMARK_STORIES
        for b in BENCHMARK_STORIES:
            if len(stories) >= 128:
                break
            stories.append((b.id + 10000, b.text))

    print(f"Loaded {len(stories)} stories for Batch 128 test.")
    prompt = build_gate_prompt(stories)
    prompt_tokens_est = len(prompt) // 4
    print(f"Estimated prompt size: {len(prompt)} characters (~{prompt_tokens_est} tokens).")

    print("\n" + "=" * 80)
    print(f"🚀 Sending Batch 128 request to {model}...")
    print("=" * 80)

    t0 = time.perf_counter()
    try:
        raw_res = await provider.chat_completion(
            messages=[
                {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.0,
            max_tokens=65536,
            response_format={"type": "json_object"},
        )
        elapsed = time.perf_counter() - t0
        print(f"✅ Response received in {elapsed:.2f} seconds ({elapsed / len(stories):.2f}s per story)!")

        # Parse JSON
        parsed = json.loads(raw_res)
        results = parsed.get("results") or []
        print(f"📊 Parsed {len(results)} / {len(stories)} story decisions from JSON.")

        # Aggregate statistics
        scope_counts: dict[str, int] = {}
        retention_counts: dict[str, int] = {}
        exclusion_counts: dict[str, int] = {}
        kept_samples: list[dict[str, Any]] = []
        dropped_samples: list[dict[str, Any]] = []

        results_by_id = {int(r["story_id"]): r for r in results if "story_id" in r}

        for sid, txt in stories:
            res = results_by_id.get(sid)
            if not res:
                continue
            sc = res.get("scope", "UNKNOWN")
            ret = res.get("retention", "UNKNOWN")
            excl = res.get("exclusion_reason")

            scope_counts[sc] = scope_counts.get(sc, 0) + 1
            retention_counts[ret] = retention_counts.get(ret, 0) + 1
            if excl:
                exclusion_counts[excl] = exclusion_counts.get(excl, 0) + 1

            if ret == "KEEP" and len(kept_samples) < 5:
                kept_samples.append({
                    "id": sid,
                    "text": txt[:80],
                    "scope": sc,
                    "topic": (res.get("brief_payload") or {}).get("topic"),
                    "headline": (res.get("brief_payload") or {}).get("headline"),
                })
            elif ret == "DROP" and len(dropped_samples) < 5:
                dropped_samples.append({
                    "id": sid,
                    "text": txt[:80],
                    "scope": sc,
                    "exclusion_reason": excl,
                    "reason": res.get("reason"),
                })

        print("\n" + "─" * 80)
        print("📈 BATCH 128 SUMMARY STATISTICS:")
        print("─" * 80)
        print(f"Scope Distribution:     {scope_counts}")
        print(f"Retention Distribution: {retention_counts}")
        print(f"Exclusion Reasons:      {exclusion_counts}")
        print(f"Coverage completeness:  {len(results_by_id)}/{len(stories)} ({len(results_by_id) / len(stories) * 100:.1f}%)")

        print("\n🟢 Sample KEEP Stories:")
        for k in kept_samples:
            print(f"  • #{k['id']} [{k['scope']}] {k['topic']}: {k['headline']}")
            print(f"    Text: {k['text']!r}")

        print("\n🔴 Sample DROP Stories:")
        for d in dropped_samples:
            print(f"  • #{d['id']} [{d['scope']}] reason={d['exclusion_reason']}: {d['reason']}")
            print(f"    Text: {d['text']!r}")

        # Save to artifact
        artifact_path = Path("/Users/air/.gemini/antigravity-ide/brain/a331c9d6-a80c-4911-ae2b-d379e7ab4cf4/batch_128_live_results.json")
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump({
                "elapsed_seconds": elapsed,
                "stories_count": len(stories),
                "parsed_count": len(results),
                "scope_counts": scope_counts,
                "retention_counts": retention_counts,
                "exclusion_counts": exclusion_counts,
                "results": results,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Full results saved to {artifact_path}")

    except Exception as exc:
        print(f"❌ Execution failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
