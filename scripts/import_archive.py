#!/usr/bin/env python3
"""CLI utility to import historical news archives into Telebrief (JSON / JSONL / CSV).

Usage:
    python scripts/import_archive.py --file data/archive/berdyansk_2023.json --source "Бердянск 24"
    python scripts/import_archive.py --file data/archive/news.csv --source "Архив СМИ" --format csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import load_config  # noqa: E402
from src.db.pool import close_pool, open_pool  # noqa: E402
from src.db.uow import DatabaseUnitOfWork  # noqa: E402
from src.embedding_providers import create_embedding_provider  # noqa: E402
from src.repositories.archive import ArchiveRepository, compute_content_hash  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("import_archive")

_HTML_TAGS_RE = re.compile(r"<[^>]+>")
_MULTIPLE_SPACES_RE = re.compile(r"\s+")


def clean_text(raw: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not raw:
        return ""
    no_html = _HTML_TAGS_RE.sub(" ", raw)
    return _MULTIPLE_SPACES_RE.sub(" ", no_html).strip()


def parse_datetime(raw: Any) -> dt.datetime:
    """Parse various date/datetime representations into UTC datetime."""
    if isinstance(raw, dt.datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=dt.timezone.utc)
        return raw.astimezone(dt.timezone.utc)
    if isinstance(raw, (int, float)):
        return dt.datetime.fromtimestamp(raw, tz=dt.timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return dt.datetime.now(dt.timezone.utc)

    text = raw.strip()
    # Try ISO format
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        pass

    # Common formats
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

    logger.warning("Could not parse date '%s', using current UTC time", text)
    return dt.datetime.now(dt.timezone.utc)


def load_records_from_file(file_path: Path, file_format: str | None = None) -> list[dict[str, Any]]:
    """Load items from JSON, JSONL, or CSV file."""
    ext = file_format or file_path.suffix.lower().lstrip(".")
    records: list[dict[str, Any]] = []

    if ext == "json":
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict) and "articles" in data:
                records = data["articles"]
            elif isinstance(data, dict):
                records = [data]
    elif ext in ("jsonl", "ndjson"):
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    elif ext == "csv":
        with file_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            records = list(reader)
    else:
        raise ValueError(f"Unsupported file format: {ext}")

    return records


async def import_archive_file(
    file_path: Path,
    *,
    source_name: str,
    edition_slug: str = "berdyansk",
    generate_embeddings: bool = True,
    batch_size: int = 50,
    dry_run: bool = False,
) -> None:
    """Import historical news file into database and compute vector embeddings."""
    if not file_path.exists():
        raise FileNotFoundError(f"Archive file not found: {file_path}")

    logger.info("Reading archive file %s...", file_path)
    raw_records = load_records_from_file(file_path)
    logger.info("Loaded %d raw records from %s", len(raw_records), file_path.name)

    config = load_config()
    db_config = config.database
    pool = await open_pool(db_config)
    uow = DatabaseUnitOfWork(pool)
    repo = ArchiveRepository()

    embedding_provider = None
    emb_model = config.embedding.model
    emb_dim = config.embedding.dimensions
    if generate_embeddings and not dry_run:
        embedding_provider = create_embedding_provider(
            config=config,
            logger=logger,
        )

    imported_count = 0
    embedded_count = 0

    try:
        for idx, item in enumerate(raw_records, start=1):
            title = clean_text(str(item.get("title") or item.get("headline") or ""))
            content = clean_text(
                str(item.get("text") or item.get("content") or item.get("body") or "")
            )
            if not title and not content:
                continue
            if not title:
                title = content[:80]

            published_at = parse_datetime(
                item.get("published_at")
                or item.get("date")
                or item.get("timestamp")
                or item.get("created_at")
            )
            source_url = item.get("url") or item.get("link") or item.get("source_url")
            external_id = str(item.get("id") or item.get("external_id") or "") or None
            category = item.get("category") or item.get("rubric")
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            if dry_run:
                logger.info(
                    "[DRY-RUN] Would import article #%d: '%s' (%s, %s)",
                    idx,
                    title[:50],
                    published_at.strftime("%Y-%m-%d"),
                    source_name,
                )
                imported_count += 1
                continue

            async with uow.transaction() as conn:
                article_id = await repo.insert_article(
                    conn,
                    edition_slug=edition_slug,
                    source_name=str(item.get("source_name") or source_name),
                    title=title,
                    content=content,
                    published_at=published_at,
                    source_url=str(source_url) if source_url else None,
                    external_id=external_id,
                    category=str(category) if category else None,
                    tags=tags,
                    metadata={"original_file": file_path.name},
                )
            imported_count += 1

            if embedding_provider is not None:
                semantic_text = f"{title}\n\n{content}"
                content_hash = compute_content_hash(semantic_text)
                try:
                    vector = await embedding_provider.embed(
                        semantic_text,
                        purpose="story_document",
                        model=emb_model,
                        dimensions=emb_dim,
                    )
                    async with uow.transaction() as conn:
                        await repo.insert_embedding(
                            conn,
                            article_id=article_id,
                            vector=vector,
                            model=emb_model,
                            dimensions=emb_dim,
                            content_hash=content_hash,
                        )
                    embedded_count += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to embed article #%d (%s): %s", article_id, title[:40], exc
                    )

            if idx % batch_size == 0 or idx == len(raw_records):
                logger.info(
                    "Progress: processed %d/%d articles (imported: %d, embedded: %d)",
                    idx,
                    len(raw_records),
                    imported_count,
                    embedded_count,
                )

        logger.info(
            "✅ Finished archive import: %d imported, %d embedded.",
            imported_count,
            embedded_count,
        )

    finally:
        await close_pool(pool)


def main():
    parser = argparse.ArgumentParser(description="Import historical news archive into Telebrief.")
    parser.add_argument(
        "--file", "-f", type=Path, required=True, help="Path to archive file (JSON/CSV)"
    )
    parser.add_argument("--source", "-s", type=str, default="Архив новостей", help="Source name")
    parser.add_argument("--edition", "-e", type=str, default="berdyansk", help="Edition slug")
    parser.add_argument("--format", type=str, default=None, help="Force format: json, jsonl, csv")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip embedding generation")
    parser.add_argument("--batch-size", type=int, default=25, help="Batch progress log interval")
    parser.add_argument("--dry-run", action="store_true", help="Dry run without writing to DB")

    args = parser.parse_args()
    asyncio.run(
        import_archive_file(
            args.file,
            source_name=args.source,
            edition_slug=args.edition,
            generate_embeddings=not args.no_embeddings,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
