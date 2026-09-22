"""Shared session helpers for the session kernel.

Runtime startup, recovery, and stream ownership are owned by
``session_kernel`` workers.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from astrabox.persistence.repository import MessageRepository, SessionRepository
from astrabox.persistence.repository.user_profile_repository import UserProfileRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, plus_seconds_iso, utcnow
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.event_broker import SessionEventBroker
from astrabox.core.service.orchestrator.message_blocks import normalize_message_blocks
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
)
from astrabox.seams.sandbox import (
    sandbox_for_name,
    sandbox_name_for_template,
)
# The built-in open_sandbox provider self-registers via
# astrabox.providers.register_builtin_providers() at runtime_manager / app
# bootstrap, so no side-effect import is needed here.
from astrabox.core.service.orchestrator.sandbox_lifecycle import new_sandbox_callback_fields
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    require_engine_for_session_kind,
)
from astrabox.core.service.orchestrator.slash_commands import (
    normalize_slash_command_details,
    normalize_slash_commands,
)
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.vault_service import VaultService

logger = get_logger(__name__)

_SESSION_CREATION_TIMEOUT_SECONDS = int(
    os.getenv("ASTRABOX_SESSION_CREATION_TIMEOUT_SECONDS", "") or 300
)


class SessionService:
    def __init__(
        self,
        *,
        sessions_repo: SessionRepository,
        messages_repo: MessageRepository,
        agent_config: AgentConfigService,
        runtime_manager: RemoteAgentRuntimeManager,
        broker: SessionEventBroker,
        ttl_seconds: int,
        spawn_background_task,
        assistant_workspace_service: Any | None = None,
        vault_service: VaultService | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._messages_repo = messages_repo
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._broker = broker
        self._ttl_seconds = ttl_seconds
        self._spawn_background_task = spawn_background_task
        self._assistant_workspace_service = assistant_workspace_service
        self._vault_service = vault_service or VaultService()
        self._session_snapshots_repo = None
        self._interaction_snapshots_repo = None

    async def create_session_record(
        self,
        user: UserContext,
        template_name: str,
        permission_mode: str | None = None,
        *,
        hidden: bool = False,
        session_kind: str = "agent_chat",
        owner_type: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        workspace_ref: dict[str, Any] | None = None,
        source_type: str | None = None,
        agent_id: str | None = None,
        deployment_name: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        # The Session resolves its Agent or Assistant by identity (``agent_id``
        # or ``workspace_ref``), never by a mutable display name.
        template = await self._agent_config.resolve_session_harness(
            {"workspace_ref": workspace_ref, "agent_id": agent_id, "owner_id": owner_id}
        )
        if template is None:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message=f"agent '{agent_id or template_name}' not found or not permitted",
                status_code=403,
            )

        resolved_sandbox_backend = sandbox_name_for_template(template)
        managed_vault_ids = list(
            getattr(template, "credential_vault_ids", None) or []
        )
        validated_vault_ids: list[str] = []
        if managed_vault_ids:
            sandbox_provider = sandbox_for_name(resolved_sandbox_backend)
            provider_supports_egress_injection = bool(
                getattr(
                    sandbox_provider,
                    "supports_egress_credential_injection",
                    False,
                )
            )
            assistant_uses_shared_workspace = session_kind == "assistant_chat"
            validated_vault_ids = await self._vault_service.validate_bound_vaults_for_session(
                managed_vault_ids,
                backend_name=resolved_sandbox_backend,
                backend_supports_egress_injection=(
                    provider_supports_egress_injection
                    and not assistant_uses_shared_workspace
                ),
                egress_injection_unsupported_reason=(
                    "Assistant conversations reuse a long-running workspace and currently "
                    "support saved MCP credentials only; they cannot safely add a "
                    "conversation-specific environment variable to that workspace."
                    if assistant_uses_shared_workspace
                    and provider_supports_egress_injection
                    else None
                ),
            )

        # Admission for the sandbox this session will provision (deployment
        # quota policy — no-op by default). A hidden system session (the
        # shared-workspace bootstrap row) is infrastructure, not a user's
        # sandbox request, so it is exempt.
        if not hidden:
            from astrabox.seams.admission import (
                ADMISSION_KIND_SANDBOX,
                AdmissionRequest,
                enforce_admission,
            )

            await enforce_admission(
                AdmissionRequest(
                    kind=ADMISSION_KIND_SANDBOX,
                    user_id=str(getattr(user, "user_id", "") or ""),
                    agent_id=str(agent_id or "").strip() or None,
                    template_name=template_name,
                )
            )
            from astrabox.observability.metrics import (
                METRIC_SANDBOXES_CREATED,
                increment,
            )

            increment(METRIC_SANDBOXES_CREATED)

        if workspace_ref is None:
            raise APIError(
                code="WORKSPACE_REF_REQUIRED",
                message=f"workspace_ref must be provided for session_kind={session_kind!r}",
                status_code=500,
            )

        resolved_session_id = str(session_id or uuid.uuid4())
        sandbox_generation, sandbox_callback_token = new_sandbox_callback_fields()
        engine_kind = require_engine_for_session_kind(
            str(getattr(template, "engine_kind", "") or "").strip(),
            session_kind,
        )
        effective_permission_mode = PermissionLifecycle.resolve_initial_mode(
            permission_mode,
            session_kind=session_kind,
            engine_kind=engine_kind,
        )
        resolved_model_name = self._runtime_manager.resolve_template_model_name(template)
        payload: dict[str, Any] = {
            "session_id": resolved_session_id,
            "user_id": user.user_id,
            # Display/grouping label for the session's harness (the agent's name).
            # Resolution keys off agent_id / workspace_ref, not this string.
            "template_name": template.name,
            # Persisted backend (persisted truth): every session carries a concrete
            # backend name so by-id control-plane ops dispatch by it, never by guessing
            # from the sandbox id. The template's None means the deployment-configured
            # default backend; normalize it to the explicit name here (never a
            # privileged-backend dispatch).
            "sandbox_backend": resolved_sandbox_backend,
            # Vaults attached at create. Engine startup and root-turn
            # preparation resolve MCP headers against these ids and write them
            # only to the sandbox's egress Vault.
            "vault_ids": validated_vault_ids,
            "permission_mode": effective_permission_mode,
            "model_name": resolved_model_name,
            "slash_commands": [],
            "slash_command_details": [],
            "state": SessionState.CREATING.value,
            "sandbox_id": None,
            "engine_session_key": None,
            "expires_at": plus_seconds_iso(self._ttl_seconds),
            "title": None,
            "runtime_unavailable": False,
            "startup_progress": "creating_sandbox",
            "sandbox_generation": sandbox_generation,
            "sandbox_callback_token": sandbox_callback_token,
            "session_kind": session_kind,
            "engine_kind": engine_kind,
            "workspace_ref": workspace_ref,
        }
        if hidden:
            payload["hidden"] = True
        if owner_type:
            payload["owner_type"] = owner_type
        if owner_id:
            payload["owner_id"] = owner_id
        # agent_chat identity fields: the agent linkage lives on the session
        # row, not on a separate agent-owned binding.
        if str(source_type or "").strip():
            payload["source_type"] = str(source_type).strip()
        if str(agent_id or "").strip():
            payload["agent_id"] = str(agent_id).strip()
        if str(deployment_name or "").strip():
            payload["deployment_name"] = str(deployment_name).strip()
        if str(title or "").strip():
            payload["title"] = str(title).strip()

        created = await self._sessions_repo.create_session(payload)

        if user.display_name and user.user_id:
            try:
                await UserProfileRepository().upsert_if_absent(
                    user_id=user.user_id,
                    display_name=user.display_name,
                )
            except Exception:
                pass

        clean = dict(created) if isinstance(created, dict) else dict(payload)
        clean.pop("sandbox_callback_token", None)
        clean.pop("vault_ids", None)
        return clean

    async def must_get_owned_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """The ownership gate every user-facing session path funnels through.

        Delegates to the repository's ownership-scoped read: the owner is part
        of the query filter (atomic — no read-then-compare to forget), and a
        wrong-owner lookup is indistinguishable from a missing session (404,
        existence not revealed).
        """
        get_owned = getattr(self._sessions_repo, "get_owned_session", None)
        if callable(get_owned):
            session = await get_owned(session_id, user.user_id)
        else:
            # A repository backend without the ownership-scoped read falls
            # back to an explicit read-then-compare.
            session = await self._sessions_repo.get_session(session_id)
            if session is not None and str(session.get("user_id") or "") != user.user_id:
                session = None
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return session

    async def get_webshell_url(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self.must_get_owned_session(user, session_id)
        if is_assistant_user_conversation(session) and self._assistant_workspace_service is not None:
            session, resolution = await reconcile_session_runtime_binding(
                session=session,
                sessions_repo=self._sessions_repo,
                assistant_workspace_service=self._assistant_workspace_service,
                persist=False,
            )
            if not resolution.can_dispatch:
                raise APIError(
                    code=resolution.reason_code or "ASSISTANT_WORKSPACE_NOT_READY",
                    message=resolution.reason_message or "assistant workspace is not ready",
                    status_code=409,
                )
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not sandbox_id:
            runtime = self._runtime_manager.get_runtime(session_id)
            sandbox_id = str(runtime.sandbox_id or "").strip() if runtime else ""

        if not sandbox_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox id missing",
                status_code=409,
            )

        backend = str(session.get("sandbox_backend") or "").strip().lower()
        if not backend:
            raise APIError(
                code="INVALID_SESSION",
                message="session has no sandbox_backend, cannot resolve webshell backend",
                status_code=400,
            )
        user_id = user.user_id
        url = await sandbox_for_name(backend).webshell_url(
            sandbox_id=sandbox_id,
            user_id=user_id,
        )
        return {"session_id": session_id, "sandbox_id": sandbox_id, "url": url}

    @staticmethod
    def _is_runtime_initialize_timeout_error(exc: APIError) -> bool:
        if str(exc.code or "").strip() != "AGENT_RUNTIME_ERROR":
            return False
        message = str(exc.message or "")
        return "Control request timeout: initialize" in message

    @staticmethod
    def _is_session_expired(session: dict[str, Any]) -> bool:
        expires_at_str = str(session.get("expires_at") or "").strip()
        if not expires_at_str:
            return False
        try:
            return parse_iso(expires_at_str) <= utcnow()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _sanitize_session(session: dict[str, Any]) -> dict[str, Any]:
        clean = dict(session)
        # Per-session model: the conversation identity is planned before the
        # session's sandbox exists (POSIX uid/gid are sandbox-local), so the
        # stored runtime_identity.sandbox_id is None. The session row's own
        # sandbox_id is the canonical reference, so project it onto the identity
        # view when the identity doesn't already carry one.
        runtime_identity = clean.get("runtime_identity")
        session_sandbox_id = str(clean.get("sandbox_id") or "").strip()
        if (
            isinstance(runtime_identity, dict)
            and session_sandbox_id
            and not str(runtime_identity.get("sandbox_id") or "").strip()
        ):
            runtime_identity = dict(runtime_identity)
            runtime_identity["sandbox_id"] = session_sandbox_id
            clean["runtime_identity"] = runtime_identity
        workspace_ref = clean.get("workspace_ref")
        if isinstance(workspace_ref, dict):
            engine_kind = str(workspace_ref.get("engine_kind") or "").strip()
            if engine_kind and not str(clean.get("engine_kind") or "").strip():
                clean["engine_kind"] = engine_kind
        clean.pop("_id", None)
        clean.pop("sandbox_generation", None)
        clean.pop("_runtime_recovery_owner", None)
        clean.pop("_retained_startup_allocations", None)
        clean.pop("sandbox_callback_token", None)
        clean.pop("turn_preparation_guard", None)
        clean.pop("turn_preparation_fence_epoch", None)
        clean.pop("workspace_ref", None)
        clean.pop("vault_ids", None)
        clean["permission_mode"] = (
            str(clean.get("permission_mode") or "").strip() or None
        )
        model_name = str(clean.get("model_name") or "").strip()
        clean["model_name"] = model_name or None
        clean["slash_commands"] = normalize_slash_commands(clean.get("slash_commands"))
        clean["slash_command_details"] = normalize_slash_command_details(clean.get("slash_command_details"))
        state = str(clean.get("state") or "")
        if (
            state not in {SessionState.TERMINATED.value, SessionState.DELETED.value}
            and str(clean.get("session_kind") or "") not in {"agent_chat", "assistant_chat"}
            and SessionService._is_session_expired(clean)
        ):
            clean["state"] = SessionState.TERMINATED.value
            clean["runtime_unavailable"] = True
            clean["last_error"] = "sandbox expired"
            return clean
        if state == SessionState.CREATING.value:
            latest_touch = None
            for key in ("updated_at", "created_at"):
                raw_value = str(clean.get(key) or "").strip()
                if not raw_value:
                    continue
                try:
                    parsed = parse_iso(raw_value)
                except (TypeError, ValueError):
                    continue
                if latest_touch is None or parsed > latest_touch:
                    latest_touch = parsed
            if latest_touch is not None:
                if (utcnow() - latest_touch).total_seconds() > _SESSION_CREATION_TIMEOUT_SECONDS:
                    clean["state"] = SessionState.TERMINATED.value
                    clean["runtime_unavailable"] = True
                    clean["last_error"] = "session creation timed out"
                    return clean
        if (
            str(clean.get("state") or "") == SessionState.READY.value
            and bool(clean.get("runtime_unavailable"))
        ):
            clean["state"] = SessionState.RECOVERY_REQUIRED.value
        (
            clean["recovery_policy"],
            clean["recovery_reason"],
        ) = SessionService.derive_recovery_fields(
            str(clean.get("state") or ""), clean
        )
        return clean

    @staticmethod
    def derive_recovery_fields(
        state: str, row: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """The client-visible recovery posture for one rendered session.

        Shared by direct/admin reads and the snapshot-backed Session read so
        the two can never drift. Outside ``RECOVERY_REQUIRED`` both
        fields are ``None``. Inside it, the persisted reason — what the
        recover worker actually determined (``sandbox_missing`` /
        ``sandbox_expired`` / ``sandbox_reattach_failed``) — wins; the
        heuristics below are only the fallback vocabulary for rows no
        recovery pass has classified yet.
        """
        if state != SessionState.RECOVERY_REQUIRED.value:
            return None, None
        policy = str(row.get("recovery_policy") or "").strip().lower() or "auto"
        reason = str(row.get("recovery_reason") or "").strip()
        if reason:
            return policy, reason
        if bool(row.get("runtime_unavailable")):
            return policy, "runtime_detached"
        return policy, "session_recovery_required"

    @staticmethod
    def _sanitize_message(message: dict[str, Any]) -> dict[str, Any]:
        clean = dict(message)
        clean.pop("_id", None)
        if isinstance(clean.get("blocks"), list):
            clean["blocks"] = normalize_message_blocks(clean.get("blocks"))
        return clean
