from __future__ import annotations

from typing import Any, Literal

from fastapi import Body, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.http_headers import build_attachment_headers
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import get_platform_service

_registered_on: int | None = None


class ListSessionFilesRequest(BaseModel):
    """Body for ``POST /sessions/{id}/files/list`` — optional path filter."""

    path: str | None = None


class MkdirSessionFilesRequest(BaseModel):
    """Body for ``POST /sessions/{id}/files/mkdir``."""

    path: str


class MoveSessionFilesRequest(BaseModel):
    """Body for ``POST /sessions/{id}/files/move``."""

    src_path: str
    dest_path: str


class DeleteSessionFilesRequest(BaseModel):
    """Body for ``POST /sessions/{id}/files/delete``."""

    paths: list[str]


# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring one — ``response_model_exclude_unset`` and ``extra="allow"`` — are
# in :mod:`astrabox.api.routes.response_envelope`. Every field below is one
# ``SessionFileService`` writes on the answering path, so a declared type is an
# assertion about that write path and not a hope about the sandbox.


class SessionFileEntry(BaseModel):
    """One directory member.

    ``size`` and ``modified_at`` come from the sandbox's directory listing, so
    only a listing carries them; the entries an upload echoes back name what was
    written and nothing else. Both stay optional here so the upload answer keeps
    the same entry shape without inventing a size it never measured.
    """

    model_config = ConfigDict(extra="allow")

    path: str
    name: str
    kind: Literal["file", "directory"]
    size: int | None = None
    modified_at: str | None = None


class SessionFileListing(BaseModel):
    """A directory read: where the panel is, and what is in it.

    ``parent_path`` is null at the session root — the panel's "up" control is
    that value's presence, not a comparison it re-derives.
    """

    model_config = ConfigDict(extra="allow")

    root_path: str
    current_path: str
    parent_path: str | None
    entries: list[SessionFileEntry]
    session_kind: str


class SessionFileUploadResult(BaseModel):
    """What an upload wrote, in the directory it wrote to."""

    model_config = ConfigDict(extra="allow")

    root_path: str
    current_path: str
    parent_path: str | None
    entries: list[SessionFileEntry]
    uploaded_count: int


class SessionDirectoryCreated(BaseModel):
    """The created directory, addressed both as a location and as a target."""

    model_config = ConfigDict(extra="allow")

    root_path: str
    current_path: str
    parent_path: str | None
    path: str


class SessionFileMoved(BaseModel):
    """Source and destination, resolved to absolute paths inside the root."""

    model_config = ConfigDict(extra="allow")

    root_path: str
    src_path: str
    dest_path: str


class SessionFilesDeleted(BaseModel):
    """The requested paths, and how many of them existed to be removed.

    ``deleted_count`` is below ``len(paths)`` when a path was already absent:
    deletion converges, so a retry after a lost response still answers 200.
    """

    model_config = ConfigDict(extra="allow")

    root_path: str
    paths: list[str]
    deleted_count: int


def register_session_file_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_platform_service()

    @app.post(
        "/api/v1/sessions/{session_id}/files/list",
        response_model=ApiEnvelope[SessionFileListing],
        response_model_exclude_unset=True,
    )
    async def list_session_files(
        session_id: str,
        request: Request,
        body: ListSessionFilesRequest | None = Body(None),
    ):
        user = await get_current_user_context(request)
        result = await _service.list_session_files(
            user,
            session_id,
            path=body.path if body else None,
        )
        return success_response(result)

    @app.post(
        "/api/v1/sessions/{session_id}/files/upload",
        response_model=ApiEnvelope[SessionFileUploadResult],
        response_model_exclude_unset=True,
    )
    async def upload_session_files(
        session_id: str,
        request: Request,
        path: str = Form(""),
        files: list[UploadFile] = File(...),
    ):
        user = await get_current_user_context(request)
        result = await _service.upload_session_files(
            user,
            session_id,
            path=path,
            files=files,
        )
        return success_response(result)

    @app.post(
        "/api/v1/sessions/{session_id}/files/mkdir",
        response_model=ApiEnvelope[SessionDirectoryCreated],
        response_model_exclude_unset=True,
    )
    async def mkdir_session_files(
        session_id: str,
        request: Request,
        body: MkdirSessionFilesRequest,
    ):
        user = await get_current_user_context(request)
        result = await _service.create_session_directory(
            user,
            session_id,
            path=body.path,
        )
        return success_response(result)

    @app.post(
        "/api/v1/sessions/{session_id}/files/move",
        response_model=ApiEnvelope[SessionFileMoved],
        response_model_exclude_unset=True,
    )
    async def move_session_files(
        session_id: str,
        request: Request,
        body: MoveSessionFilesRequest,
    ):
        user = await get_current_user_context(request)
        result = await _service.move_session_file(
            user,
            session_id,
            src_path=body.src_path,
            dest_path=body.dest_path,
        )
        return success_response(result)

    @app.post(
        "/api/v1/sessions/{session_id}/files/delete",
        response_model=ApiEnvelope[SessionFilesDeleted],
        response_model_exclude_unset=True,
    )
    async def delete_session_files(
        session_id: str,
        request: Request,
        body: DeleteSessionFilesRequest,
    ):
        user = await get_current_user_context(request)
        result = await _service.delete_session_files(
            user,
            session_id,
            paths=body.paths,
        )
        return success_response(result)

    # No response model: the answer is the file's bytes under an attachment
    # header, not the JSON envelope, so there is no schema for a client to
    # generate from.
    @app.get("/api/v1/sessions/{session_id}/files/download")
    async def download_session_file(
        session_id: str,
        request: Request,
        path: str,
    ):
        user = await get_current_user_context(request)
        content, filename = await _service.download_session_file(
            user,
            session_id,
            path=path,
        )
        headers = build_attachment_headers(filename)
        headers["Content-Length"] = str(len(content))
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers=headers,
        )
