"""A cancelled request cannot abandon its transaction owner's cleanup task."""

from __future__ import annotations

import asyncio

import anyio
import pytest

from astrabox.persistence.repository.transaction import run_owned_transaction


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_request_joins_cleanup_even_after_repeated_cancellation(cancel_twice: bool) -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    request = asyncio.create_task(run_owned_transaction(operation))
    await entered.wait()
    request.cancel()
    await cleaning.wait()
    if cancel_twice:
        request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    assert not cleaned.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert cleaned.is_set()


async def test_anyio_level_cancellation_does_not_interrupt_cleanup() -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            # More than one cancellation checkpoint: an unshielded parent
            # scope would interrupt the first and never reach this marker.
            await anyio.sleep(0)
            await anyio.sleep(0)
            cleaned.set()

    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(run_owned_transaction, operation)
            await entered.wait()
            group.cancel_scope.cancel()
    assert cleaned.is_set()
