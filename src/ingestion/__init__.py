"""Generic ingestion: provider-neutral collection types and atomic persistence."""

from __future__ import annotations

from src.ingestion.models import (
    CollectionBatch,
    CollectionCheckpoint,
    CollectionOutcome,
    CollectionTrigger,
    JSONValue,
    ObservedAsset,
    ObservedItem,
    ObservedStateEvent,
)
from src.ingestion.protocol import CollectionContext, Collector

__all__ = [
    "Collector",
    "CollectionBatch",
    "CollectionCheckpoint",
    "CollectionContext",
    "CollectionOutcome",
    "CollectionTrigger",
    "JSONValue",
    "ObservedAsset",
    "ObservedItem",
    "ObservedStateEvent",
]
