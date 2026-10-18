"""Host-issued guards for provider-owned writes on the shared database."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    API_ADMIN_SCOPE, API_WRITE_SCOPE, _read_context_from_thread_local,
)
from astrabox.core.service.orchestrator.agent_access import can_manage_agent
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.assistant.assistant_layers import LAYER_RUNTIME_FIELDS
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.agent_repository import _GENERATION_FENCE
from astrabox.persistence.repository.preparation_subject_repository import (
    PreparationSubjectState, lock_subject, subject_conflict,
)
from astrabox.seams.runtime_preparation import (
    PreparationCommitGuard, PreparationSubject, ResourcePreparationOperation,
)


def _owner(subject: PreparationSubject, state: PreparationSubjectState) -> str:
    return str(state.definition["owner_id" if subject.kind == "assistant" else "created_by"])


def _revision(subject: PreparationSubject, state: PreparationSubjectState) -> str:
    if subject.kind == "agent":
        return str(state.definition["version"])
    return AgentConfigService._assistant_revision(
        state.definition, state.environment,
        layers=[{key: layer.get(key) for key in LAYER_RUNTIME_FIELDS} for layer in state.layers],
    )


def _authorize(
    subject: PreparationSubject, state: PreparationSubjectState,
    user_id: str, roles: tuple[str, ...],
) -> None:
    permitted = (
        _owner(subject, state) == user_id if subject.kind == "assistant"
        else can_manage_agent(state.definition, user_id, roles)
    )
    if not permitted:
        raise APIError(code="FORBIDDEN", message="subject management permission required", status_code=403)


def _require_transaction(connection: AsyncConnection, subject: PreparationSubject) -> None:
    synchronous = connection.sync_connection
    if (
        connection.engine is not get_persistence().get_engine()
        or synchronous is None or connection.closed or connection.invalidated
        or not connection.in_transaction() or connection.in_nested_transaction()
        or synchronous.get_execution_options().get("isolation_level") == "AUTOCOMMIT"
    ):
        raise ValueError("preparation guard requires the selected engine's active root transaction")
    transaction = connection.get_transaction()
    previous = connection.info.get("astrabox.preparation_commit_subject")
    if previous is not None and previous[0] == transaction and previous[1] != subject:
        raise ValueError("validate only one preparation subject per transaction")
    connection.info["astrabox.preparation_commit_subject"] = (transaction, subject)


@dataclass(frozen=True)
class _CommitGuard:
    subject: PreparationSubject
    owner_user_id: str
    revision: str
    operation: ResourcePreparationOperation | None = None
    provider: str | None = field(default=None, repr=False)
    generation: tuple[tuple[str, object], ...] = field(default=(), repr=False)
    sandbox_id: str | None = field(default=None, repr=False)
    materializer: tuple[str, tuple[tuple[str, object], ...]] | None = field(default=None, repr=False)
    requester: tuple[str, tuple[str, ...]] | None = field(default=None, repr=False)

    async def validate(self, connection: AsyncConnection) -> None:
        """Keep this subject current until the caller commits or rolls back."""
        _require_transaction(connection, self.subject)
        if await connection.get_isolation_level() != "READ COMMITTED":
            raise ValueError("preparation guard requires READ COMMITTED isolation")
        state = await lock_subject(connection, self.subject)
        self._validate_state(state)

    def _validate_state(self, state: PreparationSubjectState) -> None:
        if _owner(self.subject, state) != self.owner_user_id or _revision(self.subject, state) != self.revision:
            raise subject_conflict()
        if self.requester is not None:
            _authorize(self.subject, state, *self.requester)
        runtime = state.runtime or {}
        prefix = "prepared_operation" if self.subject.kind == "assistant" else "resources_operation"
        current_id = runtime.get(f"{prefix}_id")
        if self.operation is None:
            if current_id is not None:
                raise subject_conflict()
            return
        deadline = runtime.get(f"{prefix}_deadline")
        deadline_seconds = deadline.timestamp() if hasattr(deadline, "timestamp") else float(deadline or 0)
        if (
            current_id != self.operation.operation_id
            or runtime.get(f"{prefix}_provider") != self.provider
            or deadline_seconds <= time.time()
            or any(runtime.get(key) != value for key, value in self.generation)
        ):
            raise subject_conflict()
        if self.sandbox_id is not None:
            if self.materializer is None:
                if runtime.get("state") != "READY" or runtime.get("current_sandbox_id") != self.sandbox_id:
                    raise subject_conflict()
            else:
                session_id, expected = self.materializer
                session = state.materializer_session or {}
                allocation = session.get("startup_allocation") or {}
                if (
                    runtime.get("state") != "MATERIALIZING"
                    or runtime.get("provisioning_session_id") != session_id
                    or not runtime.get("provisioning_sandbox_generation")
                    or runtime.get("provisioning_sandbox_generation") != session.get("sandbox_generation")
                    or session.get("session_id") != session_id
                    or session.get("assistant_id") != self.subject.id
                    or session.get("user_id") != self.owner_user_id
                    or any(session.get(key) != value for key, value in expected)
                    or allocation.get("sandbox_id") != self.sandbox_id
                    or allocation.get("scope") != "sandbox"
                ):
                    raise subject_conflict()


def callback_commit_guard(
    *, operation: ResourcePreparationOperation, owner_user_id: str, revision: str,
    provider: str, config: Mapping[str, Any], preparation_id: str,
    materializer: tuple[str, tuple[tuple[str, object], ...]] | None = None,
) -> PreparationCommitGuard:
    """Bind a claimed synchronization to its real operation and prepared generation."""
    agent = operation.subject.kind == "agent"
    generation = tuple(
        (column.name, config.get(key.removeprefix("_")) or None)
        for key, column in _GENERATION_FENCE.items()
    ) if agent else ()
    return _CommitGuard(
        subject=operation.subject, owner_user_id=owner_user_id,
        revision=revision, operation=operation, provider=provider,
        generation=generation, sandbox_id=None if agent else preparation_id,
        materializer=materializer,
    )


async def issue_preparation_commit_guard(*, subject: PreparationSubject) -> PreparationCommitGuard:
    """Issue a standalone guard for the explicitly bound authenticated requester.

    Requires Agent management permission or Assistant ownership and a write/admin
    scope for machine identities. No default local identity is synthesized. The
    caller must validate again in its write transaction; issuance holds no locks
    after return and neither creates nor borrows a synchronization operation.
    """
    user = _read_context_from_thread_local()
    if user is None or not user.user_id.strip():
        raise APIError(code="UNAUTHORIZED", message="a bound authenticated identity is required", status_code=401)
    if user.api_scopes is not None and not ({API_WRITE_SCOPE, API_ADMIN_SCOPE} & set(user.api_scopes)):
        raise APIError(code="FORBIDDEN", message="provider state writes require write scope", status_code=403)
    requester = (user.user_id, tuple(user.roles))
    async with get_persistence().get_engine().begin() as connection:
        state = await lock_subject(connection, subject)
        _authorize(subject, state, *requester)
        guard = _CommitGuard(
            subject=subject, owner_user_id=_owner(subject, state), revision=_revision(subject, state),
            requester=requester,
        )
        guard._validate_state(state)
        return guard
