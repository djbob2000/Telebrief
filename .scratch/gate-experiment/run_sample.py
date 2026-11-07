"""Small Gate V2 model comparison on a stratified frozen sample (scratch, not committed).

Runs inside a one-off app container. The database session is read-only and no
decision is written. Models are called through the production processing
provider, so the runtime allowlist applies to them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg  # noqa: E402

from src.config_loader import load_config  # noqa: E402
from src.domain.edition_geography import resolve_edition_geography  # noqa: E402
from src.processing.edition_scope import build_scope_contract, resolve_edition_scope  # noqa: E402
from src.processing.event_triage import (  # noqa: E402
    _GATE_V2_SYSTEM_PROMPT,
    build_gate_response_format,
    format_gate_fragment_excerpt,
)
from src.processing.providers import resolve_processing_provider  # noqa: E402

EDITION_ID = 1
PER_LABEL = 30
BATCH = 10
MAX_FRAGMENTS = 6
EXCERPT_CHARS = 320
SEED = "gate-sample-2026-11-02"
MODELS = ["anthropic/claude-haiku-5.5"]
OUT = Path("/work")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("gate_sample")

SAMPLE_SQL = """
WITH last AS (
    SELECT DISTINCT ON (story_id) story_id, retention
    FROM story_event_triage_decisions
    WHERE created_at > now() - interval '7 days'
    ORDER BY story_id, created_at DESC
), ed AS (
    SELECT l.story_id, l.retention
    FROM last l
    JOIN stories st ON st.id = l.story_id
    WHERE st.edition_id = %s
)
SELECT story_id, retention FROM (
    SELECT story_id, retention,
           row_number() OVER (PARTITION BY retention ORDER BY md5(story_id::text || %s)) AS rn
    FROM ed
) picked
WHERE rn <= %s
ORDER BY retention, story_id
"""

FRAGMENT_SQL = """
SELECT sf.story_id, f.id, f.text_content, s.name,
       COALESCE(s.role, s.kind, 'unknown'),
       COALESCE(si.published_at, si.first_collected_at, f.created_at)
FROM story_fragments sf
JOIN source_fragments f ON f.id = sf.fragment_id
JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
JOIN source_items si ON si.id = sir.source_item_id
JOIN sources s ON s.id = si.source_id
WHERE sf.story_id = ANY(%s)
ORDER BY sf.story_id, sf.id DESC
"""


async def load_sample(config, conn_str: str):
    async with await psycopg.AsyncConnection.connect(
        conn_str, options="-c default_transaction_read_only=on"
    ) as conn:
        cur = await conn.execute(SAMPLE_SQL, (EDITION_ID, SEED, PER_LABEL))
        stories = await cur.fetchall()
        story_ids = [int(s[0]) for s in stories]
        frags: dict[int, list[dict]] = {sid: [] for sid in story_ids}
        cur = await conn.execute(FRAGMENT_SQL, (story_ids,))
        for sid, fid, text, src_name, src_role, observed in await cur.fetchall():
            if len(frags[int(sid)]) >= MAX_FRAGMENTS:
                continue
            frags[int(sid)].append(
                {
                    "fragment_id": int(fid),
                    "text": str(text or "")[:EXCERPT_CHARS],
                    "source_name": str(src_name),
                    "source_role": str(src_role),
                    "observed_at": observed,
                }
            )
        slug, scope = await resolve_edition_scope(conn, config, EDITION_ID)
    geo = resolve_edition_geography(slug, scope.name)
    contract = build_scope_contract(scope, geo)
    return [(int(s[0]), str(s[1])) for s in stories], frags, contract


def build_prompt(contract: str, batch: list[tuple[int, str]], frags: dict) -> str:
    lines = [
        contract,
        "",
        "Minimum confidence for a hard DROP: 0.95. "
        "Below this threshold, supply a faithful brief; keep background-only evidence CONTEXT.",
        "",
        "Stories for Gate V2 triage and brief synthesis:",
    ]
    for sid, _ in batch:
        excerpts = [format_gate_fragment_excerpt(f) for f in frags.get(sid, [])]
        body = "\n".join(excerpts) if excerpts else "(No text)"
        sources = len({f["source_name"] for f in frags.get(sid, [])})
        lines.append(f"Story #{sid} (fragments={len(excerpts)}, sources={sources}):\n{body}\n")
    ids = ", ".join(str(sid) for sid, _ in batch)
    lines.append(
        f"CRITICAL INSTRUCTION: Your JSON output must contain a 'results' array with EXACTLY "
        f"{len(batch)} elements, one for EACH of the requested stories ({ids}). "
        "Do NOT omit any story, do NOT stop after the first story."
    )
    return "\n".join(lines)


def parse_retention(raw: str, expected_ids: list[int]) -> dict[int, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(text)
    result: dict[int, str] = {}
    for item in data.get("results", []):
        sid = int(item.get("story_id"))
        retention = str(item.get("retention", "")).upper()
        if sid in expected_ids and retention in {"KEEP", "DROP"}:
            result[sid] = retention
    return result


async def run_model(model: str, config, batches, frags, contract) -> dict:
    config.openrouter_processing_model = model
    provider = resolve_processing_provider(None, config, logger)
    call_ok = 0
    decisions: dict[int, str] = {}
    errors: list[str] = []
    for batch in batches:
        ids = [sid for sid, _ in batch]
        prompt = build_prompt(contract, batch, frags)
        try:
            raw = await provider.chat_completion(
                messages=[
                    {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                temperature=0.0,
                max_tokens=12_288,
                reasoning_effort="low",
                response_format=(None if model.startswith("anthropic/") else build_gate_response_format(len(ids))),
            )
            parsed = parse_retention(raw, ids)
            if len(parsed) == len(ids):
                call_ok += 1
            decisions.update(parsed)
        except Exception as exc:  # noqa: BLE001 - diagnostic run, record and continue
            errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    return {
        "model": model,
        "batches": len(batches),
        "valid_batches": call_ok,
        "decisions": {str(k): v for k, v in decisions.items()},
        "errors": errors,
    }


def main() -> None:
    config = load_config()
    stories, frags, contract = asyncio.run(load_sample(config, os.environ["DATABASE_URL"]))
    reference = dict(stories)
    batches = [stories[i : i + BATCH] for i in range(0, len(stories), BATCH)]
    (OUT / "sample.json").write_text(
        json.dumps(
            {"reference": {str(k): v for k, v in reference.items()}},
            ensure_ascii=False,
            indent=2,
        )
    )
    results = []
    for model in MODELS:
        res = asyncio.run(run_model(model, config, batches, frags, contract))
        results.append(res)
        (OUT / f"result_{model.replace('/', '_').replace(':', '_')}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2)
        )

    for res in results:
        dec = {int(k): v for k, v in res["decisions"].items()}
        agree = sum(1 for sid, lab in reference.items() if dec.get(sid) == lab)
        keep_ref = [sid for sid, lab in reference.items() if lab == "KEEP"]
        keep_hit = sum(1 for sid in keep_ref if dec.get(sid) == "KEEP")
        print(
            f"{res['model']}: valid_batches={res['valid_batches']}/{res['batches']} "
            f"decided={len(dec)}/{len(reference)} agreement={agree}/{len(reference)} "
            f"KEEP_recall={keep_hit}/{len(keep_ref)} errors={res['errors']}"
        )


if __name__ == "__main__":
    main()
