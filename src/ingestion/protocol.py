"""Collector protocol: the contract every provider-specific collector implements.

Collectors are pure observers: ``scan()`` performs network/provider work and
returns a :class:`~src.ingestion.models.CollectionBatch`; it never writes the
database. The ingestion service owns all persistence and transaction bounds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.domain.sources import Source
from src.ingestion.models import CollectionBatch, CollectionCheckpoint, JSONValue


@dataclass(frozen=True)
class CollectionContext:
    """Ambient inputs for a scan, kept minimal on purpose."""

    now: datetime
    logger: logging.Logger | None = None
    options: dict[str, JSONValue] = field(default_factory=dict)


class Collector(Protocol):
    """Anything that can scan one source into a CollectionBatch."""

    async def scan(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
    ) -> CollectionBatch:
        raise NotImplementedError
