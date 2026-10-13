"""Official A2A transport over the platform's Agent coordination service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Protocol

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import Event
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import a2a_pb2 as a2a
from a2a.utils import constants
from a2a.utils.errors import (
    InvalidParamsError,
    PushNotificationNotSupportedError,
    VersionNotSupportedError,
)
from a2a.utils.proto_utils import validate_proto_required_fields
from google.protobuf.message import Message as ProtoMessage
from packaging.version import InvalidVersion, Version
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from astrabox.common.utils.user_context import (
    UserContext,
    get_asserted_user_context,
    reset_current_user_context,
    set_current_user_context,
)
from astrabox.core.service.orchestrator.assistant_a2a_auth import resolve_assistant_a2a_user
from astrabox.deploy.public_path import public_url

A2A_RPC_PATH = "/api/v1/agents/{agent_id}/a2a"
A2A_CARD_PATH = "/api/v1/agents/{agent_id}/.well-known/agent-card.json"
ASSISTANT_A2A_RPC_PATH = "/api/v1/assistant-a2a/{binding_id}/agents/{agent_id}/a2a"
ASSISTANT_A2A_CARD_PATH = (
    "/api/v1/assistant-a2a/{binding_id}/agents/{agent_id}/.well-known/agent-card.json"
)


class AgentA2ABackend(Protocol):
    """Session-backed operations; every operation enforces live Agent and Session ACLs."""

    async def send(
        self, user: UserContext, agent_id: str, params: a2a.SendMessageRequest
    ) -> a2a.Task | a2a.Message: ...

    async def get(
        self, user: UserContext, agent_id: str, params: a2a.GetTaskRequest
    ) -> a2a.Task: ...

    async def list(
        self, user: UserContext, agent_id: str, params: a2a.ListTasksRequest
    ) -> a2a.ListTasksResponse: ...

    async def cancel(
        self, user: UserContext, agent_id: str, params: a2a.CancelTaskRequest
    ) -> a2a.Task: ...

    def stream(
        self, user: UserContext, agent_id: str, params: a2a.SendMessageRequest
    ) -> AsyncGenerator[Event, None]: ...

    def subscribe(
        self, user: UserContext, agent_id: str, params: a2a.SubscribeToTaskRequest
    ) -> AsyncGenerator[Event, None]: ...

    async def card(self, user: UserContext, agent_id: str, rpc_url: str) -> a2a.AgentCard: ...


class _ResolvedUser(User):
    def __init__(self, user: UserContext) -> None:
        self.platform_user = user

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self.platform_user.user_id


class _ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        user = getattr(request.state, "agent_a2a_user", None) or get_asserted_user_context()
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        context = super().build(request)
        context.user = _ResolvedUser(user)
        context.state["agent_id"] = request.path_params["agent_id"]
        route_name = getattr(request.state, "agent_a2a_rpc_name", "agent_a2a_rpc")
        route_params = {"agent_id": request.path_params["agent_id"]}
        if route_name == "assistant_a2a_rpc":
            route_params["binding_id"] = request.path_params["binding_id"]
        context.state["rpc_url"] = public_url(
            request, str(request.app.url_path_for(route_name, **route_params))
        )
        return context


def _caller(params: ProtoMessage, context: ServerCallContext) -> tuple[UserContext, str]:
    # Discovery has no RPC method; version negotiation belongs to protocol calls.
    if context.state.get("method"):
        headers = context.state.get("headers", {})
        requested = str(
            headers.get(constants.VERSION_HEADER)
            or headers.get(constants.VERSION_HEADER.lower())
            or constants.PROTOCOL_VERSION_0_3
        )
        expected = Version(constants.PROTOCOL_VERSION_1_0)
        try:
            actual = Version(requested)
            supported = (actual.major, actual.minor) == (expected.major, expected.minor)
        except InvalidVersion:
            supported = False
        if not supported:
            raise VersionNotSupportedError(
                message=f"A2A version '{requested}' is not supported by this handler. "
                f"Expected version '{constants.PROTOCOL_VERSION_1_0}'."
            )
    validate_proto_required_fields(params)
    if getattr(params, "tenant", ""):
        raise InvalidParamsError(message="Tenant routing is not supported")
    if not isinstance(context.user, _ResolvedUser):
        raise HTTPException(status_code=401, detail="Authentication required")
    return context.user.platform_user, str(context.state["agent_id"])


class AgentA2ARequestHandler(RequestHandler):
    """Delegate protocol operations without a separate TaskStore or executor."""

    def __init__(self, backend: AgentA2ABackend) -> None:
        self._backend = backend

    async def on_message_send(
        self, params: a2a.SendMessageRequest, context: ServerCallContext
    ) -> a2a.Task | a2a.Message:
        user, agent_id = _caller(params, context)
        return await self._backend.send(user, agent_id, params)

    async def on_get_task(self, params: a2a.GetTaskRequest, context: ServerCallContext) -> a2a.Task:
        user, agent_id = _caller(params, context)
        return await self._backend.get(user, agent_id, params)

    async def on_list_tasks(
        self, params: a2a.ListTasksRequest, context: ServerCallContext
    ) -> a2a.ListTasksResponse:
        user, agent_id = _caller(params, context)
        return await self._backend.list(user, agent_id, params)

    async def on_cancel_task(
        self, params: a2a.CancelTaskRequest, context: ServerCallContext
    ) -> a2a.Task:
        user, agent_id = _caller(params, context)
        return await self._backend.cancel(user, agent_id, params)

    async def on_message_send_stream(
        self, params: a2a.SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        user, agent_id = _caller(params, context)
        async with aclosing(self._backend.stream(user, agent_id, params)) as events:
            async for event in events:
                yield event

    async def on_subscribe_to_task(
        self, params: a2a.SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        user, agent_id = _caller(params, context)
        async with aclosing(self._backend.subscribe(user, agent_id, params)) as events:
            async for event in events:
                yield event

    async def on_get_extended_agent_card(
        self, params: a2a.GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> a2a.AgentCard:
        user, agent_id = _caller(params, context)
        return await self._backend.card(user, agent_id, str(context.state["rpc_url"]))

    async def on_create_task_push_notification_config(
        self, params: a2a.TaskPushNotificationConfig, context: ServerCallContext
    ) -> a2a.TaskPushNotificationConfig:
        _caller(params, context)
        raise PushNotificationNotSupportedError

    async def on_get_task_push_notification_config(
        self, params: a2a.GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> a2a.TaskPushNotificationConfig:
        _caller(params, context)
        raise PushNotificationNotSupportedError

    async def on_list_task_push_notification_configs(
        self, params: a2a.ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> a2a.ListTaskPushNotificationConfigsResponse:
        _caller(params, context)
        raise PushNotificationNotSupportedError

    async def on_delete_task_push_notification_config(
        self, params: a2a.DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        _caller(params, context)
        raise PushNotificationNotSupportedError


def _assistant_capability_app(app: ASGIApp) -> ASGIApp:
    async def authenticated(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        try:
            user = await resolve_assistant_a2a_user(
                request.path_params["binding_id"],
                request.path_params["agent_id"],
                request.headers.get("authorization", ""),
            )
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )(scope, receive, send)
            return
        request.state.agent_a2a_user = user
        request.state.agent_a2a_rpc_name = "assistant_a2a_rpc"
        token = set_current_user_context(user)
        try:
            await app(scope, receive, send)
        finally:
            reset_current_user_context(token)

    return authenticated


def create_agent_a2a_routes(backend: AgentA2ABackend) -> list[Route]:
    """Build authenticated Agent card and official A2A 1.0 JSON-RPC/SSE routes."""
    builder = _ContextBuilder()
    handler = AgentA2ARequestHandler(backend)
    routes = create_jsonrpc_routes(handler, A2A_RPC_PATH, context_builder=builder)
    routes[0].name = "agent_a2a_rpc"

    async def agent_card(request: Request) -> Response:
        context = builder.build(request)
        card = await handler.on_get_extended_agent_card(a2a.GetExtendedAgentCardRequest(), context)
        card_route = create_agent_card_routes(
            card, card_url=A2A_CARD_PATH, cache_control="private, no-store"
        )[0]
        return await card_route.endpoint(request)

    routes.append(Route(A2A_CARD_PATH, endpoint=agent_card, methods=["GET"]))
    assistant_rpc = create_jsonrpc_routes(handler, ASSISTANT_A2A_RPC_PATH, context_builder=builder)[
        0
    ]
    assistant_rpc.name = "assistant_a2a_rpc"
    assistant_card = Route(ASSISTANT_A2A_CARD_PATH, endpoint=agent_card, methods=["GET"])
    for route in (assistant_rpc, assistant_card):
        route.app = _assistant_capability_app(route.app)
    routes.extend([assistant_rpc, assistant_card])
    return routes
