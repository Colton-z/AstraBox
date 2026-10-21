from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.documentation import object_schema, request_body
from astrabox.api.routes.request_schemas import DEPLOYMENT_CREATE, DEPLOYMENT_UPDATE
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import get_platform_service
from astrabox.deploy.public_path import public_url
from astrabox.seams.channel import ChannelDescriptor

_registered_on: int | None = None


class DeploymentRecord(BaseModel):
    """One trigger binding on an Agent or Assistant, as ``DeploymentService`` stores it.

    ``secret`` is the credential the calling system signs with, returned so the
    configurer can register it there. A record reports the settings its binding
    has; ``extra="allow"`` carries the ones a scene adds (``channel_config``,
    ``schedule``, ``callback_base_url``, ...).
    """

    model_config = ConfigDict(extra="allow")

    deployment_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    assistant_id: str | None = None
    assistant_name: str | None = None
    allowed_participants: list[str] | None = None
    created_by: str | None = None
    scope: str | None = None
    owner_user_id: str | None = None
    owner_org_id: str | None = None
    source_assistant_id: str | None = None
    enabled: bool | None = None
    scene: str | None = None
    prompt_prefix: str | None = None
    attention_policy: str | None = None
    response_instruction: str | None = None
    execution_mode: str | None = None
    execution_user_id: str | None = None
    secret: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DeploymentRun(BaseModel):
    """The public Run projection produced by DeploymentService._render_run."""

    run_id: str
    deployment_id: str
    agent_id: str | None
    assistant_id: str | None
    trigger: str
    status: str
    scheduled_for: str | None
    replayed_from_run_id: str | None
    session_id: str | None
    input_id: str | None
    error: str | None
    created_at: str | None


@dataclass(frozen=True)
class ChannelProviderRecord(ChannelDescriptor):
    """The native provider descriptor plus platform configuration permissions."""

    scene: str = ""
    supports_source: bool = False
    uses_trigger_secret: bool = False
    can_configure_execution_user: bool = False


class PersonalScheduleCreate(BaseModel):
    """The owner and execution identity come only from the authenticated request."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str
    source_assistant_id: str
    name: str
    prompt_prefix: str
    schedule: dict[str, Any]


class DeploymentTriggerAck(BaseModel):
    """What an inbound trigger is told once the work is durable.

    A channel provider may merge its own acknowledgement fields into this
    answer — a platform echoing back a verification challenge, for instance —
    so unlisted members are part of the contract, not an accident.
    """

    model_config = ConfigDict(extra="allow")

    deployment_id: str
    session_id: str | None = None
    status: str


class DeletedDeployment(BaseModel):
    """The binding that is gone. ``deleted`` confirms it and its channel records were deleted."""

    model_config = ConfigDict(extra="allow")

    deployment_id: str
    deleted: bool


class ChannelAccountLinkRecord(BaseModel):
    """One participant of a trigger and the AstraBox account its messages run as."""

    participant: str
    user_id: str
    linked_by: str
    linked_at: str


class DeletedChannelAccountLink(BaseModel):
    """The participant whose link is gone; ``deleted`` is false if it had none."""

    participant: str
    deleted: bool


def register_deployment_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_platform_service()

    @app.api_route(
        "/api/v1/deployments/{deployment_id}/callback/{callback_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        # This is a byte-preserving platform callback, not a JSON API for
        # AstraBox clients. Each provider owns its request and response shape.
        include_in_schema=False,
        openapi_extra={
            "tags": ["Channels"],
            "summary": "Forward a channel provider callback",
            "description": "Provider-owned callback path, query, headers, authentication, body and status. Bytes are forwarded unchanged to the provider advertised by /api/v1/channel-providers; use its documentation_url and callback_path.",
            "requestBody": {
                "required": False,
                "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
            },
            "responses": {
                "default": {
                    "description": "Provider-owned status, headers and raw response body.",
                    "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
                }
            },
        },
    )
    async def channel_callback(
        deployment_id: str, callback_path: str, request: Request
    ):
        try:
            result = await _service.forward_channel_callback(
                deployment_id,
                method=request.method,
                path=f"/{callback_path.lstrip('/')}",
                query=str(request.url.query),
                headers={key.lower(): value for key, value in request.headers.items()},
                raw_body=await request.body(),
            )
            hop_by_hop = {
                "connection",
                "content-length",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "transfer-encoding",
                "upgrade",
            }
            headers = {
                key: value
                for key, value in result.headers.items()
                if key.lower() not in hop_by_hop
            }
            return Response(
                content=result.body,
                status_code=result.status_code,
                headers=headers,
            )
        except APIError as exc:
            return JSONResponse(status_code=exc.status_code, content=error_response(exc))

    # ── Inbound trigger (no login session; auth = the trigger's credential) ──
    # The identity middleware exempts /api/v1/deployments/ from the front-door
    # gate (``identity_middleware._DEFAULT_EXEMPT_PREFIXES``), so the only
    # routes that may live under this prefix are trigger endpoints whose
    # credential is the deployment's own signature (HMAC / scheduler secret /
    # channel provider auth). Admin CRUD lives under /api/v1/admin/** behind
    # the identity gate.
    @app.post(
        "/api/v1/deployments/{deployment_id}/trigger",
        response_model=ApiEnvelope[DeploymentTriggerAck],
        response_model_exclude_unset=True,
        openapi_extra={
            "description": "Authenticate with the configured trigger scene: HMAC webhook headers or the native channel provider credential. Request bytes follow that scene; see the webhook and channel guides.",
            "parameters": [
                {
                    "name": name,
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Required by the HMAC webhook scene.",
                }
                for name in ("X-Webhook-Timestamp", "X-Webhook-Signature")
            ],
            "requestBody": {
                "required": False,
                "content": {
                    "application/json": {
                        "schema": {
                            "description": "Webhook JSON event; provider scenes define their own payload."
                        }
                    },
                    "*/*": {"schema": {"type": "string", "format": "binary"}},
                },
            },
        },
    )
    async def trigger_deployment(deployment_id: str, request: Request):
        try:
            raw_body = await request.body()
            headers = {k.lower(): v for k, v in request.headers.items()}
            result = await _service.trigger_deployment(
                deployment_id, headers=headers, raw_body=raw_body
            )
            return success_response(result)
        except APIError as exc:
            return JSONResponse(status_code=exc.status_code, content=error_response(exc))

    @app.get("/api/v1/me/schedules", response_model=ApiEnvelope[list[DeploymentRecord]])
    async def list_personal_schedules(request: Request, source_assistant_id: str | None = None):
        user = await get_current_user_context(request)
        return success_response(await _service.list_personal_schedules(
            user, source_assistant_id=source_assistant_id,
        ))

    @app.post("/api/v1/me/schedules", response_model=ApiEnvelope[DeploymentRecord])
    async def create_personal_schedule(payload: PersonalScheduleCreate, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.create_personal_schedule(
            user, agent_id=payload.agent_id, source_assistant_id=payload.source_assistant_id,
            name=payload.name, prompt_prefix=payload.prompt_prefix, schedule=payload.schedule,
        ))

    @app.put(
        "/api/v1/me/schedules/{deployment_id}",
        response_model=ApiEnvelope[DeploymentRecord],
        openapi_extra=request_body(
            object_schema(
                {
                    "name": {"type": "string"},
                    "prompt_prefix": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "schedule": DEPLOYMENT_UPDATE["properties"]["schedule"],
                }
            )
        ),
    )
    async def update_personal_schedule(deployment_id: str, patch: dict[str, Any], request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.update_personal_schedule(user, deployment_id, patch=patch))

    @app.delete("/api/v1/me/schedules/{deployment_id}", response_model=ApiEnvelope[DeletedDeployment])
    async def delete_personal_schedule(deployment_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.delete_personal_schedule(user, deployment_id))

    @app.get(
        "/api/v1/me/schedules/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[list[DeploymentRun]]}},
    )
    async def list_personal_schedule_runs(deployment_id: str, request: Request, limit: int = 50):
        user = await get_current_user_context(request)
        return success_response(await _service.list_personal_schedule_runs(user, deployment_id, limit=limit))

    @app.post(
        "/api/v1/me/schedules/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def trigger_personal_schedule(deployment_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.trigger_personal_schedule(user, deployment_id))

    @app.post(
        "/api/v1/me/schedules/{deployment_id}/runs/{run_id}/replay",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def replay_personal_schedule_run(deployment_id: str, run_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.replay_personal_schedule_run(user, deployment_id, run_id))

    # ── Authenticated agent management ──────────────────────────────────────
    @app.get(
        "/api/v1/admin/deployments",
        response_model=ApiEnvelope[list[DeploymentRecord]],
        response_model_exclude_unset=True,
    )
    async def list_deployments(request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_deployments(user)
        return success_response(result)

    @app.get(
        "/api/v1/admin/agents/{agent_id}/deployments",
        response_model=ApiEnvelope[list[DeploymentRecord]],
        response_model_exclude_unset=True,
    )
    async def list_agent_deployments(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_agent_deployments(user, agent_id)
        return success_response(result)

    @app.get(
        "/api/v1/admin/channel-providers",
        responses={200: {"model": ApiEnvelope[list[ChannelProviderRecord]]}},
    )
    async def list_channel_providers(request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_channel_providers(user)
        return success_response(result)

    # The provider catalog is not administrator data: an Assistant owner needs
    # it to bind their own channel.
    @app.get(
        "/api/v1/channel-providers",
        responses={200: {"model": ApiEnvelope[list[ChannelProviderRecord]]}},
    )
    async def list_channel_providers_for_owner(request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_channel_providers(user)
        return success_response(result)

    @app.post(
        "/api/v1/admin/agents/{agent_id}/deployments",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
        openapi_extra=request_body(DEPLOYMENT_CREATE),
    )
    async def create_agent_deployment(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        payload = await request.json()
        result = await _service.create_agent_deployment(
            user,
            agent_id,
            payload,
            callback_base_url=public_url(request, ""),
        )
        return success_response(result)

    @app.put(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
        openapi_extra=request_body(DEPLOYMENT_UPDATE),
    )
    async def update_agent_deployment(agent_id: str, deployment_id: str, request: Request):
        user = await get_current_user_context(request)
        patch = await request.json()
        result = await _service.update_agent_deployment(user, agent_id, deployment_id, patch)
        return success_response(result)

    @app.delete(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}",
        response_model=ApiEnvelope[DeletedDeployment],
        response_model_exclude_unset=True,
    )
    async def delete_agent_deployment(agent_id: str, deployment_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.delete_agent_deployment(user, agent_id, deployment_id)
        return success_response(result)

    @app.get(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links",
        response_model=ApiEnvelope[list[ChannelAccountLinkRecord]],
    )
    async def list_agent_deployment_account_links(
        agent_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.list_agent_deployment_account_links(
            user, agent_id, deployment_id
        )
        return success_response(result)

    @app.put(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links",
        response_model=ApiEnvelope[ChannelAccountLinkRecord],
        openapi_extra=request_body(
            object_schema(
                {"participant": {"type": "string"}, "user_id": {"type": "string"}},
                required=("participant", "user_id"),
            )
        ),
    )
    async def put_agent_deployment_account_link(
        agent_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        payload = await request.json()
        result = await _service.put_agent_deployment_account_link(
            user, agent_id, deployment_id, payload if isinstance(payload, dict) else {}
        )
        return success_response(result)

    @app.delete(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/account-links",
        response_model=ApiEnvelope[DeletedChannelAccountLink],
    )
    async def delete_agent_deployment_account_link(
        agent_id: str, deployment_id: str, participant: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.delete_agent_deployment_account_link(
            user, agent_id, deployment_id, participant
        )
        return success_response(result)

    @app.get(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[list[DeploymentRun]]}},
    )
    async def list_deployment_runs(
        agent_id: str,
        deployment_id: str,
        request: Request,
        limit: int = 50,
    ):
        user = await get_current_user_context(request)
        result = await _service.list_deployment_runs(
            user, agent_id, deployment_id, limit=limit
        )
        return success_response(result)

    @app.post(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def trigger_deployment_run(
        agent_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.trigger_deployment_run(
            user, agent_id, deployment_id
        )
        return success_response(result)

    @app.post(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs/{run_id}/replay",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def replay_deployment_run(
        agent_id: str,
        deployment_id: str,
        run_id: str,
        request: Request,
    ):
        user = await get_current_user_context(request)
        result = await _service.replay_deployment_run(
            user, agent_id, deployment_id, run_id
        )
        return success_response(result)

    # ── Assistant trigger bindings (owner-only, outside /admin) ──────────────
    @app.get(
        "/api/v1/assistants/{assistant_id}/deployments",
        response_model=ApiEnvelope[list[DeploymentRecord]],
        response_model_exclude_unset=True,
    )
    async def list_assistant_deployments(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_assistant_deployments(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/deployments",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
        openapi_extra=request_body(DEPLOYMENT_CREATE),
    )
    async def create_assistant_deployment(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        payload = await request.json()
        result = await _service.create_assistant_deployment(
            user,
            assistant_id,
            payload,
            callback_base_url=public_url(request, ""),
        )
        return success_response(result)

    @app.put(
        "/api/v1/assistants/{assistant_id}/deployments/{deployment_id}",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
        openapi_extra=request_body(DEPLOYMENT_UPDATE),
    )
    async def update_assistant_deployment(
        assistant_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        patch = await request.json()
        result = await _service.update_assistant_deployment(
            user, assistant_id, deployment_id, patch
        )
        return success_response(result)

    @app.delete(
        "/api/v1/assistants/{assistant_id}/deployments/{deployment_id}",
        response_model=ApiEnvelope[DeletedDeployment],
        response_model_exclude_unset=True,
    )
    async def delete_assistant_deployment(
        assistant_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.delete_assistant_deployment(
            user, assistant_id, deployment_id
        )
        return success_response(result)

    @app.get(
        "/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[list[DeploymentRun]]}},
    )
    async def list_assistant_deployment_runs(
        assistant_id: str, deployment_id: str, request: Request, limit: int = 50,
    ):
        user = await get_current_user_context(request)
        result = await _service.list_assistant_deployment_runs(
            user, assistant_id, deployment_id, limit=limit
        )
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def trigger_assistant_deployment_run(
        assistant_id: str, deployment_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.trigger_assistant_deployment_run(
            user, assistant_id, deployment_id
        )
        return success_response(result)

    @app.post(
        "/api/v1/assistants/{assistant_id}/deployments/{deployment_id}/runs/{run_id}/replay",
        responses={200: {"model": ApiEnvelope[DeploymentRun]}},
    )
    async def replay_assistant_deployment_run(
        assistant_id: str, deployment_id: str, run_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        result = await _service.replay_assistant_deployment_run(
            user, assistant_id, deployment_id, run_id
        )
        return success_response(result)
