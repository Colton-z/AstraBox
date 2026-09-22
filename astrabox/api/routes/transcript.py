"""Backend HTTP interface for the externalized transcript truth (SessionStore mirror).

The per-session sandbox runs Claude with a sandbox-side ``SpoolSessionStore``.
It fsyncs SDK transcript batches locally, then uses these endpoints to append,
load, and enumerate child transcript keys in ``TranscriptEntryRepository`` on
the configured persistence backend. Recovery reads the same repository
directly, without the sandbox.

**Append is idempotent per batch.** The body carries an ``append_id`` naming the
batch and a ``payload_sha256`` over ``{key, entries}``; both are required, and an
append without them is refused. Re-sending a batch — which the spooled sender
does whenever a response is lost — returns the same ``store_sequence`` and
stores nothing new. ``store_sequence`` is the scope's committed entry count, and
``load`` returns it too, so a caller can tell how far the durable transcript has
advanced without counting what it reads.

**Authorization — per-session capability token.** These are a sandbox→backend
channel (no user cookie), so they authenticate the way the sandbox-callback
channel does: a per-session capability token carried in the URL. For Claude,
the platform sends the scoped base URL over the runner's typed
``configure``/``activate`` protocol after a Session claims the box. Engines
whose local logs need the platform mirror receive the same value through that
mirror's target. Each request is bound to the single Session whose token it
carries; a token cannot address another Session's transcript.

The unscoped ``/api/v1/transcript/*`` routes are **fail-closed** by default
(401). A single-tenant, network-isolated deployment that cannot use the token
scheme may set ``ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED=false`` to enable
them explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from fastapi import Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.fault_injection import consume_fault
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.core.service.orchestrator.transcript_capability import (
    verify_transcript_capability_token,
)
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)

logger = get_logger(__name__)

_registered_on: int | None = None

#: URL segment carrying the per-session capability token. The sandbox base URL
#: becomes ``{base}/api/v1/sbxcap/{token}`` and the sandbox-side adapter appends
#: ``/api/v1/transcript/{sid}/{op}`` after it, needing no change of its own.
CAPABILITY_PATH_PREFIX = "/api/v1/sbxcap"


# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring one are in :mod:`astrabox.api.routes.response_envelope`. The caller
# here is the in-box transcript sender rather than the console, so these models
# are what the SessionStore adapter reads back off the wire.


class TranscriptAppendResult(BaseModel):
    """What the store holds after a batch.

    ``count`` is the batch's size as sent; ``store_sequence`` is the scope's
    committed entry count, so a re-sent batch answers the sequence the first
    delivery got and nothing is stored twice.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    count: int
    store_sequence: int


class TranscriptLoadResult(BaseModel):
    """A scope's entries, or ``None`` for a key nothing has written.

    The null is the SessionStore contract for "unknown key" and is distinct from
    an empty list, so the adapter can tell a never-written transcript from one
    whose entries were all removed.
    """

    model_config = ConfigDict(extra="allow")

    entries: list[dict[str, Any]] | None
    store_sequence: int


class TranscriptSessionRef(BaseModel):
    """One SDK session under a project key, with its newest write time."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    mtime: int


class TranscriptSessionList(BaseModel):
    """Main transcripts only — subagent subpaths are enumerated by subkey."""

    model_config = ConfigDict(extra="allow")

    sessions: list[TranscriptSessionRef]


class TranscriptSubkeyList(BaseModel):
    """The subpaths under one SDK session — the subagent transcripts to resume."""

    model_config = ConfigDict(extra="allow")

    subkeys: list[str]


def transcript_capability_required() -> bool:
    """Default True; ``false`` enables the unscoped single-tenant routes."""
    return str(
        os.getenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "true") or "true"
    ).strip().lower() not in ("0", "false", "no", "off")


def transcript_payload_digest(
    key: dict[str, Any], entries: list[dict[str, Any]]
) -> str:
    """The digest an append declares over what it is sending.

    Canonical (``sort_keys``) JSON, so the value depends on the key/entry
    content and not on the order a serializer happened to emit fields in. Shared
    with the in-box sender so both sides compute the same string from the same
    batch.
    """
    return hashlib.sha256(
        json.dumps(
            {"key": key, "entries": entries},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _verify_payload_digest(
    key: dict[str, Any], entries: list[dict[str, Any]], declared: Any
) -> None:
    """Refuse a batch whose body does not match the digest it declares.

    An ``append_id`` is a promise that this batch is the one already stored
    under that id. The digest is what makes a broken promise a rejection here
    rather than a silent divergence between what a caller believes it stored and
    what the store holds.
    """
    if not isinstance(declared, str) or len(declared) != 64:
        raise ValueError("payload_sha256 must be a 64-character hex digest")
    if transcript_payload_digest(key, entries) != declared:
        raise ValueError("payload_sha256 does not match the request body")


def _scope_from_key(key: dict[str, Any]) -> tuple[str, str, str | None]:
    project_key = str(key.get("project_key") or "").strip()
    session_id = str(key.get("session_id") or "").strip()
    subpath_raw = key.get("subpath")
    subpath = str(subpath_raw).strip() if isinstance(subpath_raw, str) and subpath_raw.strip() else None
    if not project_key or not session_id:
        raise ValueError("transcript key requires non-empty project_key and session_id")
    return project_key, session_id, subpath


def register_transcript_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    repo = TranscriptEntryRepository()

    # ``session_id`` in the path is the platform session (routing/observability +
    # the capability-token binding anchor); the storage scope is the SDK ``key``
    # in the body so resume (load-by-SDK-key) stays faithful to the SessionStore
    # contract.

    async def _authorize(session_id: str, cap_token: str) -> JSONResponse | None:
        """Bind the request to ``session_id`` via its signed capability token.

        Stateless (HMAC verify, no DB read). Returns a rejection response, or
        None when authorized.
        """
        if not verify_transcript_capability_token(session_id, cap_token):
            return JSONResponse(
                status_code=403,
                content=error_response(
                    "FORBIDDEN", "invalid or missing transcript capability token"
                ),
            )
        return None

    # Each ``_do_*`` helper below returns the envelope dict on its success arm,
    # which is what lets the routes' ``response_model`` validate it, and a
    # JSONResponse on a rejection, which bypasses the model and keeps the error
    # envelope's own shape.
    async def _do_append(
        session_id: str, payload: dict[str, Any]
    ) -> JSONResponse | dict[str, Any]:
        try:
            key = payload.get("key") or {}
            entries = payload.get("entries") or []
            if not isinstance(entries, list):
                raise ValueError("entries must be a list")
            append_id = payload.get("append_id")
            if not isinstance(append_id, str) or not append_id.strip():
                raise ValueError("append_id must be a non-empty string")
            _verify_payload_digest(key, entries, payload.get("payload_sha256"))
            project_key, sdk_session_id, subpath = _scope_from_key(key)
            # The hook registry is empty unless app startup explicitly armed the
            # E2E harness, so ordinary append traffic performs no env or file IO.
            if consume_fault(
                "transcript_append_5xx",
                session_id=session_id,
                append_id=append_id,
                entry_count=len(entries),
            ):
                logger.warning(
                    "e2e transcript append temporarily unavailable "
                    "session=%s append_id=%s entry_count=%s",
                    session_id,
                    append_id,
                    len(entries),
                )
                return JSONResponse(
                    status_code=503,
                    content=error_response(
                        "E2E_TRANSCRIPT_APPEND_FAULT",
                        "transcript append temporarily unavailable (E2E fault)",
                    ),
                )
            store_sequence = await repo.append_entries(
                project_key, sdk_session_id, subpath, entries,
                append_id=append_id,
                platform_session_id=session_id,
            )
            return success_response(
                {"ok": True, "count": len(entries), "store_sequence": store_sequence}
            )
        except RuntimeError as exc:
            # The append_id names a batch the store already holds with different
            # entries. 409 rather than 500: the request is what conflicts, and
            # re-sending it unchanged will conflict again.
            logger.warning(
                "transcript append conflicted session=%s reason=%s append_id=%s",
                session_id,
                exc,
                (payload or {}).get("append_id"),
            )
            return JSONResponse(
                status_code=409, content=error_response("APPEND_ID_CONFLICT", str(exc))
            )
        except ValueError as exc:
            # The caller is a machine — the in-box transcript batcher — which logs
            # only the status line, so a 400 whose reason lives solely in the
            # response body is a dead end from both ends. Name it here, with the
            # shape that was rejected: the payload's own field names, never entry
            # contents (those are conversation data).
            logger.warning(
                "transcript append rejected session=%s reason=%s "
                "payload_fields=%s key_fields=%s entries_type=%s",
                session_id,
                exc,
                sorted((payload or {}).keys()),
                sorted((payload or {}).get("key", {}).keys())
                if isinstance((payload or {}).get("key"), dict)
                else type((payload or {}).get("key")).__name__,
                type((payload or {}).get("entries")).__name__,
            )
            return JSONResponse(
                status_code=400, content=error_response("INVALID_REQUEST", str(exc))
            )

    async def _do_load(
        session_id: str, payload: dict[str, Any], *, tenant_fence: str | None
    ) -> JSONResponse | dict[str, Any]:
        try:
            key = payload.get("key") or {}
            project_key, sdk_session_id, subpath = _scope_from_key(key)
            entries = await repo.load_entries(
                project_key, sdk_session_id, subpath,
                platform_session_id=tenant_fence,
            )
            store_sequence = await repo.current_sequence(
                project_key, sdk_session_id, subpath,
                platform_session_id=tenant_fence,
            )
            # ``entries`` is None for a never-written key — preserved verbatim so
            # the adapter can return None (SessionStore contract: load unknown -> None).
            return success_response(
                {"entries": entries, "store_sequence": store_sequence}
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400, content=error_response("INVALID_REQUEST", str(exc))
            )

    async def _do_list_sessions(
        payload: dict[str, Any], *, tenant_fence: str | None
    ) -> JSONResponse | dict[str, Any]:
        project_key = str((payload or {}).get("project_key") or "").strip()
        if not project_key:
            return JSONResponse(
                status_code=400,
                content=error_response("INVALID_REQUEST", "project_key required"),
            )
        sessions = await repo.list_sessions(
            project_key, platform_session_id=tenant_fence
        )
        return success_response({"sessions": sessions})

    async def _do_list_subkeys(
        payload: dict[str, Any], *, tenant_fence: str | None
    ) -> JSONResponse | dict[str, Any]:
        try:
            key = payload.get("key") or {}
            project_key, sdk_session_id, subpath = _scope_from_key(key)
            if subpath is not None:
                raise ValueError("list-subkeys key must not include subpath")
            subkeys = await repo.list_subkeys(
                project_key,
                sdk_session_id,
                platform_session_id=tenant_fence,
            )
            return success_response({"subkeys": subkeys})
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_response("INVALID_REQUEST", str(exc)),
            )

    # ── Capability-scoped routes (the default, authenticated path) ───────────
    _cap = CAPABILITY_PATH_PREFIX

    @app.post(
        _cap + "/{cap_token}/api/v1/transcript/{session_id}/append",
        response_model=ApiEnvelope[TranscriptAppendResult],
        response_model_exclude_unset=True,
    )
    async def transcript_append_scoped(
        cap_token: str, session_id: str, payload: dict[str, Any] = Body(...)
    ):
        rejected = await _authorize(session_id, cap_token)
        return rejected or await _do_append(session_id, payload)

    @app.post(
        _cap + "/{cap_token}/api/v1/transcript/{session_id}/load",
        response_model=ApiEnvelope[TranscriptLoadResult],
        response_model_exclude_unset=True,
    )
    async def transcript_load_scoped(
        cap_token: str, session_id: str, payload: dict[str, Any] = Body(...)
    ):
        rejected = await _authorize(session_id, cap_token)
        # tenant_fence = the token-authorized platform session: the body's
        # sandbox-derived key is intersected with it, so a valid token cannot
        # read another tenant's transcript via a foreign project_key.
        return rejected or await _do_load(session_id, payload, tenant_fence=session_id)

    @app.post(
        _cap + "/{cap_token}/api/v1/transcript/{session_id}/list-sessions",
        response_model=ApiEnvelope[TranscriptSessionList],
        response_model_exclude_unset=True,
    )
    async def transcript_list_sessions_scoped(
        cap_token: str, session_id: str, payload: dict[str, Any] = Body(...)
    ):
        rejected = await _authorize(session_id, cap_token)
        return rejected or await _do_list_sessions(payload, tenant_fence=session_id)

    @app.post(
        _cap + "/{cap_token}/api/v1/transcript/{session_id}/list-subkeys",
        response_model=ApiEnvelope[TranscriptSubkeyList],
        response_model_exclude_unset=True,
    )
    async def transcript_list_subkeys_scoped(
        cap_token: str, session_id: str, payload: dict[str, Any] = Body(...)
    ):
        rejected = await _authorize(session_id, cap_token)
        return rejected or await _do_list_subkeys(payload, tenant_fence=session_id)

    # ── Optional unscoped routes ─────────────────────────────────────────────
    # Fail-closed by default (401 — no capability token in the path). The
    # opt-out supports a network-isolated single-tenant deployment that cannot
    # carry the token.
    legacy_open = not transcript_capability_required()

    @app.post(
        "/api/v1/transcript/{session_id}/append",
        response_model=ApiEnvelope[TranscriptAppendResult],
        response_model_exclude_unset=True,
    )
    async def transcript_append(session_id: str, payload: dict[str, Any] = Body(...)):
        if not legacy_open:
            return _fail_closed()
        return await _do_append(session_id, payload)

    @app.post(
        "/api/v1/transcript/{session_id}/load",
        response_model=ApiEnvelope[TranscriptLoadResult],
        response_model_exclude_unset=True,
    )
    async def transcript_load(session_id: str, payload: dict[str, Any] = Body(...)):
        if not legacy_open:
            return _fail_closed()
        # Explicit single-tenant opt-out: no tenant fence is available without
        # a scoped capability token.
        return await _do_load(session_id, payload, tenant_fence=None)

    @app.post(
        "/api/v1/transcript/{session_id}/list-sessions",
        response_model=ApiEnvelope[TranscriptSessionList],
        response_model_exclude_unset=True,
    )
    async def transcript_list_sessions(session_id: str, payload: dict[str, Any] = Body(...)):
        if not legacy_open:
            return _fail_closed()
        return await _do_list_sessions(payload, tenant_fence=None)

    @app.post(
        "/api/v1/transcript/{session_id}/list-subkeys",
        response_model=ApiEnvelope[TranscriptSubkeyList],
        response_model_exclude_unset=True,
    )
    async def transcript_list_subkeys(
        session_id: str, payload: dict[str, Any] = Body(...)
    ):
        if not legacy_open:
            return _fail_closed()
        return await _do_list_subkeys(payload, tenant_fence=None)


def _fail_closed() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=error_response(
            "UNAUTHORIZED",
            "transcript requires a per-session capability token; the sandbox "
            "receives a capability-scoped base URL automatically. Set "
            "ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED=false only for a "
            "network-isolated single-tenant deployment.",
        ),
    )
