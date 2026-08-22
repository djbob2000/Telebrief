"""Typed collection errors shared by collectors, jobs, and retry policy."""

from __future__ import annotations


class TransientCollectionError(RuntimeError):
    """A collection failure that is worth retrying the scan task.

    Raised by ``scan_source`` only for genuinely transient outcomes; the
    Procrastinate retry strategy retries it a bounded number of times with an
    increasing wait. Repeated execution is safe because ingestion is
    idempotent at the database level.
    """

    def __init__(self, source_id: int, message: str | None = None) -> None:
        self.source_id = source_id
        super().__init__(message or f"transient collection failure for source {source_id}")
