"""Assistant workspace: one owner's shared sandbox and engine profile.

An owner's Assistant conversations share one physical sandbox. The platform
provisions their common runtime identity and, when persistent workspace storage
is configured, mounts the Assistant's files before engine startup. Native
runtime state is saved in the platform database independently of workspace
volumes; the engine adapter handles its vendor's startup and state restoration.

``engine_kind`` is captured at materialization time and pinned for the
workspace's lifetime — switching engines requires destroying and recreating
the workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.platform import EnginePlatform

from astrabox.core.service.orchestrator.workspace.base import (
    CapabilityScope,
    WorkspaceRef,
)


@dataclass
class AssistantWorkspace:
    ref: WorkspaceRef
    engine_kind: str
    provisioned_runtime_identity: dict[str, Any] | None = field(default=None)

    def is_shared(self) -> bool:
        return True

    def capability_scope(self) -> CapabilityScope:
        return "agent_runtime"

    def plan_runtime_identity(
        self,
        *,
        template: Any,
        session_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Plan the platform-owned Assistant account before box creation."""

        _ = (template, session_id)
        from astrabox.core.service.orchestrator.runtime.conversation_identity import (
            plan_assistant_profile_identity,
        )

        effective_user_id = str(self.ref.user_id or user_id or "").strip()
        effective_assistant_id = str(self.ref.assistant_id or "").strip()
        if not effective_user_id or not effective_assistant_id:
            raise ValueError("assistant workspace requires user_id and assistant_id")
        return plan_assistant_profile_identity(
            engine_kind=self.engine_kind,
            user_id=effective_user_id,
            assistant_id=effective_assistant_id,
            sandbox_id=None,
        )

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
        effective_user_id = str(self.ref.user_id or user_id or "").strip()
        effective_assistant_id = str(self.ref.assistant_id or "").strip()
        if not effective_user_id or not effective_assistant_id:
            raise ValueError("assistant workspace requires user_id and assistant_id")
        _ = runtime_manager
        from astrabox.core.service.orchestrator.runtime.conversation_identity import (
            provision_conversation_identity_with_bootstrap_script,
        )
        from astrabox.core.service.orchestrator.runtime.sandbox_client import (
            extract_sandbox_id,
        )

        identity = dict(runtime_identity or self.plan_runtime_identity(
            template=template,
            session_id=session_id,
            user_id=user_id,
        ))
        identity["sandbox_id"] = extract_sandbox_id(sandbox)
        self.provisioned_runtime_identity = (
            await provision_conversation_identity_with_bootstrap_script(
                sandbox,
                identity,
            )
        )
