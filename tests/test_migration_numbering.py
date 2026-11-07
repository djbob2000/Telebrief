# ruff: noqa: S101
"""The supported schema maximum must track the newest numbered migration."""

from __future__ import annotations

import re
from pathlib import Path

from src.bootstrap import SCHEMA_VERSION_MAXIMUM

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def test_schema_maximum_matches_newest_numbered_migration() -> None:
    numbers = [
        int(match.group(1))
        for path in MIGRATIONS_DIR.glob("*.sql")
        if (match := re.match(r"^(\d{4})_", path.name))
    ]
    assert numbers, "no numbered migrations found"
    assert SCHEMA_VERSION_MAXIMUM == max(numbers)


def test_migration_numbers_are_unique() -> None:
    names = [
        int(match.group(1))
        for path in MIGRATIONS_DIR.glob("*.sql")
        if (match := re.match(r"^(\d{4})_", path.name))
    ]
    assert len(names) == len(set(names))
