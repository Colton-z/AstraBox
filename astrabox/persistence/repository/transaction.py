"""Cancellation ownership shared by native document transaction providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio

T = TypeVar("T")


def require_uncancelled_transaction() -> None:
    """A callback cannot turn swallowed caller cancellation into a commit."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def run_owned_transaction(operation: Callable[[], Awaitable[T]]) -> T:
    """Join rollback and connection cleanup before propagating cancellation.

    The native provider owns commit/rollback. A caller's level cancellation
    cannot repeatedly interrupt that ownership; native task cancellation is
    delivered once, then cleanup is joined even after another Task.cancel().
    Cancellation during commit can still have an uncertain database outcome.
    """
    async def owned() -> T:
        with anyio.CancelScope(shield=True):
            return await operation()

    task = asyncio.create_task(owned())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if not task.cancelled():
            task.exception()
        raise
