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

from astrabox.api.documentation import query_parameter, request_body
from astrabox.api.routes._shared import list_page_limit
from astrabox.api.routes.conversation_models import (
    StartConversationRequest,
    conversation_idempotency_key,
)
from astrabox.api.routes.request_schemas import ASSISTANT_CREATE, ASSISTANT_UPDATE
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.connectors.service import AssistantConnectors, ConnectorService
from astrabox.core.service.orchestrator.service_registry import (
    get_assistant_service,
    get_platform_service,
)
from astrabox.seams.skills import SkillDescriptor

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


class InheritedExtensions(BaseModel):
    """What an Assistant inherits from the layers above it, by name and source.

    Credential Vaults are counted, not named; nothing here can be edited
    through the Assistant.
    """

    model_config = ConfigDict(extra="forbid")

    instructions: bool
    #: Skill sources the layers write themselves.
    skills: list[SkillDescriptor]
    #: Skill sources the layers select from the extension catalog. They reach
    #: the Assistant only while the deployment runs one
    #: (``/api/v1/extension-provider`` is not null).
    catalog_skills: list[SkillDescriptor]
    mcp_servers: list[str]
    #: How many MCP servers the layers select from extension catalogs, which
    #: likewise need the deployment to run one.
    catalog_mcp_servers: int
    packages: list[dict[str, Any]]
    credential_vaults: int


class AssistantScheduledJobs(BaseModel):
    """What an Assistant scheduled for itself, read from its program's own store.

    Each job carries the program's own field names (Hermes: ``id``, ``name``,
    ``prompt``, ``schedule_display``, ``state``, ``next_run_at``,
    ``last_run_at``, ``last_status``, ...). ``jobs`` is null when there is no
    live list to read, and ``reason`` says why: ``workspace_not_running`` or
    ``engine_keeps_no_schedule``.
    """

    model_config = ConfigDict(extra="forbid")

    assistant_id: str
    workspace_state: str
    jobs: list[dict[str, Any]] | None
    reason: str | None


class EnsurePrimaryAssistantRequest(BaseModel):
    """What a first-created primary Assistant starts with; ignored when it exists."""

    model_config = ConfigDict(extra="forbid")

    display_name: str
    timezone: str | None = None


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
    #: The system prompt, the same field an Agent carries; the Agent program
    #: applies it through its own mechanism (Hermes: its SOUL.md identity).
    system: str | None = None
    #: The owner's IANA timezone, which the Agent program keeps time in.
    timezone: str | None = None
    model_config_override: dict[str, Any] | None = None
    mcp_servers: dict[str, Any] | None = None
    #: MCP servers selected from extension catalogs, as stored:
    #: ``{provider, item_id}`` rows. Written only through ``/extensions``.
    mcp_assignments: list[dict[str, Any]] | None = None
    #: Skills selected from the extension catalog, as stored: ``skill_ids``
    #: and the Git ``skills`` they compiled to. Written only through
    #: ``/extensions``.
    extension_catalog: dict[str, Any] | None = None
    packages: list[dict[str, Any]] | None = None
    skills: list[SkillDescriptor] | None = None
    #: The engine's native configuration documents (Hermes: ``config``, merged
    #: into its config.yaml), as declared by the engine.
    engine_options: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    workspace_state: str | None = None
    current_sandbox_id: str | None = None
    #: The one Assistant the platform keeps for its owner (POST /assistants/primary).
    primary: bool | None = None
    #: Where opening the Assistant lands (POST /assistants/{id}/main-conversation).
    main_session_id: str | None = None
    #: What the deployment's and organization's Assistant defaults provide, read-only.
    inherited: InheritedExtensions | None = None


class AssistantListPage(BaseModel):
    """One page of the caller's Assistants, the most recently edited first.

    ``next_cursor`` is the value to pass back as ``cursor``; it is ``null`` on
    the last page, which ``has_more`` reports independently. ``total`` and
    ``ready`` count every Assistant the caller owns, whatever ``q`` and
    ``status`` narrowed, and come with the first page only.
    """

    model_config = ConfigDict(extra="forbid")

    assistants: list[Assistant]
    has_more: bool
    next_cursor: str | None = None
    total: int | None = None
    ready: int | None = None


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
    _connectors = ConnectorService()
    _assistant_connectors = AssistantConnectors(service_getter=get_assistant_service)

    @app.post(
        "/api/v1/assistants",
        response_model=ApiEnvelope[Assistant],
        response_model_exclude_unset=True,
        openapi_extra=request_body(ASSISTANT_CREATE),
    )
    async def create_assistant(request: Request, payload: dict[str, Any] = Body(...)):
        user = await get_current_user_context(request)
        # The apps the owner has connected are offered from the start. Read
        # before creating, so a hub that cannot answer creates nothing.
        connected = await _connectors.connected(user)
        result = await _service.create_assistant(user, payload)
        if connected:
            assistant_id = str(result["assistant_id"])
            await _assistant_connectors.adopt(user, connected, assistant_ids=[assistant_id])
            result = await _service.get_assistant(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/assistants/primary",
        response_model=ApiEnvelope[Assistant],
        response_model_exclude_unset=True,
    )
    async def ensure_primary_assistant(request: Request, body: EnsurePrimaryAssistantRequest):
        user = await get_current_user_context(request)
        # Read before creating, as for any new Assistant, so a hub that cannot
        # answer creates nothing.
        connected = await _connectors.connected(user)
        assistant, created = await _service.ensure_primary_assistant(
            user, display_name=body.display_name, timezone=body.timezone
        )
        if created and connected:
            assistant_id = str(assistant["assistant_id"])
            await _assistant_connectors.adopt(user, connected, assistant_ids=[assistant_id])
            assistant = await _service.get_assistant(user, assistant_id)
        return success_response(assistant)

    @app.get(
        "/api/v1/assistants",
        # Two shapes, like GET /api/v1/agents: `page=1` answers one page
        # (limit, cursor, q, status) for the console's list; without it the
        # answer is every Assistant the caller owns.
        response_model=ApiEnvelope[AssistantListPage] | ApiEnvelope[list[Assistant]],
        response_model_exclude_unset=True,
        openapi_extra={
            "parameters": [
                query_parameter(
                    "page",
                    "integer",
                    "Set to 1 for a cursor page; omitted returns all owned Assistants.",
                    enum=[1],
                ),
                query_parameter("limit", "integer", "Page size, clamped to 1..100.", default=50),
                query_parameter("cursor", "string", "Opaque next_cursor from the previous page."),
                query_parameter("q", "string", "Assistant search."),
                query_parameter("status", "string", "Workspace-state filter.", default="all"),
            ]
        },
    )
    async def list_assistants(request: Request):
        user = await get_current_user_context(request)
        params = request.query_params
        if str(params.get("page") or "").strip() == "1":
            page = await _service.list_assistants_page(
                user,
                limit=list_page_limit(params.get("limit")),
                cursor=params.get("cursor"),
                query=str(params.get("q") or ""),
                status=str(params.get("status") or "all").strip() or "all",
            )
            return success_response(page)
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
        openapi_extra=request_body(ASSISTANT_UPDATE),
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
        # Ownership is checked before anything is deleted; the conversations go
        # first, while the Assistant they resolve through still exists.
        await _service.get_assistant(user, assistant_id)
        await get_platform_service().delete_assistant_conversations(user, assistant_id)
        # The Assistant, its workspace and its Vault bindings go in one
        # transaction, once its box is proven destroyed.
        result = await _service.delete_assistant(user, assistant_id)
        # The Assistant is gone, so its schedules and channel bindings, and the
        # personal schedules created from it, must stop firing; they belong to
        # it and no other account can manage them.
        await get_platform_service().delete_target_deployments(assistant_id=assistant_id)
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

    @app.get(
        "/api/v1/assistants/{assistant_id}/scheduled-jobs",
        response_model=ApiEnvelope[AssistantScheduledJobs],
        response_model_exclude_unset=True,
    )
    async def list_assistant_scheduled_jobs(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.list_scheduled_jobs(user, assistant_id))

    @app.post(
        "/api/v1/assistants/{assistant_id}/main-conversation",
        response_model=ApiEnvelope[StartedAssistantConversation],
        response_model_exclude_unset=True,
    )
    async def open_main_conversation(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.open_main_conversation(user, assistant_id))

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
