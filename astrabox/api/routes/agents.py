"""Controller for Agent CRUD and lifecycle operations."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Body, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.api.routes.conversation_models import (
    StartConversationRequest,
    conversation_idempotency_key,
)
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import (
    get_agent_service,
    get_platform_service,
)
from astrabox.core.service.orchestrator.agent_schema import get_agent_schema

_registered_on: int | None = None


class SetAgentAccessRequest(BaseModel):
    """The complete policy accepted by the dedicated Agent access endpoint."""

    model_config = ConfigDict(extra="forbid")

    visibility: Literal["public", "private", "allowlist"]
    admins: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)


class AgentRecord(BaseModel):
    """One stored Agent as these routes project it.

    The Agent document is open — ``AgentService._sanitize`` passes through
    every key that is not internal — so the fields named here are the ones the
    console addresses by name, and ``extra="allow"`` carries the rest. Their
    types are the ones ``agent_schema.AGENT_FIELD_SCHEMA`` enforces on write,
    plus the identity and ownership fields ``AgentConfigService`` stamps.

    ``can_manage`` is computed per viewer and therefore present only on the
    reads that compute it (list and get), not on a write's echo of the record.
    """

    # `model` is an Agent field; pydantic's default
    # `model_` protected namespace would refuse the second one.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    agent_id: str
    name: str | None = None
    description: str | None = None
    use_cases: list[str] | None = None
    display_meta: dict[str, Any] | None = None
    model: str | None = None
    system: str | None = None
    claude_options: dict[str, Any] | None = None
    skills: list[str] | None = None
    mcp_servers: dict[str, Any] | None = None
    default_repo: dict[str, Any] | None = None
    plugin_repos: list[dict[str, Any]] | None = None
    environment_name: str | None = None
    exposure_mode: str | None = None
    idle_hibernate_seconds: int | None = None
    prewarm_enabled: bool | None = None
    enabled: bool | None = None
    created_by: str | None = None
    visibility: str | None = None
    admins: list[str] | None = None
    allowed_user_ids: list[str] | None = None
    version: int | None = None
    state: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    can_manage: bool | None = None


class AgentAccess(BaseModel):
    """The Agent authorization settings returned by ``_agent_access_view``.

    ``created_by`` is stamped on insert and is absent from documents seeded
    without an owner, so it is the one optional member of the response.
    """

    model_config = ConfigDict(extra="allow")

    created_by: str | None = None
    visibility: Literal["public", "private", "allowlist"]
    admins: list[str]
    allowed_user_ids: list[str]


class AgentFormField(BaseModel):
    """One field of the Agent authoring form, as ``agent_schema`` declares it.

    ``key`` and ``type`` are the two members every entry carries; the optional
    presentation and validation members (``group``, ``required``, ``enum``,
    ``path``, ``complex``, ``advanced``) appear per field. ``item_schema``
    describes the sub-fields of a nested object with this same grammar.
    """

    model_config = ConfigDict(extra="allow")

    key: str
    type: str
    group: str | None = None
    required: bool | None = None
    enum: list[str] | None = None
    path: str | None = None
    complex: bool | None = None
    advanced: bool | None = None
    item_schema: list["AgentFormField"] | None = None


class AgentFormGroup(BaseModel):
    """One form section. ``id`` is the contract; the label is frontend i18n."""

    model_config = ConfigDict(extra="allow")

    id: str


class AgentFormSchema(BaseModel):
    """The editable shape of an Agent, as the console's form editor reads it."""

    model_config = ConfigDict(extra="allow")

    version: int
    groups: list[AgentFormGroup]
    fields: list[AgentFormField]


class AgentEnvironmentOption(BaseModel):
    """One Environment an Agent author may choose, without its secrets."""

    model_config = ConfigDict(extra="allow")

    name: str
    display_name: str
    engine_kind: str
    enabled: bool
    #: The engine's declared engine_options field schema (may be empty). The
    #: platform carries the declaration for form rendering and interprets none
    #: of its keys.
    engine_options_schema: list[dict[str, Any]] = []


class AgentEnvironmentModels(BaseModel):
    """The model ids an Environment's gateway offers. Empty when it offers none."""

    model_config = ConfigDict(extra="allow")

    models: list[str]


class StartedAgentConversation(BaseModel):
    """The conversation a caller just created, addressed by ``session_id``."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    agent_id: str
    deployment_name: str | None = None


class AgentPreparedRuntimeStatus(BaseModel):
    """Platform-owned capacity waiting for this Agent's next conversation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    ready: bool
    prepared_count: int
    state: str | None = None
    placement: str | None = None
    runtime_generation: str | None = None
    client_pool_name: str | None = None
    sandbox_id: str | None = None
    last_error: str | None = None


def register_agent_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_agent_service()

    async def _resolve_user(request: Request):
        """Return the identity already verified by the configured provider.

        Agent handlers pass only ``UserContext`` into the service layer. They
        must not read, persist, or forward the browser/IdP bearer token because
        it authenticates the request and is not a managed Agent credential.
        """
        return await get_current_user_context(request)

    @app.post(
        "/api/v1/agents",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def create_agent(request: Request, payload: dict[str, Any] = Body(...)):
        user = await _resolve_user(request)
        result = await _service.create_agent(user, payload)
        return success_response(result)

    @app.get(
        "/api/v1/agents",
        response_model=ApiEnvelope[list[AgentRecord]],
        response_model_exclude_unset=True,
    )
    async def list_agents(request: Request):
        user = await _resolve_user(request)
        result = await _service.list_agents(user)
        return success_response(result)

    @app.get(
        "/api/v1/agent-configuration/schema",
        response_model=ApiEnvelope[AgentFormSchema],
        response_model_exclude_unset=True,
    )
    async def get_agent_configuration_schema(request: Request):
        """Agent form structure for any signed-in Agent author."""
        await _resolve_user(request)
        return success_response(get_agent_schema())

    @app.get(
        "/api/v1/agent-configuration/environments",
        response_model=ApiEnvelope[list[AgentEnvironmentOption]],
        response_model_exclude_unset=True,
    )
    async def list_agent_configuration_environments(request: Request):
        """Secret-free Environment choices for the Agent form."""
        await _resolve_user(request)
        result = await get_platform_service().list_agent_environment_options()
        return success_response(result)

    @app.get(
        "/api/v1/agent-configuration/environments/{name}/models",
        response_model=ApiEnvelope[AgentEnvironmentModels],
        response_model_exclude_unset=True,
    )
    async def list_agent_configuration_models(name: str, request: Request):
        """Searchable model ids exposed by the chosen Environment's gateway."""
        await _resolve_user(request)
        models = await get_platform_service().list_agent_environment_models(name)
        return success_response({"models": models})

    @app.get(
        "/api/v1/agents/{agent_id}",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def get_agent(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.get_agent(user, agent_id)
        return success_response(result)

    @app.get(
        "/api/v1/agents/{agent_id}/access",
        response_model=ApiEnvelope[AgentAccess],
        response_model_exclude_unset=True,
    )
    async def get_agent_access(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.get_agent_access(user, agent_id)
        return success_response(result)

    @app.get(
        "/api/v1/agents/{agent_id}/prepared-runtime",
        response_model=ApiEnvelope[AgentPreparedRuntimeStatus],
        response_model_exclude_unset=True,
    )
    async def get_agent_prepared_runtime(agent_id: str, request: Request):
        """The platform unit, if any, waiting for this Agent's next Session."""

        user = await _resolve_user(request)
        result = await _service.get_prepared_runtime_status(user, agent_id)
        return success_response(result)

    @app.post(
        "/api/v1/agents/{agent_id}/prepared-runtime/refresh",
        response_model=ApiEnvelope[AgentPreparedRuntimeStatus],
        response_model_exclude_unset=True,
    )
    async def refresh_agent_prepared_runtime(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.refresh_prepared_runtime(user, agent_id)
        return success_response(result)

    @app.put(
        "/api/v1/agents/{agent_id}/access",
        response_model=ApiEnvelope[AgentAccess],
        response_model_exclude_unset=True,
    )
    async def set_agent_access(
        agent_id: str,
        request: Request,
        body: SetAgentAccessRequest,
    ):
        user = await _resolve_user(request)
        result = await _service.set_agent_access(
            user,
            agent_id,
            body.model_dump(),
        )
        return success_response(result)

    @app.put(
        "/api/v1/agents/{agent_id}",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def update_agent(agent_id: str, request: Request, payload: dict[str, Any] = Body(...)):
        # This is the Agent authoring path. Its version check prevents one
        # concurrent edit from silently replacing another.
        user = await _resolve_user(request)
        result = await _service.update_agent(user, agent_id, payload)
        return success_response(result)

    @app.post(
        "/api/v1/agents/{agent_id}/wake",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def wake_agent(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.wake_agent(user, agent_id)
        return success_response(result)

    @app.post(
        "/api/v1/agents/{agent_id}/hibernate",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def hibernate_agent(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.hibernate_agent(user, agent_id)
        return success_response(result)

    @app.delete(
        "/api/v1/agents/{agent_id}",
        response_model=ApiEnvelope[AgentRecord],
        response_model_exclude_unset=True,
    )
    async def delete_agent(agent_id: str, request: Request):
        user = await _resolve_user(request)
        result = await _service.delete_agent(user, agent_id)
        return success_response(result)

    # The rows are session records, the same projection the admin session
    # surfaces answer with. Typing them belongs with that model rather than
    # forking a second Agent-local copy of it, so this pins the envelope only.
    @app.get(
        "/api/v1/agents/{agent_id}/sessions",
        response_model=ApiEnvelope[list[dict[str, Any]]],
        response_model_exclude_unset=True,
    )
    async def list_agent_sessions(agent_id: str, request: Request):
        # All conversations under one agent (agent_id-keyed). Requires the caller
        # to manage the agent (server enforces; 403 otherwise).
        user = await _resolve_user(request)
        limit = int(request.query_params.get("limit", "500") or "500")
        result = await get_platform_service().admin_list_agent_sessions(
            user, agent_id, limit=min(limit, 1000)
        )
        return success_response(result)

    @app.post(
        "/api/v1/agents/{agent_id}/conversations",
        response_model=ApiEnvelope[StartedAgentConversation],
        response_model_exclude_unset=True,
    )
    async def start_agent_conversation(
        agent_id: str,
        request: Request,
        _body: StartConversationRequest | None = Body(default=None),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            description="Opaque retry key for one conversation-create operation.",
        ),
    ):
        user = await _resolve_user(request)
        key = conversation_idempotency_key(idempotency_key)
        if key is None:
            result = await _service.start_conversation(user, agent_id)
        else:
            result = await _service.start_conversation(
                user,
                agent_id,
                idempotency_key=key,
            )
        return success_response(result)
