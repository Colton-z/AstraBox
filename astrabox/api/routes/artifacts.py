"""Owner management and one viewer runtime mounted for login and share links."""

from __future__ import annotations

from contextlib import aclosing
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from astrabox.api.documentation import json_response, object_schema
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_asserted_user_context, get_current_user_context
from astrabox.core.service.artifacts import limits
from astrabox.core.service.artifacts.service import ArtifactViewer, public, sse
from astrabox.core.service.orchestrator.service_registry import get_platform_service


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactMetadataPatch(InputModel):
    title: str | None = None
    description: str | None = None


class ArtifactShareRequest(InputModel):
    access: Literal["view", "interact"] = "view"
    audience: Literal["anyone", "signed_in"] = "anyone"
    version: int | None = Field(default=None, ge=1)
    expires_in_seconds: int | None = Field(default=None, gt=0)


class ArtifactShare(BaseModel):
    enabled: bool
    token: str
    access: Literal["view", "interact"]
    audience: Literal["anyone", "signed_in"]
    version: int | None
    created_at: str | None
    expires_at: str | None


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    artifact_id: str
    title: str
    description: str
    kind: Literal["html", "react", "svg", "markdown", "mermaid", "code"]
    language: str | None = None
    current_version: int
    manifest: dict[str, Any]
    origin: dict[str, Any]
    share: ArtifactShare
    created_at: str
    updated_at: str


class ArtifactVersionRecord(BaseModel):
    """One stored version; content is returned only by a detail read."""

    model_config = ConfigDict(extra="allow")
    artifact_id: str
    version: int
    sha256: str
    byte_size: int
    kind: str
    title: str
    language: str | None
    manifest: dict[str, Any]
    author_user_id: str | None
    author_kind: str
    created_at: str
    content: str | None = None


class ArtifactDetail(ArtifactRecord):
    version: ArtifactVersionRecord
    content: str
    astrabox_artifact: dict[str, Any]
    url: str


class ArtifactPage(BaseModel):
    items: list[ArtifactRecord]
    has_more: bool
    next_cursor: str | None


class ArtifactShareView(BaseModel):
    version: int | None
    access: Literal["view", "interact"]
    audience: Literal["anyone", "signed_in"]


class ArtifactRuntimeRecord(BaseModel):
    artifact_id: str
    title: str
    description: str
    kind: Literal["html", "react", "svg", "markdown", "mermaid", "code"]
    language: str | None = None
    current_version: int
    manifest: dict[str, Any]
    origin: dict[str, Any]
    share: ArtifactShareView | None = None
    created_at: str
    updated_at: str


class ArtifactRuntime(BaseModel):
    artifact: ArtifactRuntimeRecord
    version: int
    content: str
    manifest: dict[str, Any]
    viewer: dict[str, Any]
    pinned: bool
    after_seq: int


class ArtifactPublishRequest(InputModel):
    content: str
    base_version: int = Field(ge=1)


class ArtifactDataRequest(InputModel):
    data: dict[str, Any]


class ArtifactWhere(InputModel):
    field: str = Field(min_length=1)
    op: Literal["==", "!=", "<", "<=", ">", ">=", "in", "array-contains"]
    value: Any


class ArtifactOrder(InputModel):
    field: str = Field(min_length=1)
    direction: Literal["asc", "desc"] = "asc"


class ArtifactQueryRequest(InputModel):
    collection: str
    where: list[ArtifactWhere] = Field(default_factory=list)
    order_by: list[ArtifactOrder] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=limits.DOCUMENT_COUNT)


class ArtifactAddRequest(ArtifactDataRequest):
    collection: str


class ArtifactSampleMessage(InputModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ArtifactSampleTool(InputModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ArtifactSampleRequest(InputModel):
    messages: list[ArtifactSampleMessage] | None = None
    input: list[ArtifactSampleMessage] | None = None
    max_tokens: int = Field(default=limits.SAMPLE_DEFAULT_MAX_TOKENS, ge=1)
    tools: list[ArtifactSampleTool] = Field(default_factory=list)
    modelTier: str | None = None


class ArtifactRoomRequest(InputModel):
    topic: str
    data: Any


class ArtifactPresenceRequest(InputModel):
    state: Any


class ArtifactAgentRunRequest(InputModel):
    prompt: str = Field(min_length=1)


class ArtifactAgentRun(BaseModel):
    run_id: str
    session_id: str
    url: str


class ArtifactAgentStatus(BaseModel):
    state: Literal["running", "waiting_input", "completed", "failed"]
    session_id: str
    error: str | None = None


_RUNTIME_CODES = {
    "ARTIFACT_CAPABILITY_NOT_DECLARED": "not_granted",
    "ARTIFACT_NOT_WRITER": "not_writer",
    "ARTIFACT_VERSION_CONFLICT": "conflict",
    "ARTIFACT_DATA_DENIED": "not_granted",
    "ARTIFACT_ROOM_DENIED": "not_granted",
    "ARTIFACT_SAMPLE_DENIED": "not_granted",
    "ARTIFACT_AGENT_DENIED": "not_granted",
    "ARTIFACT_SIGN_IN_REQUIRED": "not_granted",
    "ARTIFACT_NOT_FOUND": "not_found",
    "ARTIFACT_SHARE_NOT_FOUND": "not_found",
    "ARTIFACT_VERSION_NOT_FOUND": "not_found",
    "ARTIFACT_DATA_NOT_FOUND": "not_found",
    "ARTIFACT_AGENT_RUN_NOT_FOUND": "not_found",
    "ARTIFACT_SAMPLE_RATE_LIMITED": "rate_limited",
    "ARTIFACT_LIVE_RATE_LIMITED": "rate_limited",
    "ARTIFACT_SAMPLE_TOO_LARGE": "too_large",
    "ARTIFACT_SAMPLE_TOKEN_LIMIT": "invalid_argument",
    "ARTIFACT_AGENT_RUN_LIMITED": "rate_limited",
    "ARTIFACT_SAMPLE_UNAVAILABLE": "unavailable",
    "ARTIFACT_AGENT_UNAVAILABLE": "unavailable",
    "ARTIFACT_STORE_UNAVAILABLE": "unavailable",
    "ARTIFACT_CONTENT_TOO_LARGE": "too_large",
    "ARTIFACT_DATA_LIMIT": "too_large",
    "ARTIFACT_DATA_COUNT_LIMIT": "too_large",
    "ARTIFACT_ROOM_LIMIT": "too_large",
    "ARTIFACT_TITLE_LIMIT": "too_large",
    "ARTIFACT_VERSION_LIMIT": "too_large",
}


def runtime_error(exc: APIError) -> JSONResponse:
    return JSONResponse(
        error_response(
            APIError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                data={"runtime_code": _RUNTIME_CODES.get(exc.code, "invalid_argument")},
            )
        ),
        status_code=exc.status_code,
    )


async def owner_viewer(
    request: Request, artifact_id: str, guest_id: str | None = None
) -> ArtifactViewer:
    user = await get_current_user_context(request)
    try:
        return await get_platform_service().artifacts.resolve_viewer(
            user=user, artifact_id=artifact_id
        )
    except APIError as exc:
        exc.data = {
            **(exc.data or {}),
            "runtime_code": _RUNTIME_CODES.get(exc.code, "invalid_argument"),
        }
        raise


async def shared_viewer(
    request: Request, token: str, guest_id: str | None = None
) -> ArtifactViewer:
    # The identity middleware resolves supplied credentials even on exempt routes.
    # An unstamped request must stay anonymous, never become the local default user.
    user = get_asserted_user_context()
    try:
        return await get_platform_service().artifacts.resolve_viewer(
            user=user, token=token, guest_id=guest_id
        )
    except APIError as exc:
        exc.data = {
            **(exc.data or {}),
            "runtime_code": _RUNTIME_CODES.get(exc.code, "invalid_argument"),
        }
        raise


def register_artifact_routes(app: FastAPI) -> None:
    if getattr(app.state, "artifact_routes_registered", False):
        return
    app.state.artifact_routes_registered = True

    @app.get(
        "/api/v1/artifacts",
        response_model=ApiEnvelope[ArtifactPage],
        response_model_exclude_unset=True,
    )
    async def list_artifacts(
        request: Request,
        cursor: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
        assistant_id: str | None = None,
        q: str | None = None,
    ):
        return success_response(
            await get_platform_service().artifacts.list(
                await get_current_user_context(request),
                cursor=cursor,
                limit=limit,
                session_id=session_id,
                assistant_id=assistant_id,
                q=q,
            )
        )

    @app.get(
        "/api/v1/artifacts/{artifact_id}",
        response_model=ApiEnvelope[dict[str, Any]],
        response_model_exclude_unset=True,
        responses={200: {"model": ApiEnvelope[ArtifactDetail]}},
    )
    async def get_artifact(artifact_id: str, request: Request):
        return success_response(
            await get_platform_service().artifacts.get(
                await get_current_user_context(request), artifact_id
            )
        )

    @app.patch(
        "/api/v1/artifacts/{artifact_id}",
        response_model=ApiEnvelope[ArtifactRecord],
        response_model_exclude_unset=True,
    )
    async def patch_artifact(artifact_id: str, body: ArtifactMetadataPatch, request: Request):
        return success_response(
            await get_platform_service().artifacts.patch(
                await get_current_user_context(request),
                artifact_id,
                body.model_dump(exclude_unset=True),
            )
        )

    @app.delete(
        "/api/v1/artifacts/{artifact_id}",
        response_model=ApiEnvelope[dict[str, bool]],
        response_model_exclude_unset=True,
    )
    async def delete_artifact(artifact_id: str, request: Request):
        return success_response(
            await get_platform_service().artifacts.delete(
                await get_current_user_context(request), artifact_id
            )
        )

    @app.get(
        "/api/v1/artifacts/{artifact_id}/versions",
        response_model=ApiEnvelope[list[dict[str, Any]]],
        response_model_exclude_unset=True,
        responses={200: {"model": ApiEnvelope[list[ArtifactVersionRecord]]}},
    )
    async def list_artifact_versions(artifact_id: str, request: Request):
        return success_response(
            await get_platform_service().artifacts.list_versions(
                await get_current_user_context(request), artifact_id
            )
        )

    @app.get(
        "/api/v1/artifacts/{artifact_id}/versions/{version}",
        response_model=ApiEnvelope[dict[str, Any]],
        response_model_exclude_unset=True,
        responses={200: {"model": ApiEnvelope[ArtifactDetail]}},
    )
    async def get_artifact_version(artifact_id: str, version: int, request: Request):
        service = get_platform_service().artifacts
        await service.owned(await get_current_user_context(request), artifact_id)
        return success_response(
            public(await service.version(artifact_id, version, content=True))
        )

    @app.post(
        "/api/v1/artifacts/{artifact_id}/versions/{version}/restore",
        response_model=ApiEnvelope[ArtifactRecord],
        response_model_exclude_unset=True,
    )
    async def restore_artifact_version(artifact_id: str, version: int, request: Request):
        return success_response(
            await get_platform_service().artifacts.restore(
                await get_current_user_context(request), artifact_id, version
            )
        )

    @app.get(
        "/api/v1/artifacts/{artifact_id}/share",
        response_model=ApiEnvelope[ArtifactShare],
        response_model_exclude_unset=True,
    )
    async def get_artifact_share(artifact_id: str, request: Request):
        return success_response(
            await get_platform_service().artifacts.get_share(
                await get_current_user_context(request), artifact_id
            )
        )

    @app.post(
        "/api/v1/artifacts/{artifact_id}/share",
        response_model=ApiEnvelope[ArtifactShare],
        response_model_exclude_unset=True,
    )
    async def enable_artifact_share(artifact_id: str, body: ArtifactShareRequest, request: Request):
        return success_response(
            await get_platform_service().artifacts.enable_share(
                await get_current_user_context(request), artifact_id, **body.model_dump()
            )
        )

    @app.delete(
        "/api/v1/artifacts/{artifact_id}/share",
        response_model=ApiEnvelope[ArtifactShare],
        response_model_exclude_unset=True,
    )
    async def disable_artifact_share(artifact_id: str, request: Request):
        return success_response(
            await get_platform_service().artifacts.disable_share(
                await get_current_user_context(request), artifact_id
            )
        )

    for prefix, dependency in (
        ("/api/v1/artifacts/{artifact_id}", owner_viewer),
        ("/api/v1/artifact-share/{token}", shared_viewer),
    ):
        router = APIRouter(prefix=prefix)

        @router.get(
            "/runtime",
            response_model=ApiEnvelope[ArtifactRuntime],
            response_model_exclude_unset=True,
        )
        async def artifact_runtime(viewer: ArtifactViewer = Depends(dependency)):
            try:
                return success_response(await get_platform_service().artifacts.runtime(viewer))
            except APIError as exc:
                return runtime_error(exc)

        @router.get(
            "/runtime/live",
            response_class=StreamingResponse,
            responses={
                **{200: {"content": {"text/event-stream": {}}}},
                **{
                    200: {
                        "description": "Server-sent events. See the Artifact runtime guide for event payloads and lifecycle.",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                    }
                },
            },
        )
        async def artifact_live(
            after_seq: int = Query(default=0, ge=0), viewer: ArtifactViewer = Depends(dependency)
        ):
            service = get_platform_service().artifacts

            async def tail():
                try:
                    async with aclosing(service.live(viewer, after_seq)) as stream:
                        async for event in stream:
                            yield event
                except APIError as exc:
                    yield sse(
                        "error",
                        {
                            "code": _RUNTIME_CODES.get(exc.code, "unavailable"),
                            "message": exc.message,
                        },
                    )

            return StreamingResponse(
                tail(),
                media_type="text/event-stream",
                headers={"cache-control": "no-store", "x-accel-buffering": "no"},
            )

        @router.post(
            "/runtime/publish",
            response_model=ApiEnvelope[ArtifactRecord],
            response_model_exclude_unset=True,
        )
        async def artifact_publish(
            body: ArtifactPublishRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.publish(
                        viewer, body.content, body.base_version
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.get(
            "/runtime/data/{path:path}",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "path": {"type": "string"},
                        "exists": {"type": "boolean"},
                        "data": {"description": "Stored JSON document, or null when absent."},
                    }
                )
            ),
        )
        async def artifact_data_get(path: str, viewer: ArtifactViewer = Depends(dependency)):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_get(viewer, path)
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.put(
            "/runtime/data/{path:path}",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "writes": {
                            "type": "array",
                            "items": object_schema({"path": {"type": "string"}, "op": {"type": "string"}}),
                        }
                    }
                )
            ),
        )
        async def artifact_data_set(
            path: str, body: ArtifactDataRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_write(
                        viewer, [{"action": "set", "path": path, "data": body.data}]
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.patch(
            "/runtime/data/{path:path}",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "writes": {
                            "type": "array",
                            "items": object_schema({"path": {"type": "string"}, "op": {"type": "string"}}),
                        }
                    }
                )
            ),
        )
        async def artifact_data_update(
            path: str, body: ArtifactDataRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_write(
                        viewer, [{"action": "update", "path": path, "data": body.data}]
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.delete(
            "/runtime/data/{path:path}",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "writes": {
                            "type": "array",
                            "items": object_schema({"path": {"type": "string"}, "op": {"type": "string"}}),
                        }
                    }
                )
            ),
        )
        async def artifact_data_delete(path: str, viewer: ArtifactViewer = Depends(dependency)):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_write(
                        viewer, [{"action": "delete", "path": path}]
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/data-query",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "items": {
                            "type": "array",
                            "items": object_schema(
                                {
                                    "id": {"type": "string"},
                                    "path": {"type": "string"},
                                    "data": {"type": "object", "additionalProperties": True},
                                }
                            ),
                        }
                    }
                )
            ),
        )
        async def artifact_data_query(
            body: ArtifactQueryRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_query(viewer, **body.model_dump())
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/data-add",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(object_schema({"id": {"type": "string"}, "path": {"type": "string"}})),
        )
        async def artifact_data_add(
            body: ArtifactAddRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.data_add(
                        viewer, body.collection, body.data
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/sample",
            response_class=StreamingResponse,
            responses={
                **{200: {"content": {"text/event-stream": {}}}},
                **{
                    200: {
                        "description": "Server-sent events. See the Artifact runtime guide for event payloads and lifecycle.",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                    }
                },
            },
        )
        async def artifact_sample(
            body: ArtifactSampleRequest, request: Request, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                if len(await request.body()) > limits.SAMPLE_REQUEST_BYTES:
                    raise APIError(code="ARTIFACT_SAMPLE_TOO_LARGE", message="sample request exceeds 256 KiB", status_code=413)
                stream = await get_platform_service().artifact_samples.prepare(
                    viewer, body.model_dump(exclude_unset=True)
                )
                return StreamingResponse(
                    stream,
                    background=BackgroundTask(stream.aclose),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-store", "x-accel-buffering": "no"},
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/room/{room}/emit",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(object_schema({"seq": {"type": "integer"}})),
        )
        async def artifact_room_emit(
            room: str, body: ArtifactRoomRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.room_write(
                        viewer, room, topic=body.topic, data=body.data
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/room/{room}/presence",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(object_schema({"seq": {"type": "integer"}})),
        )
        async def artifact_room_presence(
            room: str, body: ArtifactPresenceRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifacts.room_write(
                        viewer, room, data=body.state, presence=True
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.get(
            "/runtime/viewers",
            response_model=ApiEnvelope[dict[str, Any]],
            response_model_exclude_unset=True,
            responses=json_response(
                object_schema(
                    {
                        "profiles": {
                            "type": "array",
                            "items": object_schema(
                                {"id": {"type": "string"}, "display_name": {"type": "string"}}
                            ),
                        }
                    }
                )
            ),
        )
        async def artifact_viewers(ids: str = "", viewer: ArtifactViewer = Depends(dependency)):
            try:
                return success_response(
                    await get_platform_service().artifacts.profiles(
                        viewer, [i for i in ids.split(",") if i]
                    )
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.post(
            "/runtime/agent-runs",
            response_model=ApiEnvelope[ArtifactAgentRun],
            response_model_exclude_unset=True,
        )
        async def artifact_agent_run(
            body: ArtifactAgentRunRequest, viewer: ArtifactViewer = Depends(dependency)
        ):
            try:
                return success_response(
                    await get_platform_service().artifact_agent_runs.start(viewer, body.prompt)
                )
            except APIError as exc:
                return runtime_error(exc)

        @router.get(
            "/runtime/agent-runs/{run_id}",
            response_model=ApiEnvelope[ArtifactAgentStatus],
            response_model_exclude_unset=True,
        )
        async def artifact_agent_status(run_id: str, viewer: ArtifactViewer = Depends(dependency)):
            try:
                return success_response(
                    await get_platform_service().artifact_agent_runs.status(viewer, run_id)
                )
            except APIError as exc:
                return runtime_error(exc)

        app.include_router(router)
