"""Deployment workspace: shared ``agent_id``-keyed NAS subtree.

Runtime-start path is a true no-op: it mounts nothing and returns an empty
plugin allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.platform import EnginePlatform

from astrabox.core.service.orchestrator.workspace.base import (
    CapabilityScope,
    WorkspaceRef,
)


@dataclass(frozen=True)
class DeploymentWorkspace:
    ref: WorkspaceRef

    def is_shared(self) -> bool:
        return True

    def capability_scope(self) -> CapabilityScope:
        return "agent_runtime"

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
        return None
