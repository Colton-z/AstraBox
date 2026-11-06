"""A fixed-Agent Satori v1 endpoint with the deployment's existing identity seam."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from satori.model import Identify, Opcode

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    API_READ_SCOPE, API_WRITE_SCOPE, UserContext, reset_current_user_context, set_current_user_context,
)
from astrabox.core.service.environment_access import require_environment_access
from astrabox.api.native_documentation import satori_operation
from astrabox.api.routes.session_files import FileDownloadResponse
from astrabox.core.service.orchestrator import satori_feed
from astrabox.core.service.orchestrator.satori_service import SatoriService
from astrabox.core.service.orchestrator.session_file_service import FileDownload
from astrabox.core.service.orchestrator.session_runtime.attachments import MAX_ATTACHMENTS
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
    return action in {"channel.create", "message.create", "upload.create", "internal/astrabox.preparation.perform",
                      "internal/astrabox.interaction.answer", "internal/astrabox.response.stop"}


@router.post("/{action:path}", include_in_schema=False, openapi_extra=satori_operation(proxy=False))
async def satori_action(agent_id: str, action: str, request: Request) -> JSONResponse:
    try:
        user = await _identity(request, write=_writes(action))
        if request.headers.get("satori-platform") != "astrabox" or request.headers.get("satori-user-id") != agent_id:
            raise APIError(code="INVALID_REQUEST", message="Select this endpoint's astrabox Agent Login", status_code=400)
        if action == "upload.create":
            return JSONResponse(await _upload(request, user, agent_id))
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


async def _upload(request: Request, user: UserContext, agent_id: str) -> dict[str, str]:
    """Stage the multipart ``upload.create`` body's files; each part is one file."""
    try:
        form = await request.form(max_files=MAX_ATTACHMENTS, max_fields=MAX_ATTACHMENTS)
    except Exception:
        raise APIError(code="INVALID_REQUEST", message="upload.create expects multipart/form-data", status_code=400) from None
    binding = set_current_user_context(user)
    try:
        return await SatoriService(user, agent_id).upload(list(form.multi_items()))
    finally:
        reset_current_user_context(binding)
        await form.close()


@router.get("/proxy/{url:path}", include_in_schema=False, openapi_extra=satori_operation(proxy=True))
async def satori_proxy(agent_id: str, url: str, request: Request) -> Response:
    """Serve one of this Login's ``internal:`` resources to a caller allowed to read it."""
    try:
        user = await _identity(request)
        # The link's own percent-encoding is kept: it is decoded once, where its path is read.
        raw = request.scope.get("raw_path", b"").decode("latin-1")
        target = raw[raw.index("/proxy/") + len("/proxy/"):] if "/proxy/" in raw else url
        if target[:11].lower() == "internal%3a":
            target = "internal:" + target[11:]
        binding = set_current_user_context(user)
        try:
            body, media_type = await SatoriService(user, agent_id).proxy(target)
        finally:
            reset_current_user_context(binding)
        if isinstance(body, FileDownload):
            response = FileDownloadResponse(body, request)
            response.headers["content-type"] = media_type
            return response
        return Response(content=body, media_type=media_type)
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
        async with satori_feed.watch(service.repo, user.user_id, agent_id) as feed:
            known = await service.repo.capture(user.user_id, agent_id)
            if identify.sn is not None and identify.sn > known:
                raise APIError(code="INVALID_REQUEST", message="Satori sn is outside this identity's feed",
                               status_code=400)
            after = known if identify.sn is None else identify.sn
            await connection.send({"op": Opcode.READY, "body": {"logins": [login.dump()], "proxy_urls": []}})

            async def send_events() -> None:
                nonlocal after, known
                while connection.alive:
                    rows = await service.repo.events(user.user_id, agent_id, after=after)
                    for row in rows:
                        await connection.send({"op": Opcode.EVENT, "body": await service.event(row, login)})
                        after = row["sn"]
                    if len(rows) < 100:
                        # Every event through `known` was committed before that
                        # read, so none past `after` remains for this identity.
                        after = max(after, known)
                        known = await feed.beyond(after)

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
