# ruff: noqa: S101
"""Parallel embedding calls: bounded concurrency, batch-order results, clean cancellation."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from src.processing.embeddings import embed_batches_in_order, embedding_concurrency


def test_results_come_back_in_batch_order_with_bounded_concurrency() -> None:
    in_flight = 0
    peak = 0

    async def embed(batch: int) -> str:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # Later batches finish first, so only the ordering logic keeps them in order.
            await asyncio.sleep(0.005 * (10 - batch))
            return f"v{batch}"
        finally:
            in_flight -= 1

    async def main() -> list[int]:
        seen: list[int] = []
        async with aclosing(
            embed_batches_in_order(list(range(10)), embed, concurrency=4)
        ) as results:
            async for batch, vector, seconds in results:
                assert vector == f"v{batch}"
                assert seconds >= 0
                seen.append(batch)
        return seen

    assert asyncio.run(main()) == list(range(10))
    assert peak == 4


def test_failure_raises_at_its_position_and_cancels_pending_calls() -> None:
    cancelled: list[int] = []

    async def embed(batch: int) -> str:
        if batch == 2:
            raise RuntimeError("provider down")
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            cancelled.append(batch)
            raise
        return f"v{batch}"

    async def main() -> list[int]:
        seen: list[int] = []
        with pytest.raises(RuntimeError, match="provider down"):
            async with aclosing(
                embed_batches_in_order(list(range(6)), embed, concurrency=4)
            ) as results:
                async for batch, _vector, _seconds in results:
                    seen.append(batch)
        return seen

    # Batches 0 and 1 are yielded (and would be persisted) before the failure.
    assert asyncio.run(main()) == [0, 1]
    # Calls for batches still pending at the failure are cancelled, not left running.
    assert set(cancelled) <= {3, 4, 5}
    assert cancelled


def test_consumer_stopping_early_cancels_pending_calls() -> None:
    finished: list[int] = []

    async def embed(batch: int) -> str:
        await asyncio.sleep(0.01 if batch == 0 else 0.5)
        finished.append(batch)
        return f"v{batch}"

    async def main() -> int:
        async with aclosing(
            embed_batches_in_order(list(range(4)), embed, concurrency=4)
        ) as results:
            async for batch, _vector, _seconds in results:
                if batch == 0:
                    break
        await asyncio.sleep(0)
        return len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()])

    leftover = asyncio.run(main())
    assert leftover == 0
    assert 1 not in finished  # the slow calls never completed after the consumer left


def test_embedding_concurrency_defaults_to_four_and_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_CONCURRENCY", raising=False)
    assert embedding_concurrency() == 4
    monkeypatch.setenv("EMBEDDING_CONCURRENCY", "8")
    assert embedding_concurrency() == 8
    monkeypatch.setenv("EMBEDDING_CONCURRENCY", "0")
    assert embedding_concurrency() == 1
    monkeypatch.setenv("EMBEDDING_CONCURRENCY", "many")
    assert embedding_concurrency() == 4
