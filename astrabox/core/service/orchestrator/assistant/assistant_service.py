"""Assistant catalog service - CRUD + conversation startup.

An assistant is a single-owner, long-lived personal workspace: the owner's many
conversation sessions share one active runtime at a time. ``owner_id`` is the runtime
authorization boundary — every catalog and workspace operation requires the
caller to be the owner, and there is no cross-user sharing. The workspace row
is keyed by assistant definition and owns the current sandbox pointer plus the
durable profile carried between sandboxes; workspace wake is the provisioning
path.

An Assistant needs an engine that declares ``assistant_chat`` support.  Hermes
is the product default, but a third-party resident engine can implement the
same product without a core-code allowlist.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
    default_permission_mode_for_engine,
    engine_allowed_for_session_kind,
    unsupported_engine_configuration_inputs,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    normalize_plugin_repos,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    normalize_runtime_identity,
)
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import parse_iso, utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction, may_sever_last_name

logger = get_logger(__name__)

# Engine validity comes from the single matrix (domain-model.md §2).
_IMMUTABLE_AFTER_MATERIALIZE = frozenset({"engine_kind", "environment_name", "owner_id"})

# A materializer Session in one of these has stopped driving the workspace: it
# cannot reach READY, so the materialization it claimed has no owner left.
_TERMINAL_SESSION_STATES = frozenset({"TERMINATED", "DELETED"})

#: How far past the sandbox READY timeout an untouched non-terminal materializer
#: row has to be before it is treated as abandoned, and the floor that applies
#: when the deployment configures no ready timeout at all. Both are generous on
#: purpose: the cost of waiting longer is a workspace that stays MATERIALIZING a
#: while, and the cost of being early is stealing a rebuild from a Session
#: that was merely slow. See ``_stalled_materializer_session``.
_MATERIALIZATION_STALL_READY_TIMEOUT_MULTIPLE = 3
_MATERIALIZATION_STALL_FLOOR_SECONDS = 900


@dataclass(frozen=True)
class AssistantWorkspaceAcquisition:
    """The owner decision for one Session asking to use an Assistant workspace."""

    workspace: dict[str, Any]
    owns_materialization: bool
    response: dict[str, Any]


class AssistantService:
    @staticmethod
    def _validate_engine_configuration(
        engine_kind: str,
        values: dict[str, Any],
    ) -> None:
        unsupported = unsupported_engine_configuration_inputs(engine_kind, values)
        if unsupported:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"engine '{engine_kind}' does not consume Assistant "
                    f"configuration fields: {', '.join(unsupported)}"
                ),
                status_code=400,
            )

    @staticmethod
    def _require_engine_kind(assistant: dict[str, Any]) -> str:
        assistant_id = str(assistant.get("assistant_id") or "").strip()
        engine_kind = str(assistant.get("engine_kind") or "").strip()
        if not engine_kind:
            raise APIError(
                code="ASSISTANT_INVALID",
                message=f"assistant={assistant_id!r} has no engine_kind",
                status_code=500,
            )
        if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
            raise APIError(
                code="ASSISTANT_ENGINE_UNSUPPORTED",
                message=(
                    f"assistant={assistant_id!r} uses engine_kind={engine_kind!r}, "
                    "which is not installed with assistant_chat support"
                ),
                status_code=409,
            )
        return engine_kind

    @staticmethod
    def _resolve_permission_mode_default(
        engine_kind: str,
        value: Any,
        *,
        supplied: bool,
    ) -> str | None:
        capabilities = capabilities_for_engine_kind(engine_kind)
        requested = str(value or "").strip() or None
        if not capabilities.permission_modes:
            if supplied and requested is not None:
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine_kind={engine_kind!r} has no permission-mode "
                        "configuration"
                    ),
                    status_code=400,
                )
            return None
        mode = requested or default_permission_mode_for_engine(
            engine_kind,
            "assistant_chat",
        )
        if mode is None:
            raise APIError(
                code="ENGINE_PERMISSION_MODE_REQUIRED",
                message=(
                    f"engine_kind={engine_kind!r} requires an explicit default "
                    "permission mode"
                ),
                status_code=400,
            )
        if mode not in capabilities.permission_modes:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"permission_mode_default={mode!r} is not declared by "
                    f"engine_kind={engine_kind!r}; "
                    f"available={list(capabilities.permission_modes)!r}"
                ),
                status_code=400,
            )
        return mode

    def __init__(
        self,
        *,
        agent_config: Any,
        session_kernel: Any,
        runtime_manager: Any | None = None,
        catalog_repo: AssistantCatalogRepository | None = None,
        workspace_service: AssistantWorkspaceService | None = None,
        sessions_repo: SessionRepository | None = None,
        sandbox_lifecycle_service: Any | None = None,
        spawn_background_task: Callable[..., Any],
    ) -> None:
        self._agent_config = agent_config
        self._session_kernel = session_kernel
        self._runtime_manager = runtime_manager
        self._catalog_repo = catalog_repo or AssistantCatalogRepository()
        self._workspace_service = workspace_service or AssistantWorkspaceService()
        self._sandbox_lifecycle_service = sandbox_lifecycle_service
        # Parking commits a filesystem, which the backend does asynchronously
        # and can take minutes. Required rather than optional: a service that
        # cannot run it outside the request would have to hold one open for the
        # whole commit, and that is the shape this replaced.
        self._spawn_background_task = spawn_background_task
        # Read-only: wake needs to know whether the materializer Session
        # named by ``provisioning_session_id`` is still driving the workspace.
        self._sessions_repo = sessions_repo or SessionRepository()

    async def _converge_workspace_if_sandbox_dead(
        self,
        *,
        workspace: dict[str, Any],
        assistant_id: str,
        reason_prefix: str,
    ) -> dict[str, Any]:
        """Probe a named owner sandbox at a mutation boundary and converge death.

        Listing and other GETs stay read-only. Wake and conversation creation
        are resource-using mutations, so they are the right boundary to ask the
        provider whether a persisted READY pointer is still real. A transient
        probe is not terminal evidence and leaves the row untouched.
        """
        sandbox_id = str(workspace.get("current_sandbox_id") or "").strip()
        workspace_state = str(workspace.get("state") or "").strip()
        if workspace_state not in {"READY", "HIBERNATING"} or not sandbox_id:
            return workspace
        if self._runtime_manager is None:
            raise APIError(
                code="ASSISTANT_RUNTIME_MANAGER_UNAVAILABLE",
                message="cannot verify a ready Assistant workspace without runtime manager",
                status_code=500,
            )
        probe = await self._runtime_manager.get_sandbox_lifecycle_probe(sandbox_id)
        if not self._runtime_manager._is_terminal_sandbox_lifecycle_probe(probe):
            return workspace
        if self._sandbox_lifecycle_service is None:
            raise APIError(
                code="ASSISTANT_LIFECYCLE_UNAVAILABLE",
                message="cannot converge a dead Assistant workspace",
                status_code=500,
            )
        probe_status = str(getattr(probe, "probe_status", "") or "").strip()
        sandbox_state = str(getattr(probe, "sandbox_state", "") or "").strip()
        evidence = probe_status or "terminal_state"
        if sandbox_state:
            evidence = f"{evidence}:{sandbox_state}"
        await self._sandbox_lifecycle_service.converge_dead_sandbox_owners(
            sandbox_id,
            last_error="sandbox terminated",
            reason=f"{reason_prefix}:{evidence}",
        )
        refreshed = await self._workspace_service.get_workspace(
            user_id=str(
                workspace.get("created_by_user_id")
                or workspace.get("user_id")
                or ""
            ).strip(),
            assistant_id=assistant_id,
        )
        if refreshed is None:
            raise APIError(
                code="ASSISTANT_WORKSPACE_NOT_FOUND",
                message=f"assistant={assistant_id} workspace disappeared during convergence",
                status_code=500,
            )
        if (
            str(refreshed.get("state") or "").strip() == "READY"
            and str(refreshed.get("current_sandbox_id") or "").strip()
            == sandbox_id
        ):
            raise APIError(
                code="ASSISTANT_WORKSPACE_CONVERGENCE_FAILED",
                message=(
                    f"assistant={assistant_id} still names confirmed-dead sandbox "
                    f"{sandbox_id} after lifecycle convergence"
                ),
                status_code=503,
            )
        return refreshed

    async def create_assistant(
        self, user: UserContext, config: dict[str, Any]
    ) -> dict[str, Any]:
        display_name = str(config.get("display_name") or "").strip()
        environment_name = str(config.get("environment_name") or "").strip()
        if not display_name:
            raise APIError(
                code="ASSISTANT_INVALID",
                message="display_name is required",
                status_code=400,
            )
        if not environment_name:
            raise APIError(
                code="ASSISTANT_INVALID",
                message="environment_name is required",
                status_code=400,
            )
        # An Assistant is not an Agent: it selects an Environment and applies
        # its own overrides. The Agent program comes from that Environment.
        environment = await self._agent_config.get_environment(environment_name)
        if not environment or environment.get("enabled") is False:
            raise APIError(
                code="ASSISTANT_ENVIRONMENT_MISSING",
                message=f"environment '{environment_name}' not found or disabled",
                status_code=403,
            )
        environment_engine_kind = str(environment.get("engine_kind") or "").strip()
        if not environment_engine_kind:
            raise APIError(
                code="ASSISTANT_ENVIRONMENT_INVALID",
                message=f"environment {environment_name!r} has no engine_kind",
                status_code=500,
            )
        engine_kind = environment_engine_kind
        requested_engine_kind = str(config.get("engine_kind") or "").strip()
        if requested_engine_kind and not engine_allowed_for_session_kind(
            requested_engine_kind,
            "assistant_chat",
        ):
            raise APIError(
                code="ASSISTANT_ENGINE_UNSUPPORTED",
                message=(
                    f"engine_kind={requested_engine_kind!r} is not installed "
                    "with assistant_chat support"
                ),
                status_code=400,
            )
        if requested_engine_kind and requested_engine_kind != engine_kind:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"environment {environment_name!r} uses engine_kind="
                    f"{engine_kind!r}, not {requested_engine_kind!r}"
                ),
                status_code=400,
            )
        if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
            raise APIError(
                code="ASSISTANT_ENGINE_UNSUPPORTED",
                message=(
                    f"engine_kind={engine_kind!r} is not installed with "
                    "assistant_chat support"
                ),
                status_code=400,
            )
        assistant_id = f"asst_{uuid.uuid4().hex[:16]}"
        permission_mode_default = self._resolve_permission_mode_default(
            engine_kind,
            config.get("permission_mode_default"),
            supplied="permission_mode_default" in config,
        )
        plugin_repos_override = normalize_plugin_repos(
            config.get("plugin_repos_override")
        )
        self._validate_engine_configuration(
            engine_kind,
            {
                "mcp_servers": config.get("mcp_config_override"),
                "skills": config.get("skill_manifest_override"),
                "plugin_repos": plugin_repos_override,
            },
        )
        payload = {
            "assistant_id": assistant_id,
            "owner_id": user.user_id,
            "display_name": display_name,
            "icon": str(config.get("icon") or "").strip() or None,
            "description": str(config.get("description") or "").strip() or None,
            "engine_kind": engine_kind,
            "environment_name": environment_name,
            "permission_mode_default": permission_mode_default,
            "model_config_override": config.get("model_config_override"),
            "mcp_config_override": config.get("mcp_config_override"),
            "plugin_repos_override": plugin_repos_override,
            "skill_manifest_override": config.get("skill_manifest_override"),
        }
        created = await self._catalog_repo.create_assistant(payload)
        return self._sanitize_assistant(created)

    async def list_assistants(self, user: UserContext) -> list[dict[str, Any]]:
        rows, workspaces = await asyncio.gather(
            self._catalog_repo.list_assistants(),
            self._workspace_service.list_user_workspaces(user_id=user.user_id),
        )
        workspace_by_assistant = {
            str(workspace.get("assistant_id") or "").strip(): workspace
            for workspace in workspaces
            if str(workspace.get("assistant_id") or "").strip()
        }
        rendered: list[dict[str, Any]] = []
        for row in rows:
            if str((row or {}).get("owner_id") or "").strip() != user.user_id:
                continue
            item = self._sanitize_assistant(row)
            workspace = workspace_by_assistant.get(
                str(item.get("assistant_id") or "").strip()
            )
            item["workspace_state"] = (
                str((workspace or {}).get("state") or "").strip()
                or "NOT_MATERIALIZED"
            )
            if workspace:
                item["current_sandbox_id"] = workspace.get("current_sandbox_id")
            rendered.append(item)
        return rendered

    async def get_assistant(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        assistant = await self._must_use_assistant(user, assistant_id)
        workspace = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        rendered = self._sanitize_assistant(assistant)
        rendered["workspace_state"] = (
            str(workspace.get("state") or "").strip() if workspace else "NOT_MATERIALIZED"
        )
        if workspace:
            rendered["current_sandbox_id"] = workspace.get("current_sandbox_id")
        return rendered

    async def update_assistant(
        self,
        user: UserContext,
        assistant_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        assistant = await self._must_use_assistant(user, assistant_id)
        has_workspace = await self._workspace_service.has_assistant_workspace(
            assistant_id=assistant_id,
        )
        if has_workspace:
            for key in _IMMUTABLE_AFTER_MATERIALIZE:
                if key in updates and updates[key] != assistant.get(key):
                    raise APIError(
                        code="ASSISTANT_IMMUTABLE_FIELD",
                        message=(
                            f"field={key!r} is immutable once workspace is materialized "
                            f"(assistant_id={assistant_id})"
                        ),
                        status_code=409,
                    )
        sanitized_updates = {
            k: v
            for k, v in updates.items()
            if k
            in {
                "display_name",
                "icon",
                "description",
                "permission_mode_default",
                "model_config_override",
                "mcp_config_override",
                "plugin_repos_override",
                "skill_manifest_override",
            }
        }
        if "permission_mode_default" in sanitized_updates:
            sanitized_updates["permission_mode_default"] = (
                self._resolve_permission_mode_default(
                    str(assistant.get("engine_kind") or "").strip(),
                    sanitized_updates["permission_mode_default"],
                    supplied=True,
                )
            )
        if "plugin_repos_override" in sanitized_updates:
            sanitized_updates["plugin_repos_override"] = normalize_plugin_repos(
                sanitized_updates["plugin_repos_override"]
            )
        self._validate_engine_configuration(
            str(assistant.get("engine_kind") or "").strip(),
            {
                platform_name: (
                    sanitized_updates[override_name]
                    if override_name in sanitized_updates
                    else assistant.get(override_name)
                )
                for platform_name, override_name in (
                    ("mcp_servers", "mcp_config_override"),
                    ("skills", "skill_manifest_override"),
                    ("plugin_repos", "plugin_repos_override"),
                )
            },
        )
        if sanitized_updates:
            await self._catalog_repo.update_assistant(assistant_id, sanitized_updates)
        refreshed = await self._catalog_repo.get_assistant(assistant_id)
        return self._sanitize_assistant(refreshed or assistant)

    async def delete_assistant(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        """Delete the catalog row — but only once its sandbox is proven gone.

        Deleting the catalog row is a name-severing act carried out by
        unreachability rather than by a write: ``_must_use_assistant`` resolves
        through ``get_assistant``, whose query excludes deleted rows, so after
        the soft-delete every path that could ever act on this workspace —
        wake (the only retry of a pending destruction), hibernate, the console
        URL — answers 404 forever. The workspace row survives holding
        ``current_sandbox_id``, and nothing can read it and act.

        So the destruction happens first, and the delete is refused if it
        cannot be confirmed. That refusal is the conservative failure
        direction, and it is also the recoverable one: the catalog row stays
        resolvable, so the user (or wake) can retry — which soft-deleting first
        would have made impossible. A workspace that never held a sandbox
        deletes with no destroy at all.
        """
        await self._must_use_assistant(user, assistant_id)
        workspace = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        sandbox_id = str((workspace or {}).get("current_sandbox_id") or "").strip()
        await self._workspace_service.mark_recovery_required(
            user_id=user.user_id,
            assistant_id=assistant_id,
            reason="catalog soft-deleted",
        )
        if sandbox_id:
            released = await self._retry_recovery_destruction(
                user_id=user.user_id,
                assistant_id=assistant_id,
                workspace=workspace or {},
                sandbox_id=sandbox_id,
            )
            if not released:
                raise APIError(
                    code="ASSISTANT_SANDBOX_UNDESTROYED",
                    message=(
                        f"assistant={assistant_id} still has sandbox {sandbox_id}, "
                        "whose destruction could not be confirmed; the assistant is "
                        "kept so the destruction stays retryable — retry the delete"
                    ),
                    status_code=409,
                    data={"sandbox_id": sandbox_id, "retryable": True},
                )
        await self._catalog_repo.soft_delete(assistant_id)
        return {"assistant_id": assistant_id, "deleted": True}

    async def acquire_workspace_for_session(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
    ) -> AssistantWorkspaceAcquisition:
        """Let an existing Session acquire or wait for the Assistant runtime.

        Conversation startup supplies its own Session id.  If the workspace
        needs a sandbox, that Session becomes the materializer and the ordinary
        lifecycle worker creates it.  No second, hidden Session is created.
        """

        session_id = str(provisioning_session_id or "").strip()
        if not session_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message="Assistant workspace acquisition requires a Session id",
                status_code=500,
            )
        assistant = await self._must_use_assistant(user, assistant_id)
        acquisition = await self._acquire_workspace_materialization(
            user=user,
            assistant=assistant,
            assistant_id=assistant_id,
            provisioning_session_id=session_id,
        )

        if not acquisition.owns_materialization:
            return acquisition
        observed_generation = (acquisition.workspace or {}).get("provisioning_sandbox_generation")
        session = await self._sessions_repo.get_session(session_id)
        generation = str(provisioning_sandbox_generation or "").strip()
        if (
            not generation
            or not session
            or session.get("state") != "CREATING"
            or session.get("sandbox_generation") != generation
        ):
            raise APIError(
                code="RUNTIME_RECOVERY_SUPERSEDED",
                message="Assistant materializer startup generation changed",
                status_code=409,
            )
        claimed = await self._workspace_service.claim_materialization_generation(
            user_id=user.user_id,
            assistant_id=assistant_id,
            provisioning_session_id=session_id,
            observed_generation=observed_generation,
            sandbox_generation=generation,
        )
        if not claimed:
            raise APIError(
                code="ASSISTANT_WORKSPACE_READY_CONFLICT",
                message="Assistant materializer ownership changed before startup",
                status_code=409,
            )
        return acquisition

    async def publish_workspace_runtime_ready(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None:
        """Publish profile readiness and, for the elected Session, owner readiness."""

        session_id = str(provisioning_session_id or "").strip()
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not user.user_id or not assistant_id or not session_id or not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_PROFILE_MARKER_INVALID",
                message="Assistant runtime publication requires owner and runtime identities",
                status_code=500,
            )
        workspace = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        owns_materialization = (
            str((workspace or {}).get("state") or "").strip() == "MATERIALIZING"
            and str((workspace or {}).get("provisioning_session_id") or "").strip() == session_id
            and (workspace or {}).get("provisioning_sandbox_generation")
            == provisioning_sandbox_generation
        )
        already_ready_on_runtime = (
            str((workspace or {}).get("state") or "").strip() == "READY"
            and str((workspace or {}).get("current_sandbox_id") or "").strip()
            == resolved_sandbox_id
        )
        if owns_materialization:
            await self._workspace_service.mark_ready(
                user_id=user.user_id,
                assistant_id=assistant_id,
                provisioning_session_id=session_id,
                provisioning_sandbox_generation=provisioning_sandbox_generation,
                sandbox_id=resolved_sandbox_id,
                expires_at=expires_at,
                runtime_identity=runtime_identity,
            )
            return
        if not already_ready_on_runtime:
            raise APIError(
                code="ASSISTANT_WORKSPACE_READY_CONFLICT",
                message=(
                    "Assistant runtime startup lost its workspace authority "
                    f"assistant={assistant_id} session={session_id} "
                    f"sandbox={resolved_sandbox_id}"
                ),
                status_code=409,
            )
        updated = await self._workspace_service.mark_assistant_profile_ready(
            user_id=user.user_id,
            assistant_id=assistant_id,
            sandbox_id=resolved_sandbox_id,
        )
        if not updated:
            raise APIError(
                code="ASSISTANT_PROFILE_MARKER_NOT_WRITTEN",
                message=(
                    "Failed to persist Assistant profile readiness for "
                    f"assistant={assistant_id} sandbox={resolved_sandbox_id}"
                ),
                status_code=409,
            )

    async def converge_workspace_startup_failure(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
        failure_phase: str,
        created_sandbox_id: str | None,
        cleanup: SandboxDestruction | None,
    ) -> bool:
        """Converge owner state after common startup cleanup has settled."""

        session_id = str(provisioning_session_id or "").strip()
        workspace = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        workspace_state = str((workspace or {}).get("state") or "").strip()
        workspace_materializer = str(
            (workspace or {}).get("provisioning_session_id") or ""
        ).strip()
        workspace_sandbox_id = str(
            (workspace or {}).get("current_sandbox_id") or ""
        ).strip()
        resolved_created_sandbox_id = str(created_sandbox_id or "").strip()
        owns_materialization = (
            bool(session_id)
            and workspace_state == "MATERIALIZING"
            and workspace_materializer == session_id
            and (workspace or {}).get("provisioning_sandbox_generation")
            == provisioning_sandbox_generation
        )
        published_runtime = (
            workspace_state == "READY"
            and bool(resolved_created_sandbox_id)
            and workspace_sandbox_id == resolved_created_sandbox_id
        )
        if not owns_materialization and not published_runtime:
            return False
        if workspace_sandbox_id:
            if cleanup is not None and cleanup.outcome == "REFUSED":
                # The allocation belongs to a durable runtime owner now.
                # This Session's failed startup cannot retire that binding.
                return False
            return bool(
                await self._workspace_service.mark_post_commit_failure(
                    user_id=user.user_id,
                    assistant_id=assistant_id,
                    cleanup=cleanup,
                    sandbox_id=resolved_created_sandbox_id,
                    failure_phase=failure_phase,
                )
            )
        cleanup_confirmed = bool(
            resolved_created_sandbox_id
            and may_sever_last_name(
                cleanup,
                sandbox_id=resolved_created_sandbox_id,
            )
        )
        undestroyed = (
            None
            if cleanup_confirmed
            else (
                (cleanup.leaked_sandbox_id if cleanup is not None else None)
                or resolved_created_sandbox_id
                or None
            )
        )
        if undestroyed:
            logger.error(
                "Assistant materializer failed with an unconfirmed sandbox: "
                "session=%s assistant=%s sandbox=%s (%s)",
                session_id,
                assistant_id,
                undestroyed,
                cleanup.detail if cleanup is not None else "cleanup produced no verdict",
            )
            return bool(
                await self._workspace_service.adopt_undestroyed_sandbox(
                    user_id=user.user_id,
                    assistant_id=assistant_id,
                    sandbox_id=undestroyed,
                    reason=f"materialization_{failure_phase}_sandbox_undestroyed",
                    expected_owner={
                        "state": "MATERIALIZING",
                        "provisioning_session_id": session_id,
                        "provisioning_sandbox_generation": provisioning_sandbox_generation,
                    },
                )
            )
        return bool(
            await self._workspace_service.mark_materialization_failed(
                user_id=user.user_id,
                assistant_id=assistant_id,
                provisioning_session_id=session_id,
                provisioning_sandbox_generation=provisioning_sandbox_generation,
                failure_phase=failure_phase,
            )
        )

    async def wake_workspace(self, user: UserContext, assistant_id: str) -> dict[str, Any]:
        """Make the workspace ready when no conversation Session exists yet.

        Explicit wake is the exceptional entry point that needs a hidden
        materializer.  Conversation startup calls
        :meth:`acquire_workspace_for_session` and uses its own Session instead.
        Both paths share the same owner claim and lifecycle state machine.
        """

        assistant = await self._must_use_assistant(user, assistant_id)
        candidate_session_id = str(uuid.uuid4())
        acquisition = await self._acquire_workspace_materialization(
            user=user,
            assistant=assistant,
            assistant_id=assistant_id,
            provisioning_session_id=candidate_session_id,
        )
        if not acquisition.owns_materialization:
            return acquisition.response
        await self._create_hidden_workspace_materializer(
            user=user,
            assistant=assistant,
            assistant_id=assistant_id,
            provisioning_session_id=candidate_session_id,
        )
        return acquisition.response

    async def _acquire_workspace_materialization(
        self,
        *,
        user: UserContext,
        assistant: dict[str, Any],
        assistant_id: str,
        provisioning_session_id: str,
    ) -> AssistantWorkspaceAcquisition:
        """Resolve owner state and atomically elect one Session to materialize it."""

        engine_kind = self._require_engine_kind(assistant)
        workspace = await self._workspace_service.materialize_workspace_if_absent(
            user_id=user.user_id,
            assistant_id=assistant_id,
            engine_kind=engine_kind,
            provisioning_session_id=provisioning_session_id,
        )
        sandbox_id = str(workspace.get("current_sandbox_id") or "").strip()
        workspace_state = str(workspace.get("state") or "").strip()
        if workspace_state == "READY" and sandbox_id:
            workspace = await self._converge_workspace_if_sandbox_dead(
                workspace=workspace,
                assistant_id=assistant_id,
                reason_prefix="assistant_workspace_wake",
            )
            sandbox_id = str(workspace.get("current_sandbox_id") or "").strip()
            workspace_state = str(workspace.get("state") or "").strip()
            if workspace_state == "READY" and sandbox_id:
                response = {
                    "assistant_id": assistant_id,
                    "state": "READY",
                    "engine_kind": workspace.get("engine_kind"),
                    "current_sandbox_id": sandbox_id,
                }
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response=response,
                )
        if workspace_state == "HIBERNATING" and sandbox_id:
            # A process may have stopped after freezing the workspace but before
            # its storage commit reached the crash-safe release state. Re-drive
            # that exact step: prepare on the next box is only safe after this
            # box's files are durable and its destruction is confirmed.
            await self._complete_hibernation_release(
                user_id=user.user_id,
                assistant_id=assistant_id,
                sandbox_id=sandbox_id,
            )
            refreshed = await self._workspace_service.get_workspace(
                user_id=user.user_id,
                assistant_id=assistant_id,
            )
            workspace = refreshed or workspace
            sandbox_id = str(workspace.get("current_sandbox_id") or "").strip()
            workspace_state = str(workspace.get("state") or "").strip()
            if workspace_state == "READY" and sandbox_id:
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response={
                        "assistant_id": assistant_id,
                        "state": "READY",
                        "engine_kind": workspace.get("engine_kind"),
                        "current_sandbox_id": sandbox_id,
                    },
                )
            if workspace_state == "HIBERNATING" and sandbox_id:
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response={
                        "assistant_id": assistant_id,
                        "state": "HIBERNATING",
                        "engine_kind": workspace.get("engine_kind"),
                        "current_sandbox_id": sandbox_id,
                        "retryable": True,
                    },
                )
        if workspace_state == "RECOVERY_REQUIRED" and sandbox_id:
            # The previous sandbox's destruction is not confirmed, and the
            # retained pointer is the only record of that sandbox's id. Wake is
            # the retry: destroy exactly that sandbox, and release the pointer
            # only once the kill is confirmed. Releasing it without a confirmed
            # kill would strand a running box nothing can name again; keeping it
            # forever without retrying would strand the workspace.
            released = await self._retry_recovery_destruction(
                user_id=user.user_id,
                assistant_id=assistant_id,
                workspace=workspace,
                sandbox_id=sandbox_id,
            )
            if not released:
                response = {
                    "assistant_id": assistant_id,
                    "state": "RECOVERY_REQUIRED",
                    "engine_kind": workspace.get("engine_kind"),
                    "recovery_pending_sandbox_id": sandbox_id,
                    "retryable": True,
                }
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response=response,
                )
            # Pointer released; fall through and materialize a fresh sandbox in
            # this same call. The state is still RECOVERY_REQUIRED, which is
            # what the MATERIALIZING compare-and-set below expects.
            sandbox_id = ""
        active_materializer_id = str(
            workspace.get("provisioning_session_id") or ""
        ).strip()
        owns_materialization = (
            workspace_state == "MATERIALIZING"
            and active_materializer_id == provisioning_session_id
        )
        if (
            workspace_state == "MATERIALIZING"
            and active_materializer_id
            and not owns_materialization
        ):
            # MATERIALIZING is a claim that one Session is driving the workspace
            # to READY, not a state the workspace can rest in. Honour the claim
            # only while its owner is still alive; a terminal (or vanished)
            # materializer means nothing is provisioning and the next acquirer
            # must re-drive it.
            stalled = await self._stalled_materializer_session(
                active_materializer_id,
                workspace=workspace,
            )
            if stalled is None:
                response = {
                    "assistant_id": assistant_id,
                    "state": "MATERIALIZING",
                    "engine_kind": workspace.get("engine_kind"),
                    "provisioning_session_id": active_materializer_id,
                }
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response=response,
                )
            redriving = await self._redrive_stalled_materialization(
                user_id=user.user_id,
                assistant_id=assistant_id,
                provisioning_session_id=active_materializer_id,
                stalled_session=stalled,
                provisioning_sandbox_generation=workspace.get("provisioning_sandbox_generation"),
            )
            if redriving is not None:
                response = {
                    "assistant_id": assistant_id,
                    "engine_kind": workspace.get("engine_kind"),
                    **redriving,
                }
                return AssistantWorkspaceAcquisition(
                    workspace=workspace,
                    owns_materialization=False,
                    response=response,
                )
            active_materializer_id = ""
            owns_materialization = False
        if not owns_materialization:
            owns_materialization = await self._workspace_service.claim_materialization(
                user_id=user.user_id,
                assistant_id=assistant_id,
                expected_state=workspace_state,
                provisioning_session_id=provisioning_session_id,
            )
        if not owns_materialization:
            current = await self._workspace_service.get_workspace(
                user_id=user.user_id,
                assistant_id=assistant_id,
            )
            current_state = str((current or {}).get("state") or "").strip()
            response = {
                "assistant_id": assistant_id,
                "state": current_state or workspace_state,
                "engine_kind": (current or workspace).get("engine_kind"),
                "current_sandbox_id": (current or {}).get("current_sandbox_id"),
                "provisioning_session_id": (current or {}).get(
                    "provisioning_session_id"
                ),
            }
            return AssistantWorkspaceAcquisition(
                workspace=current or workspace,
                owns_materialization=False,
                response=response,
            )
        current = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        materializing = current or {
            **workspace,
            "state": "MATERIALIZING",
            "provisioning_session_id": provisioning_session_id,
        }
        response = {
            "assistant_id": assistant_id,
            "state": "MATERIALIZING",
            "engine_kind": materializing.get("engine_kind"),
            "provisioning_session_id": provisioning_session_id,
        }
        return AssistantWorkspaceAcquisition(
            workspace=materializing,
            owns_materialization=True,
            response=response,
        )

    async def _create_hidden_workspace_materializer(
        self,
        *,
        user: UserContext,
        assistant: dict[str, Any],
        assistant_id: str,
        provisioning_session_id: str,
    ) -> None:
        """Create the lifecycle Session used only by an explicit workspace wake."""

        environment_name = str(assistant.get("environment_name") or "").strip()
        if not environment_name:
            raise APIError(
                code="ASSISTANT_INVALID",
                message=f"assistant={assistant_id} has no environment_name",
                status_code=500,
            )
        engine_kind = self._require_engine_kind(assistant)
        try:
            result = await self._session_kernel.create_session(
                user,
                # The runtime configuration resolves by the Assistant workspace_ref
                # (kind=assistant), not by this positional ref, which carries the
                # Environment for display.
                environment_name,
                permission_mode=(
                    str(assistant.get("permission_mode_default") or "").strip()
                    or None
                ),
                session_id=provisioning_session_id,
                session_kind="assistant_chat",
                workspace_ref={
                    "kind": "assistant",
                    "user_id": user.user_id,
                    "assistant_id": assistant_id,
                    "engine_kind": engine_kind,
                },
                hidden=True,
                owner_type="assistant_workspace",
                owner_id=assistant_id,
            )
        except BaseException:
            with contextlib.suppress(BaseException):
                await self._workspace_service.mark_materialization_failed(
                    user_id=user.user_id,
                    assistant_id=assistant_id,
                    provisioning_session_id=provisioning_session_id,
                    provisioning_sandbox_generation=None,
                    failure_phase="session_create_failed",
                )
            raise
        new_provisioning_session_id = str(result.get("session_id") or "").strip()
        if new_provisioning_session_id != provisioning_session_id:
            await self._workspace_service.mark_materialization_failed(
                user_id=user.user_id,
                assistant_id=assistant_id,
                provisioning_session_id=provisioning_session_id,
                provisioning_sandbox_generation=None,
                failure_phase="session_identity_mismatch",
            )
            raise APIError(
                code="ASSISTANT_WORKSPACE_MATERIALIZER_IDENTITY_MISMATCH",
                message=(
                    "Assistant workspace materializer did not use its claimed "
                    "Session identity"
                ),
                status_code=500,
            )

    async def _complete_hibernation_release(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> dict[str, Any]:
        """Stop native writers and save after workspace admission is closed.

        Before RECOVERY_REQUIRED, recovery repeats the engine's save barrier.
        After it, the native state is durable and only destruction remains.
        """
        if self._runtime_manager is None:
            raise APIError(
                code="ASSISTANT_RUNTIME_MANAGER_UNAVAILABLE",
                message="cannot hibernate assistant workspace without runtime manager",
                status_code=500,
            )
        box = None
        phase = "runtime_identity"
        try:
            workspace = await self._workspace_service.get_workspace(
                user_id=user_id,
                assistant_id=assistant_id,
            )
            identity = normalize_runtime_identity((workspace or {}).get("runtime_identity"))
            if identity is None:
                raise ValueError("workspace has no valid runtime identity")
            phase = "engine_capability"
            adapter = get_engine_adapter(self._require_engine_kind(workspace or {}))
            phase = "sandbox_connect"
            box = await self._runtime_manager.connect_sandbox_only(sandbox_id)
            phase = "native_state_save"
            await adapter.quiesce_and_save_runtime_state(box, runtime_identity=identity)
        except Exception as exc:
            data = exc.data if isinstance(exc, APIError) else None
            if isinstance(data, dict) and isinstance(data.get("phase"), str):
                phase = data["phase"]
            raise APIError(
                code="ASSISTANT_WORKSPACE_CONVERGENCE_FAILED",
                message=(
                    f"assistant={assistant_id} hibernation stopped during {phase}; "
                    f"sandbox {sandbox_id} is retained"
                ),
                status_code=503,
                data={"sandbox_id": sandbox_id, "retryable": True, "phase": phase},
            ) from exc
        finally:
            close = getattr(box, "close", None) if box is not None else None
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()

        owns_release = await self._workspace_service.mark_hibernation_release_required(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=sandbox_id,
        )
        if not owns_release:
            current = await self._workspace_service.get_workspace(
                user_id=user_id,
                assistant_id=assistant_id,
            )
            current_state = str((current or {}).get("state") or "").strip()
            current_sandbox_id = str(
                (current or {}).get("current_sandbox_id") or ""
            ).strip()
            if current_state in {"RECOVERY_REQUIRED", "HIBERNATING"}:
                return {
                    "assistant_id": assistant_id,
                    "hibernated": not current_sandbox_id,
                    "released": not current_sandbox_id,
                    "recovery_required": current_state == "RECOVERY_REQUIRED",
                    "previous_sandbox_id": sandbox_id,
                    "sandbox_id": current_sandbox_id or None,
                }
            raise APIError(
                code="ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT",
                message=(
                    f"assistant={assistant_id} workspace changed while releasing "
                    f"sandbox {sandbox_id}; the newer transition was left untouched"
                ),
                status_code=409,
            )

        destruction = await self._runtime_manager.destroy_sandbox_by_id(sandbox_id)
        if not may_sever_last_name(destruction, sandbox_id=sandbox_id):
            return {
                "assistant_id": assistant_id,
                "hibernated": False,
                "released": False,
                "recovery_required": True,
                "previous_sandbox_id": sandbox_id,
                "sandbox_id": sandbox_id,
            }
        finished = await self._workspace_service.finish_hibernation(
            user_id=user_id,
            assistant_id=assistant_id,
            destroyed_sandbox_id=sandbox_id,
            expected_state="RECOVERY_REQUIRED",
        )
        if not finished:
            raise APIError(
                code="ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT",
                message=(
                    f"sandbox {sandbox_id} was destroyed after release was recorded, "
                    f"but assistant={assistant_id} workspace moved concurrently"
                ),
                status_code=409,
            )
        logger.info(
            "assistant hibernate released durable workspace box: assistant=%s sandbox=%s",
            assistant_id,
            sandbox_id,
        )
        return {
            "assistant_id": assistant_id,
            "hibernated": True,
            "released": True,
            "recovery_required": False,
            "previous_sandbox_id": sandbox_id,
            "sandbox_id": None,
        }

    async def _stalled_materializer_session(
        self,
        provisioning_session_id: str,
        *,
        workspace: dict[str, Any],
    ) -> dict[str, Any] | None:
        """The materializer Session row when it has stopped driving, else None.

        A materializer cannot publish READY in three cases:

        * the row is gone — nothing will publish READY from it;
        * the row reached TERMINATED / DELETED — its failure tail ran and said
          so. Conclusive, no timing involved.
        * **the process carrying it may have died mid-startup.** A stale
          CREATING row leaves the workspace MATERIALIZING without a process
          that can finish it, which is the failure the claim must survive.

        There is no liveness signal for a process that is gone; the only thing
        the row can offer is that it has not been touched. So the third case is
        judged on staleness, deliberately and with the trade stated: the bound
        is derived from the sandbox READY timeout (which is the longest a
        healthy Session can legitimately go without a write) with generous
        headroom, so a slow-but-alive materializer is not stolen from. Being wrong
        here costs nothing dangerous on its own: taking over a materialization
        only starts a rebuild, and the box the old materializer may still hold is
        protected by the caller's separate requirement to prove it destroyed
        before claiming (see :meth:`_redrive_stalled_materialization`).
        """
        session = await self._sessions_repo.get_session(provisioning_session_id)
        if not isinstance(session, dict):
            if self._materializer_session_is_stale(workspace):
                return {"session_id": provisioning_session_id}
            return None
        state = str(session.get("state") or "").strip()
        if state in _TERMINAL_SESSION_STATES:
            return session
        if self._materializer_session_is_stale(session):
            return session
        return None

    def _materializer_session_is_stale(self, session: dict[str, Any]) -> bool:
        """Has this non-terminal materializer gone untouched past every bound?"""
        raw = str(session.get("updated_at") or "").strip()
        try:
            updated_at = parse_iso(raw) if raw else None
        except (TypeError, ValueError):
            updated_at = None
        if updated_at is None:
            # No timestamp to judge by. "I cannot tell" is not "it is dead", so
            # the materializer keeps its claim and the workspace keeps reporting
            # MATERIALIZING — the conservative direction for a takeover.
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        ready_timeout = int(
            getattr(load_astrabox_settings(), "sandbox_ready_timeout_seconds", 0) or 0
        )
        bound = max(
            _MATERIALIZATION_STALL_FLOOR_SECONDS,
            ready_timeout * _MATERIALIZATION_STALL_READY_TIMEOUT_MULTIPLE,
        )
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        return age > bound

    async def _redrive_stalled_materialization(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        stalled_session: dict[str, Any],
        provisioning_sandbox_generation: str | None,
    ) -> dict[str, Any] | None:
        """Claim a stalled materialization; return None once wake owns the rebuild.

        A non-None return is the posture to report instead of rebuilding: the
        dead materializer left a sandbox whose destruction is not confirmed, or a
        concurrent wake claimed the rebuild first. Rebuilding over an
        unconfirmed sandbox would strand a running box that nothing names —
        the workspace pointer is empty here, so the dead session's row is the
        last record of that sandbox's id.
        """
        leaked_sandbox_id = str(stalled_session.get("sandbox_id") or "").strip()
        stalled_posture = {
            "state": "MATERIALIZING",
            "provisioning_session_id": provisioning_session_id,
            "retryable": True,
        }
        if leaked_sandbox_id:
            # Fence publication before supplier cleanup. The durable pointer
            # retains the orphan even if this host dies during destruction.
            adopted = await self._workspace_service.adopt_undestroyed_sandbox(
                user_id=user_id,
                assistant_id=assistant_id,
                sandbox_id=leaked_sandbox_id,
                reason="stalled_materializer_cleanup",
                expected_owner={
                    "state": "MATERIALIZING",
                    "provisioning_session_id": provisioning_session_id,
                    "provisioning_sandbox_generation": provisioning_sandbox_generation,
                },
            )
            if not adopted:
                return stalled_posture
            released = await self._retry_recovery_destruction(
                user_id=user_id,
                assistant_id=assistant_id,
                workspace={"provisioning_session_id": provisioning_session_id},
                sandbox_id=leaked_sandbox_id,
            )
            if not released:
                return {
                    "state": "RECOVERY_REQUIRED",
                    "retryable": True,
                    "recovery_pending_sandbox_id": leaked_sandbox_id,
                }
            reopened = await self._workspace_service.transition_state(
                user_id=user_id,
                assistant_id=assistant_id,
                expected_state="RECOVERY_REQUIRED",
                new_state="MATERIALIZING",
                expected_extra={"current_sandbox_id": None, "provisioning_session_id": None},
            )
            return None if reopened else stalled_posture
        claimed = await self._workspace_service.claim_stalled_materialization(
            user_id=user_id,
            assistant_id=assistant_id,
            provisioning_session_id=provisioning_session_id,
            provisioning_sandbox_generation=provisioning_sandbox_generation,
            last_error=(
                str(stalled_session.get("last_error") or "").strip()
                or (
                    "assistant workspace materializer Session "
                    f"{provisioning_session_id} ended without publishing READY"
                )
            ),
        )
        if not claimed:
            return stalled_posture
        return None

    async def _retry_recovery_destruction(
        self,
        *,
        user_id: str,
        assistant_id: str,
        workspace: dict[str, Any],
        sandbox_id: str,
    ) -> bool:
        """Retry the pending destruction of a RECOVERY_REQUIRED workspace's
        sandbox; return True once the pointer has been released.

        Only a confirmed kill releases the pointer, because the pointer is the
        sandbox's last surviving name: released early, the box would run out
        its lease with nothing able to address it. An unconfirmed kill leaves
        the workspace exactly as it was — still RECOVERY_REQUIRED, still
        pointing at that sandbox — so the next wake retries.
        """
        if self._runtime_manager is None:
            logger.warning(
                "assistant workspace recovery cannot retry the destruction "
                "without a runtime manager: assistant=%s sandbox=%s",
                assistant_id,
                sandbox_id,
            )
            return False
        try:
            destruction = await self._runtime_manager.destroy_sandbox_by_id(sandbox_id)
        except Exception:
            logger.warning(
                "assistant workspace recovery kill raised: assistant=%s sandbox=%s",
                assistant_id,
                sandbox_id,
                exc_info=True,
            )
            return False
        # The pointer is released only by a confirmed destruction of this exact
        # box. Checking the id as well as the outcome is what keeps a proof about
        # an earlier sandbox from releasing a pointer that has since moved.
        if not may_sever_last_name(destruction, sandbox_id=sandbox_id):
            logger.warning(
                "assistant workspace recovery kept the pointer: assistant=%s "
                "sandbox=%s outcome=%s (%s)",
                assistant_id,
                sandbox_id,
                destruction.outcome,
                destruction.detail,
            )
            return False
        return await self._workspace_service.release_recovered_sandbox(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=sandbox_id,
        )

    async def hibernate_workspace(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        """Save the native runtime state and release the workspace's box.

        HIBERNATING closes workspace admission; the engine then stops native
        writers and confirms database storage before the platform releases
        compute. File persistence is separate and requires an optional workspace
        volume. A MATERIALIZING workspace has no completed runtime to save, so
        hibernate only stops that unpublished orphan and refuses to forget an
        unconfirmed survivor.
        """
        await self._must_use_assistant(user, assistant_id)
        workspace = await self._workspace_service.get_workspace(
            user_id=user.user_id,
            assistant_id=assistant_id,
        )
        previous_sandbox_id = (
            str((workspace or {}).get("current_sandbox_id") or "").strip()
            or None
        )
        provisioning_session_id = str(
            (workspace or {}).get("provisioning_session_id") or ""
        ).strip()
        workspace_state = str((workspace or {}).get("state") or "").strip()
        if previous_sandbox_id:
            hibernated_at = (
                str((workspace or {}).get("hibernated_at") or "").strip()
                or utcnow_iso()
            )
            if workspace_state == "READY":
                marked = await self._workspace_service.begin_hibernation(
                    user_id=user.user_id,
                    assistant_id=assistant_id,
                    sandbox_id=previous_sandbox_id,
                    hibernated_at=hibernated_at,
                )
                if not marked:
                    raise APIError(
                        code="ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT",
                        message=(
                            f"assistant={assistant_id} workspace moved while "
                            f"hibernating sandbox {previous_sandbox_id}"
                        ),
                        status_code=409,
                    )
            elif workspace_state not in {"HIBERNATING", "RECOVERY_REQUIRED"}:
                raise APIError(
                    code="ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT",
                    message=(
                        f"assistant={assistant_id} workspace cannot hibernate from "
                        f"state={workspace_state!r}"
                    ),
                    status_code=409,
                )
            if workspace_state == "RECOVERY_REQUIRED":
                released = await self._retry_recovery_destruction(
                    user_id=user.user_id,
                    assistant_id=assistant_id,
                    workspace=workspace or {},
                    sandbox_id=previous_sandbox_id,
                )
                if released:
                    released = await self._workspace_service.finish_hibernation(
                        user_id=user.user_id,
                        assistant_id=assistant_id,
                        destroyed_sandbox_id=None,
                        expected_state="RECOVERY_REQUIRED",
                    )
                return {
                    "assistant_id": assistant_id,
                    "hibernated": released,
                    "released": released,
                    "recovery_required": not released,
                    "previous_sandbox_id": previous_sandbox_id,
                    "sandbox_id": None if released else previous_sandbox_id,
                    "hibernated_at": (workspace or {}).get("hibernated_at"),
                }
            result = await self._complete_hibernation_release(
                user_id=user.user_id,
                assistant_id=assistant_id,
                sandbox_id=previous_sandbox_id,
            )
            result["hibernated_at"] = hibernated_at
            return result

        needs_destroy = bool(
            workspace_state == "MATERIALIZING" and provisioning_session_id
        )
        destruction: SandboxDestruction | None = None
        if needs_destroy:
            if self._runtime_manager is None:
                raise APIError(
                    code="ASSISTANT_RUNTIME_MANAGER_UNAVAILABLE",
                    message="cannot hibernate assistant workspace without runtime manager",
                    status_code=500,
                )
            # No fallback id to offer: the pointer is empty in this branch, so
            # the runtime is the only thing that names the materializer's box.
            destruction = await self._runtime_manager.terminate_runtime(
                provisioning_session_id
            )
        undestroyed = (
            destruction.leaked_sandbox_id if destruction is not None else None
        )
        if undestroyed:
            # The materializer's box outlived its Session and the workspace never
            # named it. Give it the pointer, which is the one place a later wake
            # will look.
            adopted = await self._workspace_service.adopt_undestroyed_sandbox(
                user_id=user.user_id,
                assistant_id=assistant_id,
                sandbox_id=undestroyed,
                reason="hibernate_materializing_sandbox_undestroyed",
            )
            logger.error(
                "assistant hibernate could not confirm the destruction of the "
                "sandbox its materializer built: assistant=%s sandbox=%s adopted=%s (%s)",
                assistant_id,
                undestroyed,
                adopted,
                destruction.detail if destruction is not None else "",
            )
            return {
                "assistant_id": assistant_id,
                "hibernated": False,
                "released": False,
                "recovery_required": bool(adopted),
                "previous_sandbox_id": None,
                "sandbox_id": undestroyed,
            }
        hibernated_at = utcnow_iso()
        ok = await self._workspace_service.finish_hibernation(
            user_id=user.user_id,
            assistant_id=assistant_id,
            destroyed_sandbox_id=None,
            expected_state=workspace_state or "HIBERNATING",
        )
        return {
            "assistant_id": assistant_id,
            "hibernated": ok,
            "released": bool(destruction is not None and destruction.confirmed),
            "recovery_required": False,
            "previous_sandbox_id": None,
            "sandbox_id": None,
        }

    async def destroy_workspace(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        await self._must_use_assistant(user, assistant_id)
        ok = await self._workspace_service.mark_recovery_required(
            user_id=user.user_id,
            assistant_id=assistant_id,
            reason="explicit destroy_workspace",
        )
        return {"assistant_id": assistant_id, "destroyed": ok}

    async def start_conversation(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        permission_mode: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        assistant = await self._must_use_assistant(user, assistant_id)
        engine_kind = self._require_engine_kind(assistant)
        environment_name = str(assistant.get("environment_name") or "").strip()
        if not environment_name:
            raise APIError(
                code="ASSISTANT_INVALID",
                message=f"assistant={assistant_id} has no environment_name",
                status_code=500,
            )
        effective_permission_mode = (
            str(permission_mode or "").strip()
            or str(assistant.get("permission_mode_default") or "").strip()
            or None
        )
        # engine_kind goes into workspace_ref so the lifecycle worker's startup
        # command can pick the right engine adapter without making a second
        # round-trip to assistant_catalog. The catalog row's engine_kind is
        # immutable-after-materialize (see _IMMUTABLE_AFTER_MATERIALIZE at the
        # top of this module), so caching it on the session's workspace_ref is
        # not stale-data-prone — any change to engine_kind requires destroying
        # and recreating the workspace, which would mean creating a fresh
        # assistant row anyway.
        create_options: dict[str, Any] = {
            "permission_mode": effective_permission_mode,
            "session_kind": "assistant_chat",
            "workspace_ref": {
                "kind": "assistant",
                "user_id": user.user_id,
                "assistant_id": assistant_id,
                "engine_kind": engine_kind,
            },
        }
        if idempotency_key is not None:
            create_options["idempotency_key"] = idempotency_key
        return await self._session_kernel.create_session(
            user,
            environment_name,
            **create_options,
        )

    async def _must_use_assistant(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        """Resolve an assistant the caller owns.

        Missing and foreign assistants share one non-disclosing 404 (the same
        policy as webhook mutations): existence must not leak across owners.
        """
        assistant = await self._catalog_repo.get_assistant(assistant_id)
        owner_id = str((assistant or {}).get("owner_id") or "").strip()
        if assistant is None or not owner_id or owner_id != user.user_id:
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message=f"assistant={assistant_id} not found",
                status_code=404,
            )
        return assistant

    @staticmethod
    def _sanitize_assistant(doc: dict[str, Any] | None) -> dict[str, Any]:
        if not doc:
            return {}
        clean = dict(doc)
        clean.pop("_id", None)
        clean.pop("deleted", None)
        clean.pop("credential_vault_ids", None)
        clean.pop("credentials_updated_by", None)
        clean.pop("credentials_updated_at", None)
        return clean
