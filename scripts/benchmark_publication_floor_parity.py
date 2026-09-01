"""Benchmark publication floor parity: evaluate Event-First runs against frozen legacy floors."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import AsyncConnection

from scripts.export_publication_regression_case import export_publication_case
from scripts.publication_regression import (
    LegacyCoverageCase,
    LegacyRegressionReport,
    evaluate_case,
)


def format_report_summary(report: LegacyRegressionReport, case: LegacyCoverageCase) -> str:
    lines = [
        "=" * 70,
        f"LEGACY FLOOR PARITY REPORT: {case.id}",
        f"Edition: {case.edition_slug} | Type: {case.publication_type} | Commit: {case.legacy_commit}",
        "=" * 70,
        f"Legacy Floor Coverage:        {report.legacy_floor_coverage * 100:.1f}%",
        f"Legacy Microdetail Retention:  {report.legacy_microdetail_retention * 100:.1f}%",
        f"Regressions:                  {len(report.regression_unit_ids)} units",
        "-" * 70,
        "UNIT BREAKDOWN:",
    ]
    for u in report.units:
        status = "PASSED" if u.loss.value == "COVERED" else (f"ALLOWED_LOSS ({u.allowed_loss_reason.value})" if u.allowed_loss_reason else f"FAILED ({u.loss.value})")
        lines.append(f"  • [{status}] {u.unit_id}")
        if u.retained_microdetails:
            lines.append(f"      Retained: {', '.join(u.retained_microdetails)}")
        if u.missing_microdetails:
            lines.append(f"      Missing:  {', '.join(u.missing_microdetails)}")
    lines.append("=" * 70)
    return "\n".join(lines)


async def run_benchmark(args: argparse.Namespace) -> int:
    case_path = Path(args.case)
    if not case_path.exists():
        raise FileNotFoundError(f"Case file not found: {case_path}")
    case = LegacyCoverageCase.load_json(case_path)

    if args.export:
        export_path = Path(args.export)
        if not export_path.exists():
            raise FileNotFoundError(f"Export file not found: {export_path}")
        with open(export_path, "r", encoding="utf-8") as f:
            exported_payload = json.load(f)
    elif args.run_id is not None:
        db_url = (
            args.database_url
            or os.environ.get("DATABASE_URL")
            or os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
            or "postgresql://localhost:5432/telebrief"
        )
        async with await AsyncConnection.connect(db_url) as conn:
            exported_payload = await export_publication_case(conn, args.run_id)
    else:
        raise ValueError("Must provide either --export <path> or --run-id <id>")

    report = evaluate_case(case, exported_payload)
    print(format_report_summary(report, case))

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"Saved parity report to {out_path}")

    if report.legacy_floor_coverage < 1.0 or report.legacy_microdetail_retention < 1.0 or report.regression_unit_ids:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Event-First publications against legacy floor fixtures")
    parser.add_argument("--case", type=str, required=True, help="Legacy floor case JSON path")
    parser.add_argument("--export", type=str, default=None, help="Exported publication case JSON path")
    parser.add_argument("--run-id", type=int, default=None, help="Database publication run ID")
    parser.add_argument("--database-url", type=str, default=None, help="Database connection URL")
    parser.add_argument("--output", type=str, default=None, help="Output report JSON path")
    args = parser.parse_args()
    exit_code = asyncio.run(run_benchmark(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
