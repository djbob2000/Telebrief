"""Find which request parameter makes Haiku return 400 (scratch, one batch, read-only DB)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, "/work")
sys.path.insert(0, "/app")

from run_sample import BATCH, build_prompt, load_sample  # noqa: E402

from src.config_loader import load_config  # noqa: E402
from src.processing.event_triage import (  # noqa: E402
    _GATE_V2_SYSTEM_PROMPT,
    build_gate_response_format,
)
from src.processing.providers import resolve_processing_provider  # noqa: E402

MODEL = "anthropic/claude-haiku-5.5"
logger = logging.getLogger("debug_haiku")


def chain(exc: BaseException) -> str:
    parts = []
    cur: BaseException | None = exc
    while cur is not None:
        body = getattr(cur, "body", None) or getattr(getattr(cur, "response", None), "text", None)
        parts.append(f"{type(cur).__name__}: {str(cur)[:300]} body={str(body)[:500]}")
        cur = cur.__cause__ or cur.__context__
    return " | ".join(parts)


async def main() -> None:
    config = load_config()
    stories, frags, contract = await load_sample(config, os.environ["DATABASE_URL"])
    batch = stories[:BATCH]
    prompt = build_prompt(contract, batch, frags)
    config.openrouter_processing_model = MODEL
    provider = resolve_processing_provider(None, config, logger)
    ids = [sid for sid, _ in batch]
    variants = {
        "as_harness": dict(reasoning_effort="low", response_format=build_gate_response_format(len(ids))),
        "no_reasoning": dict(reasoning_effort=None, response_format=build_gate_response_format(len(ids))),
        "no_response_format": dict(reasoning_effort="low", response_format=None),
        "neither": dict(reasoning_effort=None, response_format=None),
    }
    for name, extra in variants.items():
        try:
            raw = await provider.chat_completion(
                messages=[
                    {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=MODEL,
                temperature=0.0,
                max_tokens=12_288,
                **extra,
            )
            print(f"{name}: OK len={len(raw)} head={raw[:120]!r}")
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(f"{name}: FAIL {chain(exc)}")


if __name__ == "__main__":
    asyncio.run(main())
