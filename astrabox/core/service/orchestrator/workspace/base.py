"""In-sandbox workspace scope: identity, paths, mounts, and provisioning.

The concrete workspaces are the Agent runtime/conversation and the Assistant
workspace from the two-product domain model. Sandbox ownership and lifecycle
selection belong to ``runtime_subject.py``; this protocol begins after a
sandbox action has been selected.

The factory ``workspace_from_subject_kind`` lives in the package
``__init__.py`` so that its concrete-workspace imports stay at module
top-level — deferring these ``astrabox.*`` imports into the function body risks
circular imports or a late ModuleNotFoundError when invoked from
background-worker contexts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.platform import EnginePlatform

WorkspaceKind = Literal["agent", "assistant"]
CapabilityScope = Literal["conversation", "agent_runtime"]


@dataclass(frozen=True)
class WorkspaceRef:
    kind: WorkspaceKind
    user_id: str | None = None
    agent_id: str | None = None
    assistant_id: str | None = None

    def as_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"kind": self.kind}
        if self.user_id:
            doc["user_id"] = self.user_id
        if self.agent_id:
            doc["agent_id"] = self.agent_id
        if self.assistant_id:
            doc["assistant_id"] = self.assistant_id
        return doc


class Workspace(Protocol):
    ref: WorkspaceRef

    def is_shared(self) -> bool: ...

    def capability_scope(self) -> CapabilityScope: ...

    async def mount_and_provision(
        self,
        runtime_manager: "EnginePlatform",
        sandbox: Any,
        *,
        template: Any,
        session_id: str,
        user_id: str | None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        """Mount storage and materialize platform-owned repositories/extensions."""
        ...
