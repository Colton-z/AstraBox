"""Controller for Assistant CRUD and lifecycle operations.

Sessions created by ``POST /api/v1/assistants/{id}/conversations`` are
``session_kind="assistant_chat"`` with ``workspace_ref.kind="assistant"`` —
all turn / stream / interaction / interrupt / file-ops traffic
reuses the existing ``/api/v1/sessions/{id}/...`` session endpoints
(mirrors the agent-chat flow).
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Header, Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.conversation_models import (
    StartConversationRequest,
    conversation_idempotency_key,
)
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import (
    get_assistant_service,
)

logger = get_logger(__name__)

_registered_on: int | None = None


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.
#
# Every member below is optional. ``AssistantService`` reports a workspace by
# its posture, and which members accompany a posture differs between them — a
# workspace that is materializing names its bootstrap session, a ready one
# names its sandbox. Requiring a member that some posture does not carry would
# turn that posture's successful read into a 500.


class Assistant(BaseModel):
    """One Assistant's catalog row, as ``AssistantService._sanitize_assistant``
    renders it.

    The row is open — sanitizing removes the storage id and the credential
    bindings and passes the rest through — so the members named here are the
    ones the console addresses by name and ``extra="allow"`` carries the rest.

    ``workspace_state`` and ``current_sandbox_id`` are not stored on the row:
    list and detail reads join them from the workspace. An Assistant whose
    workspace was never built reports ``workspace_state="NOT_MATERIALIZED"``.
    """

    # `model_config_override` is an Assistant field; pydantic's default
    # `model_` protected namespace would refuse it.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    assistant_id: str | None = None
    owner_id: str | None = None
    display_name: str | None = None
    icon: str | None = None
    description: str | None = None
    engine_kind: str | None = None
    environment_name: str | None = None
    permission_mode_default: str | None = None
    model_config_override: dict[str, Any] | None = None
    mcp_config_override: dict[str, Any] | None = None
    plugin_repos_override: list[dict[str, Any]] | None = None
    skill_manifest_override: list[str] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    workspace_state: str | None = None
    current_sandbox_id: str | None = None


class AssistantDeletion(BaseModel):
    """Confirmation that an Assistant's catalog row was soft-deleted.

    The row is severed only after its sandbox is proven destroyed, so a caller
    that receives this knows no box outlived the name. A destruction that
    cannot be confirmed refuses with ``ASSISTANT_SANDBOX_UNDESTROYED`` instead,
    keeping the Assistant resolvable so the delete stays retryable.
    """

    model_config = ConfigDict(extra="allow")

    assistant_id: str | None = None
    deleted: bool | None = None


class AssistantWorkspace(BaseModel):
    """An Assistant workspace's posture after a wake.

    Waking is a request to reach ``READY``, not a wait for it. ``state`` says
    where the workspace is: ``READY`` names ``current_sandbox_id``;
    ``MATERIALIZING`` names the ``provisioning_session_id`` that is building it,
    which the caller polls; ``RECOVERY_REQUIRED`` names the sandbox whose
    destruction is unconfirmed, and blocks a rebuild until it is released.
    A completed hibernate has no sandbox to resume: wake materializes a fresh
    one from the configured workspace medium.
    """

    model_config = ConfigDict(extra="allow")

    assistant_id: str | None = None
    state: str | None = None
    engine_kind: str | None = None
    current_sandbox_id: str | None = None
    provisioning_session_id: str | None = None
    recovery_pending_sandbox_id: str | None = None
    retryable: bool | None = None


class AssistantWorkspaceHibernation(BaseModel):
    """The outcome of taking an Assistant workspace offline.

    The storage provider carries the files; ``released`` means the old sandbox
    was proven gone and a later wake may prepare a fresh one from that medium.
    ``recovery_required`` retains ``sandbox_id`` when destruction was not
    confirmed, so the operation remains retryable without losing the box's
    last name.
    """

    model_config = ConfigDict(extra="allow")

    assistant_id: str | None = None
    hibernated: bool | None = None
    released: bool | None = None
    recovery_required: bool | None = None
    previous_sandbox_id: str | None = None
    sandbox_id: str | None = None
    hibernated_at: str | None = None


class AssistantWorkspaceDestruction(BaseModel):
    """Confirmation that an Assistant's workspace was destroyed.

    The Assistant's catalog row survives; a later wake builds a fresh
    workspace for it.
    """

    model_config = ConfigDict(extra="allow")

    assistant_id: str | None = None
    destroyed: bool | None = None


class StartedAssistantConversation(BaseModel):
    """The conversation a caller just created, addressed by ``session_id``.

    Conversation creation answers with the session itself, so the members of a
    session read are carried here too. All turn, stream, interaction and file
    traffic for it then goes to ``/api/v1/sessions/{session_id}/...``.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    session_kind: str | None = None
    state: str | None = None


def register_assistant_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_assistant_service()

    @app.post(
        "/api/v1/assistants",
        response_model=ApiEnvelope[Assistant],
        response_model_exclude_unset=True,
    )
    async def create_assistant(request: Request, payload: dict[str, Any] = Body(...)):
        user = await get_current_user_context(request)
        result = await _service.create_assistant(user, payload)
        return success_response(result)

    @app.get(
        "/api/v1/assistants",
        response_model=ApiEnvelope[list[Assistant]],
        response_model_exclude_unset=True,
    )
    async def list_assistants(request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_assistants(user)
        return success_response(result)

    @app.get(
        "/api/v1/assistants/{assistant_id}",
        response_model=ApiEnvelope[Assistant],
        response_model_exclude_unset=True,
    )
    async def get_assistant(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.get_assistant(user, assistant_id)
        return success_response(result)

    @app.patch(
        "/api/v1/assistants/{assistant_id}",
        response_model=ApiEnvelope[Assistant],
        response_model_exclude_unset=True,
    )
    async def update_assistant(
        assistant_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ):
        user = await get_current_user_context(request)
        result = await _service.update_assistant(user, assistant_id, payload)
        return success_response(result)

    @app.delete(
        "/api/v1/assistants/{assistant_id}",
        response_model=ApiEnvelope[AssistantDeletion],
        response_model_exclude_unset=True,
    )
    async def delete_assistant(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.delete_assistant(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/workspace/wake",
        response_model=ApiEnvelope[AssistantWorkspace],
        response_model_exclude_unset=True,
    )
    async def wake_workspace(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.wake_workspace(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/workspace/hibernate",
        response_model=ApiEnvelope[AssistantWorkspaceHibernation],
        response_model_exclude_unset=True,
    )
    async def hibernate_workspace(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.hibernate_workspace(user, assistant_id)
        return success_response(result)

    @app.delete(
        "/api/v1/assistants/{assistant_id}/workspace",
        response_model=ApiEnvelope[AssistantWorkspaceDestruction],
        response_model_exclude_unset=True,
    )
    async def destroy_workspace(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.destroy_workspace(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/conversations",
        response_model=ApiEnvelope[StartedAssistantConversation],
        response_model_exclude_unset=True,
    )
    async def start_assistant_conversation(
        assistant_id: str,
        request: Request,
        _body: StartConversationRequest | None = Body(default=None),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            description="Opaque retry key for one conversation-create operation.",
        ),
    ):
        user = await get_current_user_context(request)
        key = conversation_idempotency_key(idempotency_key)
        if key is None:
            result = await _service.start_conversation(user, assistant_id)
        else:
            result = await _service.start_conversation(
                user,
                assistant_id,
                idempotency_key=key,
            )
        return success_response(result)
