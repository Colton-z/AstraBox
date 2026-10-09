"""Capability-scoped database custody of opaque native runtime snapshots."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable
from typing import Any, Literal, Self

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.api.routes.transcript import CAPABILITY_PATH_PREFIX
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.core.service.orchestrator.transcript_capability import (
    verify_runtime_state_capability_token,
)
from astrabox.persistence.repository.runtime_state_snapshot_repository import (
    NATIVE_SESSION_STORE,
    PROFILE_STATE,
    RuntimeStateOwner,
    RuntimeStateSnapshotConflict,
    RuntimeStateSnapshotRepository,
)

_registered_on: int | None = None


class RuntimeStateOwnerPayload(BaseModel):
    """The platform-issued owner of a native runtime profile."""

    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    subject_kind: Literal["assistant", "agent", "session"]
    subject_id: str = Field(min_length=1)
    engine_kind: str = Field(min_length=1)

    def owner(self) -> RuntimeStateOwner:
        return RuntimeStateOwner(**self.model_dump())

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        self.owner()
        return self


class RuntimeStateLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: RuntimeStateOwnerPayload


class RuntimeStateSaveRequest(RuntimeStateLoadRequest):
    payload_b64: str
    sha256: str = Field(min_length=64, max_length=64)
    expected_snapshot_id: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeStateSaveResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    snapshot_id: str


class RuntimeStateLoadResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    snapshot_id: str | None
    payload_b64: str | None
    sha256: str | None
    size: int


def register_runtime_state_routes(app: Any) -> None:
    """Mount native-state access under the existing private capability prefix."""
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)
    prefix = CAPABILITY_PATH_PREFIX + "/{cap_token}/api/v1/runtime-state"

    def authorize(owner: RuntimeStateOwner, token: str) -> JSONResponse | None:
        if verify_runtime_state_capability_token(owner.canonical_key(), token):
            return None
        return JSONResponse(
            status_code=403,
            content=error_response("FORBIDDEN", "invalid runtime state capability"),
        )

    # The native SessionStore at the root, the profile state beside it. One
    # capability covers both: they are the same owner's runtime state.
    for path, name, repo in (
        ("", "runtime_state", RuntimeStateSnapshotRepository(NATIVE_SESSION_STORE)),
        ("/profile", "runtime_profile", RuntimeStateSnapshotRepository(PROFILE_STATE)),
    ):
        _mount_archive(app, prefix + path, name, repo, authorize)


def _mount_archive(
    app: Any,
    prefix: str,
    name: str,
    repo: RuntimeStateSnapshotRepository,
    authorize: Callable[[RuntimeStateOwner, str], JSONResponse | None],
) -> None:
    """Register load and save for one kind of owner archive under ``prefix``."""

    async def runtime_state_load(cap_token: str, payload: RuntimeStateLoadRequest):
        owner = payload.owner.owner()
        rejected = authorize(owner, cap_token)
        if rejected is not None:
            return rejected
        snapshot = await repo.load(owner)
        return success_response({
            "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
            "payload_b64": (
                base64.b64encode(snapshot.payload).decode("ascii")
                if snapshot is not None else None
            ),
            "sha256": snapshot.sha256 if snapshot is not None else None,
            "size": snapshot.size if snapshot is not None else 0,
        })

    async def runtime_state_save(cap_token: str, payload: RuntimeStateSaveRequest):
        owner = payload.owner.owner()
        rejected = authorize(owner, cap_token)
        if rejected is not None:
            return rejected
        try:
            raw = base64.b64decode(payload.payload_b64, validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse(
                status_code=400,
                content=error_response("INVALID_REQUEST", "invalid snapshot encoding"),
            )
        if hashlib.sha256(raw).hexdigest() != payload.sha256:
            return JSONResponse(
                status_code=400,
                content=error_response("INVALID_REQUEST", "snapshot digest mismatch"),
            )
        try:
            snapshot_id = await repo.save(
                owner, raw, expected_snapshot_id=payload.expected_snapshot_id
            )
        except RuntimeStateSnapshotConflict:
            return JSONResponse(
                status_code=409,
                content=error_response(
                    "RUNTIME_STATE_CONFLICT", "snapshot predecessor is not current"
                ),
            )
        return success_response({"snapshot_id": snapshot_id})

    app.post(
        prefix + "/load",
        response_model=ApiEnvelope[RuntimeStateLoadResult],
        response_model_exclude_unset=True,
        name=f"{name}_load",
    )(runtime_state_load)
    app.post(
        prefix + "/save",
        response_model=ApiEnvelope[RuntimeStateSaveResult],
        response_model_exclude_unset=True,
        name=f"{name}_save",
    )(runtime_state_save)
