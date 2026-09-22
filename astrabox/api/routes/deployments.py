from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import get_platform_service

_registered_on: int | None = None


class DeploymentRecord(BaseModel):
    """One trigger binding on an Agent, as ``DeploymentService`` stores it.

    ``secret`` is the credential the calling system signs with, returned so the
    configurer can register it there. The document is open — the service strips
    only the storage id — so ``extra="allow"`` carries anything a scene adds.
    """

    model_config = ConfigDict(extra="allow")

    deployment_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    created_by: str | None = None
    enabled: bool | None = None
    scene: str | None = None
    prompt_prefix: str | None = None
    attention_policy: str | None = None
    secret: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


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
    """The binding that is gone. ``deleted`` is the confirmed soft delete."""

    model_config = ConfigDict(extra="allow")

    deployment_id: str
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

    @app.get("/api/v1/admin/channel-providers")
    async def list_channel_providers(request: Request):
        user = await get_current_user_context(request)
        result = await _service.list_channel_providers(user)
        return success_response(result)

    @app.post(
        "/api/v1/admin/agents/{agent_id}/deployments",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
    )
    async def create_agent_deployment(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        payload = await request.json()
        result = await _service.create_agent_deployment(
            user,
            agent_id,
            payload,
            callback_base_url=str(request.base_url).rstrip("/"),
        )
        return success_response(result)

    @app.put(
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}",
        response_model=ApiEnvelope[DeploymentRecord],
        response_model_exclude_unset=True,
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
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs"
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
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs"
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
        "/api/v1/admin/agents/{agent_id}/deployments/{deployment_id}/runs/{run_id}/replay"
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
