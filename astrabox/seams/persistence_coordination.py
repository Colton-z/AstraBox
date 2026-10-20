"""Runtime exclusion and committed-state wakeups supplied by the selected store.

Notices are hints to reread durable state, not a queue or proof of execution.
The provider enlists them in the repository mutation's transaction. A listener
reports a gap after reconnect so subscribers reread what they may have missed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncConnection

NoticeKind = Literal[
    "input", "output", "reader", "answer", "control", "permission", "terminal",
    "terminal_interrupt", "session", "transcript",
]


@dataclass(frozen=True, slots=True)
class Notice:
    kind: NoticeKind
    seq: int


@dataclass(frozen=True, slots=True)
class SessionNotice:
    session_id: str
    kind: NoticeKind
    seq: int


@dataclass(frozen=True, slots=True)
class Resync:
    """Something may have been missed; read durable state again."""


RuntimeLockCheck = Callable[[], Awaitable[None]]


@runtime_checkable
class PersistenceCoordination(Protocol):
    async def notify(self, connection: AsyncConnection, notice: SessionNotice) -> None:
        """Enqueue a concrete wakeup on the mutation's actual transaction.

        The caller owns the connection and transaction. Do not commit or open
        another transaction: rollback emits nothing, and listeners observe the
        wakeup only after the mutation commits. Failure refuses the mutation.
        """
        ...

    def runtime_lock(self, session_id: str) -> AbstractAsyncContextManager[RuntimeLockCheck]:
        """Exclude other replicas for this Session until the context exits.

        Loss of ownership cancels the holder and makes the yielded check and
        context exit refuse success. Repository work uses independent resources.
        Call the check before publishing completed supplier work. This does not
        retract an accepted supplier request or fence an undetected partition.
        """
        ...

    async def listen(
        self,
        *,
        on_notice: Callable[[SessionNotice], None],
        on_resync: Callable[[], None],
        on_ready: Callable[[], None],
    ) -> None:
        """Listen until cancelled, calling ready after subscription is active.

        Notices announce committed state only. Restore a lost subscription,
        report the gap with resync, then call ready again. Consumers subscribe
        before their initial durable read and reread on resync; delivery may
        coalesce duplicate notices. Cancellation closes listener resources.
        A return, unhandled exception or cancellation outside service shutdown
        ends the core-owned task and makes that replica not ready. The core
        exposes task diagnostics; it does not retry provider-specific errors.
        """
        ...
