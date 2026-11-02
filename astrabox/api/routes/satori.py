"""A fixed-Agent Satori v1 endpoint with the deployment's existing identity seam."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from satori.model import Identify, Opcode

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    API_READ_SCOPE, API_WRITE_SCOPE, UserContext, reset_current_user_context, set_current_user_context,
)
from astrabox.core.service.environment_access import require_environment_access
from astrabox.core.service.orchestrator.satori_service import SatoriService
from astrabox.vendor.satori_connection import WebsocketConnection

router = APIRouter(prefix="/api/satori/agents/{agent_id}/v1")
logger = get_logger(__name__)


async def _identity(connection: Request | WebSocket, *, token: str | None = None, write: bool = False) -> UserContext:
    headers = dict(connection.headers)
    if token is not None:
        headers["authorization"] = "Bearer " + token
    user = await connection.app.state.web_identity_resolver.resolve(headers)
    if user is None:
        raise APIError(code="UNAUTHORIZED", message="An authenticated caller is required", status_code=401)
    scope = API_WRITE_SCOPE if write else API_READ_SCOPE
    if user.api_scopes is not None and scope not in user.api_scopes:
        raise APIError(code="FORBIDDEN", message="Required API scope is missing", status_code=403)
    await require_environment_access(user, operation="Satori " + ("write" if write else "read"))
    return user


def _writes(action: str) -> bool:
    return action in {"channel.create", "message.create", "internal/astrabox.preparation.perform",
                      "internal/astrabox.interaction.answer", "internal/astrabox.response.stop"}


@router.post("/{action:path}", include_in_schema=False)
async def satori_action(agent_id: str, action: str, request: Request) -> JSONResponse:
    try:
        user = await _identity(request, write=_writes(action))
        if request.headers.get("satori-platform") != "astrabox" or request.headers.get("satori-user-id") != agent_id:
            raise APIError(code="INVALID_REQUEST", message="Select this endpoint's astrabox Agent Login", status_code=400)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise APIError(code="INVALID_REQUEST", message="Expected a JSON object", status_code=400) from None
        if not isinstance(body, dict):
            raise APIError(code="INVALID_REQUEST", message="Expected a JSON object", status_code=400)
        key = request.headers.get("idempotency-key")
        if key is not None and (not key.strip() or len(key) > 128):
            raise APIError(code="INVALID_REQUEST", message="Idempotency-Key must contain 1..128 characters", status_code=400)
        binding = set_current_user_context(user)
        try:
            service = SatoriService(user, agent_id)
            if action.startswith("internal/"):
                await service.login()
                result = await service.internal(action.removeprefix("internal/"), body)
            else:
                result = await service.call(action, body, key=key)
        finally:
            reset_current_user_context(binding)
        return JSONResponse(result)
    except APIError as exc:
        return JSONResponse({"code": exc.code, "message": exc.user_message, "data": exc.data}, status_code=exc.status_code)


@router.websocket("/events")
async def satori_events(agent_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    connection = WebsocketConnection(websocket)
    tasks: list[asyncio.Task[Any]] = []
    binding = None
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if not isinstance(raw, dict) or raw.get("op") != Opcode.IDENTIFY or not isinstance(raw.get("body"), dict):
            await websocket.close(code=1008, reason="IDENTIFY required")
            return
        body = raw["body"]
        if set(body) - {"token", "sn"} or ("sn" in body and (type(body["sn"]) is not int or body["sn"] < 0)):
            await websocket.close(code=1008, reason="Invalid IDENTIFY")
            return
        if "token" in body and not isinstance(body["token"], str):
            await websocket.close(code=1008, reason="Invalid token")
            return
        identify = Identify.parse(body)
        user = await _identity(websocket, token=identify.token)
        binding = set_current_user_context(user)
        service = SatoriService(user, agent_id)
        login = await service.login()
        high, rows = await service.repo.poll(user.user_id, agent_id, after=identify.sn)
        after = high if identify.sn is None else identify.sn
        await connection.send({"op": Opcode.READY, "body": {"logins": [login.dump()], "proxy_urls": []}})

        async def send_events() -> None:
            nonlocal rows, after
            while connection.alive:
                for row in rows:
                    await connection.send({"op": Opcode.EVENT, "body": await service.event(row, login)})
                    after = row["sn"]
                if len(rows) < 100:
                    await asyncio.sleep(0.25)
                _high, rows = await service.repo.poll(user.user_id, agent_id, after=after)

        tasks = [asyncio.create_task(connection.heartbeat()), asyncio.create_task(send_events())]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except APIError as exc:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1008, reason=exc.user_message[:120])
    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except Exception:
        logger.exception("Satori connection failed")
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1011, reason="Satori event stream failed")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.connection_closed()
        if binding is not None:
            reset_current_user_context(binding)
