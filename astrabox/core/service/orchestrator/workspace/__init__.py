"""Workspace abstraction package."""

from __future__ import annotations

from astrabox.core.service.orchestrator.workspace.deployment import DeploymentWorkspace
from astrabox.core.service.orchestrator.workspace.deployment_conversation import (
    DeploymentConversationWorkspace,
)
from astrabox.core.service.orchestrator.workspace.assistant import AssistantWorkspace
from astrabox.core.service.orchestrator.workspace.base import (
    CapabilityScope,
    Workspace,
    WorkspaceKind,
    WorkspaceRef,
)


def workspace_from_subject_kind(
    subject_kind: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    assistant_id: str | None = None,
    engine_kind: str,
) -> Workspace:
    """Build a concrete Workspace for the given runtime subject kind.

    Lives in ``__init__.py`` so the concrete workspace imports stay at module
    load time — deferring these
    ``astrabox.*`` imports into the function body risks circular imports or a late
    ModuleNotFoundError when called from a background-worker context.
    """
    if subject_kind == "deployment_runtime":
        return DeploymentWorkspace(ref=WorkspaceRef(kind="agent", agent_id=agent_id))
    if subject_kind == "deployment_conversation":
        return DeploymentConversationWorkspace(
            ref=WorkspaceRef(kind="agent", user_id=user_id, agent_id=agent_id)
        )
    if subject_kind == "assistant_runtime":
        from astrabox.core.service.orchestrator.engine.capabilities import (
            engine_allowed_for_session_kind,
        )

        if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
            raise ValueError(
                f"engine_kind={engine_kind!r} does not support assistant_chat"
            )
        return AssistantWorkspace(
            ref=WorkspaceRef(
                kind="assistant",
                user_id=user_id,
                assistant_id=assistant_id,
            ),
            engine_kind=engine_kind,
        )
    raise ValueError(f"unknown runtime subject_kind: {subject_kind!r}")


__all__ = [
    "DeploymentWorkspace",
    "DeploymentConversationWorkspace",
    "AssistantWorkspace",
    "CapabilityScope",
    "Workspace",
    "WorkspaceKind",
    "WorkspaceRef",
    "workspace_from_subject_kind",
]
