"""Generate explicitly requested offline comparisons; never deliver or write DB state."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.digest_evaluation.fixtures import load_digest_case  # noqa: E402
from scripts.digest_evaluation.replay import replay_digest, write_replay_result  # noqa: E402
from src.ai_providers import create_provider  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--variant",
        choices=("baseline", "compact", "thematic", "combined", "source_grouped"),
        default="baseline",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--review-clean-text",
        action="store_true",
        help="Offline experiment: edit even without findings",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 3:
        parser.error("--repeat must be between 1 and 3")
    return args


async def main() -> int:
    args = parse_args()
    case = load_digest_case(args.fixture)
    cfg = {
        **case.generation,
        "writer_material_format": (
            "source_grouped_v1"
            if args.variant == "source_grouped"
            else "compact_v1"
            if args.variant in ("compact", "combined")
            else "legacy"
        ),
        "review_without_findings": args.review_clean_text,
        "editor_scope": "thematic_blocks"
        if args.variant in ("thematic", "combined", "source_grouped")
        else "targeted_items",
    }
    # Explicitly configured credentials/models only; no infrastructure bootstrap.
    from dotenv import load_dotenv

    load_dotenv()
    cfg["model"] = cfg.get("model") or os.getenv("OPENROUTER_MODEL", "")
    if not cfg["model"]:
        raise ValueError("DIGEST_EVALUATION_MODEL_NOT_CONFIGURED")
    case = replace(case, generation=cfg)
    provider = create_provider(
        "openrouter",
        logging.getLogger(__name__),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", ""),
        openrouter_model_2=os.getenv("OPENROUTER_MODEL_2", ""),
    )
    from scripts.digest_evaluation.usage import instrument_usage, summarize_usage

    usage_records = instrument_usage(provider)
    source_root = Path(__file__).resolve().parent.parent
    implementation_hash = hashlib.sha256()
    for file in sorted((source_root / "src/publication").glob("digest_*.py")):
        implementation_hash.update(file.name.encode())
        implementation_hash.update(file.read_bytes())
    implementation_hash.update((source_root / "src/publication/generation.py").read_bytes())
    failed = False
    for repeat in range(1, args.repeat + 1):
        usage_records.clear()
        result = await replay_digest(case, provider=provider)
        result.diagnostics.update(summarize_usage(usage_records))
        result.diagnostics["cost_availability"] = (
            "provider_reported" if result.diagnostics["cost"] is not None else "not_available"
        )
        result.diagnostics["runtime_source_hash"] = implementation_hash.hexdigest()
        result.diagnostics["input_hash"] = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
        result.diagnostics["configured_model"] = cfg["model"]
        result.diagnostics.update(variant=args.variant, repeat=repeat)
        write_replay_result(result, args.output_dir / f"{case.name}-{args.variant}-{repeat}")
        print(
            f"{case.name} {args.variant} {repeat}: {result.status}; calls={result.diagnostics['provider_calls']}"
        )
        failed |= result.status != "accepted"
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
