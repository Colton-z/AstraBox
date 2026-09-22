"""Deployment-conversation workspace: per-session sandbox for ``agent_chat``.

Unlike ``DeploymentWorkspace`` (shared, ``agent_id``-keyed, runtime-start no-op),
this is the **per-session** workspace: each deployment conversation owns its own
sandbox, and the conversation's files live in that box, under the home of the
account the agent image bakes. A conversation is separated from every other one
by having its own box — nothing another conversation owns is mounted into it.

``mount_and_provision`` runs the one-shot conversation bootstrap that provisions
the per-conversation identity and caches skills/plugins. The provisioned identity
is exposed via ``provisioned_runtime_identity`` for the caller to persist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.platform import EnginePlatform

from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    plan_conversation_identity,
    prepare_agent_runtime_skill_cache,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    assert_identity_boundary_complete,
    plan_capabilities,
    resolve_runtime_profile,
    template_capability_hash,
)
from astrabox.seams.sandbox import (
    sandbox_for_template,
)
# The built-in open_sandbox provider self-registers via
# astrabox.providers.register_builtin_providers() (invoked at runtime_manager /
# app bootstrap). An unregistered backend name fails loud in
# sandbox_for_template(...).
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
    get_underlying_sandbox,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    bootstrap_conversation_runtime_from_agent_cache,
    prepare_agent_runtime_plugin_cache,
)
from astrabox.core.service.orchestrator.workspace.base import (
    CapabilityScope,
    WorkspaceRef,
)


def _template_skills(template: Any) -> list[str]:
    skills = getattr(template, "skills", None)
    if skills is None and isinstance(template, dict):
        skills = template.get("skills")
    return [str(item).strip() for item in (skills or []) if str(item).strip()]


def build_conversation_identity(
    *, session_id: str, agent_id: str, template: Any
) -> dict[str, Any]:
    """Prepare the per-conversation runtime_identity + capability_plan.

    ``sandbox_id`` is None here — the identity is keyed off ``session_id`` and
    describes paths inside a box that does not exist yet.

    Every path is the runtime profile's own rendering, which puts the workspace
    under the workload user's home — a directory the agent image already
    created and already gave to that user. Nothing here computes a host-side
    storage path: what makes a conversation's workspace durable, if a
    deployment wants it durable, is a mount over that path, and the mount is
    the deployment's to plan. That also keeps one conversation out of another's
    files (docs/providers/opensandbox.md, "Isolation is what you mount"). A
    backend may use a dedicated sandbox or an isolated placement in an
    Agent-shared sandbox; this plan exposes only the conversation's paths.
    """
    engine_kind = str(
        template.get("engine_kind")
        if isinstance(template, dict)
        else getattr(template, "engine_kind", "")
    ).strip()
    runtime_profile = resolve_runtime_profile(
        template,
        session_kind="agent_chat",
        engine_kind=engine_kind,
    )
    identity = plan_conversation_identity(
        session_id=session_id,
        sandbox_id=None,
        agent_id=agent_id,
        runtime_profile=runtime_profile,
    )
    identity["template_capability_hash"] = template_capability_hash(template)
    capability_plan = plan_capabilities(template, identity)
    assert_identity_boundary_complete(identity, capability_plan)
    identity["capability_plan"] = capability_plan
    return identity


@dataclass
class DeploymentConversationWorkspace:
    """Per-session workspace for one deployment conversation.

    Not frozen: ``mount_and_provision`` records the conversation identity it
    provisions into ``provisioned_runtime_identity`` so the startup caller can
    persist it onto the session row.
    """

    ref: WorkspaceRef
    provisioned_runtime_identity: dict[str, Any] | None = field(default=None)

    def is_shared(self) -> bool:
        return False

    def capability_scope(self) -> CapabilityScope:
        return "conversation"

    def plan_runtime_identity(
        self,
        *,
        template: Any,
        session_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        _ = user_id
        agent_id = str(self.ref.agent_id or "").strip()
        if not agent_id:
            raise ValueError("DeploymentConversationWorkspace requires ref.agent_id")
        return build_conversation_identity(
            session_id=session_id,
            agent_id=agent_id,
            template=template,
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
        agent_id = str(self.ref.agent_id or "").strip()
        if not agent_id:
            raise ValueError("DeploymentConversationWorkspace requires ref.agent_id")

        # The generic startup path passes runtime_identity=None for this subject;
        # the per-conversation identity is owned here (agent_chat-specific prep).
        if not runtime_identity:
            runtime_identity = self.plan_runtime_identity(
                template=template,
                session_id=session_id,
                user_id=user_id,
            )

        # The workspace is the image's: it exists and belongs to the workload account
        # before this runs, so the conversation bootstrap (below) only verifies it is
        # writable. A fresh sandbox is provisioned per
        # conversation, so nothing is carried over — populate the
        # conversation's plugin + skill runtime directly on the box: clone plugin
        # repos and fetch skills. Remote and stdio MCP definitions stay in the
        # engine's native plugin configuration.
        # Any failure here raises and fails the conversation.
        # The two caches live in separate directories with separate locks and
        # hash short-circuits, so they can be proven concurrently: on a
        # pool-prepared box both are one ready-probe exec, and overlapping them
        # takes one round trip off the session-start path.
        await asyncio.gather(
            prepare_agent_runtime_plugin_cache(
                sandbox,
                template,
                get_underlying_sandbox_fn=get_underlying_sandbox,
            ),
            prepare_agent_runtime_skill_cache(
                get_underlying_sandbox(sandbox),
                _template_skills(template),
            ),
        )
        # The conversation bootstrap transport is backend-specific: some backends reach the
        # sandbox sidecar over HTTP via the gateway ("sidecar_http"), but other backends'
        # sandboxes are not reachable that way and must run the bootstrap script
        # inside the sandbox ("sandbox_command_script"). Resolve it from the
        # template's backend — omitting it (defaulting to sidecar_http) makes those backends'
        # conversations fail at bootstrap with a 502 gateway error.
        bootstrap_transport = sandbox_for_template(template).conversation_bootstrap_transport
        identity = await bootstrap_conversation_runtime_from_agent_cache(
            sandbox,
            template,
            session_id,
            get_underlying_sandbox_fn=get_underlying_sandbox,
            runtime_identity=runtime_identity,
            skills=_template_skills(template),
            bootstrap_transport=bootstrap_transport,
        )
        # The conversation identity is planned before the sandbox exists
        # (sandbox_id=None — POSIX uid/gid are sandbox-local, keyed off
        # session_id). Now that the per-session sandbox is realized, stamp its
        # id onto the identity so the persisted session row records the
        # identity↔sandbox binding (recovery/rebuild reads it from Mongo, e.g.
        # the uid/gid reallocation path), not just the live session.sandbox_id.
        realized_sandbox_id = extract_sandbox_id(
            get_underlying_sandbox(sandbox)
        ) or extract_sandbox_id(sandbox)
        if realized_sandbox_id and isinstance(identity, dict):
            identity = {**identity, "sandbox_id": realized_sandbox_id}
        self.provisioned_runtime_identity = identity
