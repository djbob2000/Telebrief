"""Unit of Work: explicit transaction boundaries over the shared pool.

The unit of work acquires a pooled connection and opens exactly one
transaction per ``transaction()`` block. It deliberately does not own
repositories or hand out implicit global connections: repositories receive
the yielded :class:`psycopg.AsyncConnection` explicitly, so the caller keeps
full ownership of transaction scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

__all__ = ["DatabaseUnitOfWork"]


class DatabaseUnitOfWork:
    """Yield a pooled connection wrapped in a single database transaction."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Acquire a pooled connection and run one transaction on it.

        The transaction commits when the block exits cleanly and rolls back
        if the body raises.
        """
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn
