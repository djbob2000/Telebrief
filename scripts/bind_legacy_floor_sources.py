"""Offline fixture-binding tool: binds semantic units to real raw source fragments."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import AsyncConnection

from scripts.export_publication_regression_case import export_source_corpus_for_window
from scripts.publication_regression import (
    LegacyCoverageCase,
    LegacyCoverageUnit,
    LegacySourceIdentity,
    normalize_microdetail_text,
)


def _matches_matcher_groups(text: str, groups: list[list[str]]) -> bool:
    normalized = normalize_microdetail_text(text)
    for group in groups:
        if all(normalize_microdetail_text(term) in normalized for term in group):
            return True
    return False


def bind_case_sources(
    case: LegacyCoverageCase,
    source_corpus: Sequence[dict[str, Any]],
    matchers: dict[str, list[list[str]]],
) -> tuple[LegacyCoverageCase, dict[str, Any]]:
    """Bind semantic units in a legacy coverage case to matching source identities."""
    bound_units: list[LegacyCoverageUnit] = []
    report: dict[str, Any] = {
        "case_id": case.id,
        "total_source_corpus_count": len(source_corpus),
        "unit_bindings": {},
    }

    for unit in case.coverage_units:
        if unit.allowed_loss_reason is not None:
            bound_units.append(unit)
            report["unit_bindings"][unit.id] = {
                "allowed_loss_reason": unit.allowed_loss_reason.value,
                "matched_count": 0,
                "sources": [],
            }
            continue

        unit_matchers = matchers.get(unit.id, [])
        if not unit_matchers:
            raise ValueError(f"No matchers defined for binding unit '{unit.id}'")

        matched_sources: list[LegacySourceIdentity] = []
        matched_details: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()

        for src in source_corpus:
            text = src.get("text", "")
            if _matches_matcher_groups(text, unit_matchers):
                fix_id = src.get("fixture_fragment_id")
                fp = src.get("source_fingerprint")
                key = (str(fix_id), str(fp))
                if key not in seen_keys:
                    seen_keys.add(key)
                    matched_sources.append(
                        LegacySourceIdentity(
                            fixture_fragment_id=fix_id,
                            source_fingerprint=fp,
                        )
                    )
                    matched_details.append(
                        {
                            "fixture_fragment_id": fix_id,
                            "source_fingerprint": fp,
                            "text": text,
                            "observed_at": src.get("observed_at"),
                        }
                    )

        if not matched_sources:
            raise ValueError(
                f"Zero source matches for binding unit '{unit.id}' in corpus ({len(source_corpus)} items)"
            )

        bound_units.append(
            LegacyCoverageUnit(
                id=unit.id,
                description=unit.description,
                acceptable_sources=tuple(matched_sources),
                required_microdetails=unit.required_microdetails,
                allowed_loss_reason=unit.allowed_loss_reason,
            )
        )
        report["unit_bindings"][unit.id] = {
            "matched_count": len(matched_sources),
            "sources": matched_details,
        }

    bound_case = LegacyCoverageCase(
        id=case.id,
        legacy_commit=case.legacy_commit,
        edition_slug=case.edition_slug,
        publication_type=case.publication_type,
        snapshot_at=case.snapshot_at,
        lookback_hours=case.lookback_hours,
        window_start=case.window_start,
        window_end=case.window_end,
        coverage_units=tuple(bound_units),
    )
    return bound_case, report


async def main_async(args: argparse.Namespace) -> None:
    case_path = Path(args.case)
    case = LegacyCoverageCase.load_json(case_path)

    matchers_path = Path(args.matchers)
    with open(matchers_path, "r", encoding="utf-8") as f:
        matchers = json.load(f)

    if args.source_export:
        with open(args.source_export, "r", encoding="utf-8") as f:
            data = json.load(f)
        source_corpus = data.get("source_corpus", [])
    elif args.use_case_window:
        db_url = (
            args.database_url
            or os.environ.get("DATABASE_URL")
            or os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
            or "postgresql://localhost:5432/telebrief"
        )
        edition = args.edition or case.edition_slug
        async with await AsyncConnection.connect(db_url) as conn:
            source_corpus = await export_source_corpus_for_window(
                conn,
                edition_slug=edition,
                window_start=case.window_start,
                window_end=case.window_end,
            )
    else:
        raise ValueError("Must provide either --source-export or --use-case-window")

    bound_case, report = bind_case_sources(case, source_corpus, matchers)

    out_path = Path(args.output or args.case)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bound_case.to_dict(), f, ensure_ascii=False, indent=2)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"Bound {len(bound_case.coverage_units)} units ({len(report['unit_bindings'])} active) to {out_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind legacy coverage floor units to real source fragments")
    parser.add_argument("--case", type=str, required=True, help="Input legacy floor JSON")
    parser.add_argument("--matchers", type=str, required=True, help="Input source matchers JSON")
    parser.add_argument("--source-export", type=str, default=None, help="Input exported source corpus JSON")
    parser.add_argument("--use-case-window", action="store_true", help="Fetch corpus from DB using case window")
    parser.add_argument("--edition", type=str, default=None, help="Edition slug override")
    parser.add_argument("--database-url", type=str, default=None, help="Database connection URL")
    parser.add_argument("--output", type=str, default=None, help="Output bound case JSON (defaults to overwriting --case)")
    parser.add_argument("--report", type=str, default=None, help="Output review report JSON")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
