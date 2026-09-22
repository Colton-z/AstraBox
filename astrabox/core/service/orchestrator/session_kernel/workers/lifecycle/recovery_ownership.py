"""Fence explicit recovery with its existing durable command identity.

This is a Session compare-and-set, not a renewable worker lease. Abandoned
CREATING Sessions follow BootstrapReconciler's existing TERMINATED outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NoReturn

from astrabox.common.utils.errors import APIError


@dataclass(frozen=True)
class RecoveryOwnership:
    repository: Any
    session_id: str
    owner: str | None
    sandbox_generation: str | None = None
    assignment_id: str | None = None
    allocations: list[Any] = field(default_factory=list, compare=False)

    @contextmanager
    def bind(self):
        token = _CURRENT_RECOVERY.set(self)
        try:
            yield
        finally:
            _CURRENT_RECOVERY.reset(token)

    @property
    def expected(self) -> dict[str, Any]:
        expected = {"state": "CREATING", "_runtime_recovery_owner": self.owner}
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

    async def update(self, updates: dict[str, Any]) -> bool:
        applied = await self.repository.compare_and_update_session(
            self.session_id,
            expected=self.expected,
            updates=updates,
        )
        if not applied:
            # A response can be lost after the CAS committed. Accept only the
            # same owner's complete intended write, never another generation.
            current = await self.repository.get_session(self.session_id)
            if not current or current.get("_runtime_recovery_owner") != self.owner:
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
