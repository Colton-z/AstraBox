"""The claim that lets exactly one startup drive a session's runtime.

A startup or a recovery owns a session while its row is ``CREATING`` with
``runtime_recovery_owner`` naming it (and, once a box generation is chosen,
``sandbox_generation`` naming that). Every write it makes compares the claim,
so a superseded startup cannot publish a box over its successor's. The claim
is a compare-and-set on the row, not a renewable lease: a startup abandoned by
a process restart is ended by the bootstrap reconciliation, never resumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, NoReturn

from astrabox.common.utils.errors import APIError
from astrabox.persistence.sql.notify import NoticeKind

#: The startup (by its assignment id) that last reached the sandbox provider
#: for this session; see :meth:`RecoveryOwnership.claim_first_provider_attempt`.
STARTUP_PROVIDER_ASSIGNMENT_FIELD = "startup_provider_assignment"


@dataclass(frozen=True)
class RecoveryOwnership:
    repository: Any
    session_id: str
    owner: str | None
    sandbox_generation: str | None = None
    assignment_id: str | None = None
    allocations: list[Any] = field(default_factory=list, compare=False)

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = _CURRENT_RECOVERY.set(self)
        try:
            yield
        finally:
            _CURRENT_RECOVERY.reset(token)

    @property
    def expected(self) -> dict[str, Any]:
        expected: dict[str, Any] = {"state": "CREATING", "runtime_recovery_owner": self.owner}
        if self.sandbox_generation is not None:
            expected["sandbox_generation"] = self.sandbox_generation
        return expected

    async def require_current(self) -> dict[str, Any]:
        current = await self.repository.get_session(self.session_id)
        if not current or any(current.get(key) != value for key, value in self.expected.items()):
            await self.raise_lost()
        return current

    async def raise_lost(self) -> NoReturn:
        # The active allocation field can move after a provider finishes.
        # Retain each original scope before unwinding the task-local context.
        for allocation in self.allocations:
            record = {
                "allocation": allocation.as_record(),
                "sandbox_generation": self.sandbox_generation,
                "assignment_id": self.assignment_id,
            }
            try:
                await self.repository.retain_startup_allocation(self.session_id, record)
            except BaseException as exc:
                raise APIError(
                    code="SANDBOX_CLEANUP_UNCONFIRMED",
                    status_code=502,
                    message=f"superseded startup allocation could not be retained: {exc}",
                    data={"retained_allocation": record, "retention_confirmed": False},
                ) from exc
        raise self.lost()

    def lost(self) -> APIError:
        return APIError(
            code="RUNTIME_RECOVERY_SUPERSEDED",
            message="runtime recovery no longer owns this Session startup",
            status_code=409,
            data={
                "startup_allocations": [allocation.as_record() for allocation in self.allocations]
            }
            if self.allocations
            else None,
        )

    async def claim_first_provider_attempt(self) -> bool:
        """Record that this startup is about to act on the sandbox provider.

        True means no earlier attempt under this assignment reached that point,
        so no sandbox carries the assignment yet. False means one may: the
        field survives a process that died after this write, so another attempt
        under the same assignment finds it set and must look for the sandbox
        the earlier one may have created or claimed. A concurrent attempt under
        the same assignment loses the compare-and-set and also gets False. A
        lost ownership gets False too; the caller's later ownership checks
        report it.
        """

        if not self.assignment_id:
            return False
        return bool(
            await self.repository.compare_and_update_session(
                self.session_id,
                expected={
                    **self.expected,
                    STARTUP_PROVIDER_ASSIGNMENT_FIELD: {"$ne": self.assignment_id},
                },
                updates={STARTUP_PROVIDER_ASSIGNMENT_FIELD: self.assignment_id},
            )
        )

    async def update(self, updates: dict[str, Any], *, notify: NoticeKind | None = None) -> bool:
        """Write ``updates`` while this claim holds; ``notify`` rides the same statement."""
        applied = await self.repository.compare_and_update_session(
            self.session_id,
            expected=self.expected,
            updates=updates,
            notify=notify,
        )
        if not applied:
            # A response can be lost after the CAS committed. Accept only the
            # same owner's complete intended write, never another generation.
            current = await self.repository.get_session(self.session_id)
            if not current or current.get("runtime_recovery_owner") != self.owner:
                await self.raise_lost()
            if (
                self.sandbox_generation is not None
                and current.get("sandbox_generation") != self.sandbox_generation
            ):
                await self.raise_lost()
            if current.get("state") != updates.get("state", "CREATING"):
                await self.raise_lost()
            if any(current.get(key) != value for key, value in updates.items()):
                await self.raise_lost()
        return True


_CURRENT_RECOVERY: ContextVar[RecoveryOwnership | None] = ContextVar(
    "runtime_recovery", default=None
)


def current_recovery(session_id: str) -> RecoveryOwnership | None:
    ownership = _CURRENT_RECOVERY.get()
    return ownership if ownership is not None and ownership.session_id == session_id else None
