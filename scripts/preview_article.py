"""Generate an article preview without delivery side effects.

Usage:
    python scripts/preview_article.py [--edition berdyansk] [--hours 24]
        [--date YYYY-MM-DD] [--output PATH]
    python scripts/preview_article.py --run-id ID [--output PATH]

Ordinary preview mode uses the Event-First snapshot and selection pipeline.
Frozen-run mode replays only the sealed inputs from an existing article run.
Neither mode delivers the article.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.article_preview import (
    ArticleRunPreviewOutcome,
    build_article_preview_from_run,
)
from src.publication.facade import build_publication_preview
from src.runtime import install_runtime
from src.timezones import get_timezone, normalize_timezone_name


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a daily Event-First article preview without delivery."
    )
    parser.add_argument("--edition", default=None, help="Edition slug")
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Replay the sealed inputs of an existing article publication run",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Lookback window in hours (default: 24)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Snapshot date in the edition timezone (YYYY-MM-DD, at 20:00)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local path for the Markdown preview",
    )
    parser.add_argument(
        "--diagnostics-output",
        type=Path,
        default=None,
        help="Optional local path for compact generation diagnostics JSON (frozen-run mode)",
    )
    args = parser.parse_args(argv)
    if args.run_id is not None and (args.date is not None or args.hours is not None):
        parser.error("--run-id cannot be combined with --date or --hours")
    if args.run_id is not None and args.run_id <= 0:
        parser.error("--run-id must be a positive integer")
    if args.run_id is None and args.diagnostics_output is not None:
        parser.error("--diagnostics-output is available only with --run-id")
    if (
        args.run_id is not None
        and args.output is not None
        and args.diagnostics_output is not None
        and args.output.resolve() == args.diagnostics_output.resolve()
    ):
        parser.error("--output and --diagnostics-output must use different paths")
    return args


def _snapshot_at(date_value: str | None, timezone: str) -> dt.datetime | None:
    if date_value is None:
        return None
    parsed = dt.date.fromisoformat(date_value)
    canonical_timezone = normalize_timezone_name(timezone)
    return dt.datetime(
        parsed.year,
        parsed.month,
        parsed.day,
        20,
        0,
        tzinfo=get_timezone(canonical_timezone),
    )


async def main() -> None:
    args = parse_args()
    if args.run_id is not None:
        await _run_frozen_run_preview(args)
        return

    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    try:
        edition_slug = args.edition or "berdyansk"
        snapshot_at = None
        if args.date is not None:
            edition_timezone = await _load_edition_timezone(infra, edition_slug)
            snapshot_at = _snapshot_at(args.date, edition_timezone)
        publication_preview = await build_publication_preview(
            publication_type="daily_article",
            edition_slug=edition_slug,
            snapshot_at=snapshot_at,
            lookback_hours=args.hours if args.hours is not None else 24,
            config=config,
        )
        parts = [
            f"# {publication_preview.title}" if publication_preview.title else "# Вечерняя статья"
        ]
        if publication_preview.lead and not (
            publication_preview.body
            and publication_preview.body.startswith(publication_preview.lead)
        ):
            parts.extend(["", publication_preview.lead])
        if publication_preview.body:
            parts.extend(["", publication_preview.body])
        article = "\n".join(parts).rstrip() + "\n"

        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(article, encoding="utf-8")
            print(f"Preview saved to {args.output}")
        else:
            print(article, end="")
    finally:
        await infra.close()


async def _run_frozen_run_preview(args: argparse.Namespace) -> None:
    run_id = int(args.run_id)
    infra = None
    outcome: ArticleRunPreviewOutcome | None = None
    try:
        config = load_config()
        infra = await build_infrastructure(config.database)
        install_runtime(infra)
        outcome = await build_article_preview_from_run(
            run_id,
            config=config,
            expected_edition_slug=args.edition,
        )
    except Exception as exc:
        outcome = _failed_no_draft_outcome(run_id, exc)
    finally:
        if infra is not None:
            try:
                await infra.close()
            except Exception as exc:
                if outcome is None:
                    outcome = _failed_no_draft_outcome(run_id, exc)
                else:
                    diagnostics = dict(outcome.diagnostics)
                    diagnostics["cleanup_failure"] = {"exception_type": type(exc).__name__}
                    status = "failed" if outcome.status == "accepted" else outcome.status
                    if status == "failed":
                        diagnostics["failure"] = {"exception_type": type(exc).__name__}
                    outcome = ArticleRunPreviewOutcome(
                        status=status,
                        candidate=outcome.candidate,
                        diagnostics=diagnostics,
                        error=outcome.error or exc,
                    )

    if outcome is None:
        outcome = _failed_no_draft_outcome(run_id, RuntimeError("preview did not return"))

    article = _render_frozen_run_outcome(outcome)
    if args.diagnostics_output is not None:
        args.diagnostics_output.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics_output.write_text(
            json.dumps(outcome.diagnostics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Diagnostics saved to {args.diagnostics_output}", file=sys.stderr)
    elif outcome.status != "accepted":
        print(json.dumps(outcome.diagnostics, ensure_ascii=False, indent=2), file=sys.stderr)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(article, encoding="utf-8")
        print(f"Preview saved to {args.output}")
    else:
        print(article, end="")

    if outcome.status != "accepted":
        failure = outcome.diagnostics.get("failure")
        reason = (
            str(failure.get("reason") or failure.get("exception_type") or "unknown")
            if isinstance(failure, dict)
            else "unknown"
        )
        print(f"Frozen article preview {outcome.status}: {reason}", file=sys.stderr)
        raise SystemExit(1)


def _failed_no_draft_outcome(
    run_id: int,
    error: Exception,
) -> ArticleRunPreviewOutcome:
    return ArticleRunPreviewOutcome(
        status="failed",
        candidate=None,
        diagnostics={
            "schema_version": "article-run-preview-v3",
            "status": "failed",
            "run_id": run_id,
            "failure": {"exception_type": type(error).__name__},
        },
        error=error,
    )


def _render_frozen_run_outcome(outcome: ArticleRunPreviewOutcome) -> str:
    """Label nonaccepted candidate prose and explain when no draft was captured."""
    if outcome.status == "accepted" and outcome.candidate is not None:
        return outcome.candidate.markdown

    label = (
        "REJECTED PREVIEW — DO NOT PUBLISH"
        if outcome.status == "rejected"
        else "FAILED PREVIEW — DO NOT PUBLISH"
    )
    if outcome.candidate is None:
        return (
            f"# {label}\n\n"
            "No draft candidate was produced for this frozen run. See the diagnostics output.\n"
        )
    assessment_note = (
        "The latest candidate has not been assessed. See the last assessed checkpoint in diagnostics.\n\n"
        if outcome.candidate.assessment is None
        else ""
    )
    return f"# {label}\n\n{assessment_note}{outcome.candidate.markdown.lstrip()}"


async def _load_edition_timezone(infra: object, edition_slug: str) -> str:
    """Read the selected edition's timezone for explicit local-date previews."""
    uow = getattr(infra, "uow", None)
    if uow is None:
        raise ValueError("edition timezone lookup requires database infrastructure")
    async with uow.transaction() as conn:
        cursor = await conn.execute(
            "SELECT timezone FROM editions WHERE slug = %s",
            (edition_slug,),
        )
        row = await cursor.fetchone()
    if row is None or not row[0]:
        raise ValueError(f"edition {edition_slug!r} has no configured timezone")
    timezone_name = normalize_timezone_name(str(row[0]).strip())
    try:
        get_timezone(timezone_name)
    except ValueError as exc:
        raise ValueError(
            f"edition {edition_slug!r} has invalid timezone {timezone_name!r}"
        ) from exc
    return timezone_name


if __name__ == "__main__":
    asyncio.run(main())
