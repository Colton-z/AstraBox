"""The platform's own name for a durable workspace.

Storage is a platform capability and must not be laid out by business data. An
Agent is configuration a developer creates and deletes; the conversations that
ran under it produced user files that have to be archived, so a path shaped
`.../{agent_id}/...` leaves a tree nobody may delete and nobody can attribute
once the Agent is gone.

So the dependency runs the other way: the platform mints a workspace id in a
namespace it owns, and the row that owns the data points at it. Deleting an
Agent deletes an Agent. Archiving a workspace is an operation the platform
performs over its own names, asking no business table anything.

The id is minted once and then read back, because the path it produces has to
be the same one tomorrow — a workspace whose name is re-derived is a workspace
the next box will not find.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

#: The field both owning rows carry. One name, because the read that resolves it
#: is one function and a second spelling would be a second answer.
WORKSPACE_ID_FIELD = "workspace_id"


def mint_workspace_id() -> str:
    """A name for a workspace whose owner does not exist yet.

    A prepared box is disposable until it is claimed: it and the directory it
    was given are made together and, if nobody takes them, destroyed together.
    The claim is what turns the name into user data, by recording it on the row
    that will answer for it.
    """

    return uuid.uuid4().hex


def _minted() -> str:
    return mint_workspace_id()


async def ensure_workspace_id(
    *,
    subject_kind: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    assistant_id: str | None = None,
) -> str:
    """Read this subject's workspace id, minting and persisting one if absent.

    The owning row is the subject's own: a conversation's is its session, an
    Assistant's is its catalog entry. Both are the row whose deletion means the
    files are the user's to lose, which is what makes them the right place to
    keep the name.

    A whole conversation box provisioned before its Session exists has no owning
    row and therefore receives an explicitly minted id from its caller. An
    Agent-shared box does have a durable owner before any Session exists: the
    Agent row keeps its one workspace id so every replacement mounts the same
    root.
    """

    kind = str(subject_kind or "").strip()
    if kind == "deployment_conversation":
        return await _ensure_session_workspace_id(session_id)
    if kind == "deployment_runtime":
        return await _ensure_agent_workspace_id(agent_id)
    if kind == "assistant_runtime":
        return await _ensure_assistant_workspace_id(assistant_id)
    raise APIError(
        code="NAS_MOUNT_FAILED",
        message=(
            f"cannot resolve a workspace id for subject_kind {subject_kind!r}"
        ),
        status_code=500,
    )


async def claim_workspace_id(session_id: str, workspace_id: str) -> str:
    """Make a precreated workspace the Session's durable workspace exactly once."""

    from astrabox.persistence.repository.session_repository import SessionRepository

    key = str(session_id or "").strip()
    candidate = str(workspace_id or "").strip()
    if not key or not candidate:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message="claiming a precreated workspace requires Session and workspace ids",
            status_code=500,
        )
    repository = SessionRepository()
    for _attempt in range(3):
        row = await repository.get_session(key)
        if row is None:
            raise APIError(
                code="SESSION_NOT_FOUND",
                message=f"session {key!r} has no row to claim a workspace on",
                status_code=404,
            )
        existing = str(row.get(WORKSPACE_ID_FIELD) or "").strip()
        if existing:
            if existing == candidate:
                return existing
            raise APIError(
                code="NAS_MOUNT_FAILED",
                message=(
                    f"session {key!r} already owns workspace {existing!r} and "
                    f"cannot claim {candidate!r}"
                ),
                status_code=409,
            )
        expected = (
            row.get(WORKSPACE_ID_FIELD)
            if WORKSPACE_ID_FIELD in row
            else {"$exists": False}
        )
        if await repository.compare_and_update_session(
            key,
            expected={WORKSPACE_ID_FIELD: expected},
            updates={WORKSPACE_ID_FIELD: candidate},
        ):
            logger.info(
                "claimed a precreated workspace: session=%s workspace=%s",
                key,
                candidate,
            )
            return candidate
    raise APIError(
        code="NAS_MOUNT_FAILED",
        message=f"session {key!r} changed repeatedly while claiming its workspace",
        status_code=409,
    )


async def _ensure_session_workspace_id(session_id: str | None) -> str:
    from astrabox.persistence.repository.session_repository import SessionRepository

    key = str(session_id or "").strip()
    if not key:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message="a conversation's workspace id needs its session",
            status_code=500,
        )
    repository = SessionRepository()
    row = await repository.get_session(key)
    if row is None:
        raise APIError(
            code="SESSION_NOT_FOUND",
            message=f"session {key!r} has no row to keep a workspace id on",
            status_code=404,
        )
    existing = str(row.get(WORKSPACE_ID_FIELD) or "").strip()
    if existing:
        return existing
    minted = _minted()
    await repository.update_session(key, {WORKSPACE_ID_FIELD: minted})
    logger.info(
        "minted a workspace id for session=%s workspace=%s", key, minted
    )
    return minted


async def _ensure_agent_workspace_id(agent_id: str | None) -> str:
    """Return the durable root mounted into an Agent-shared runtime box.

    That box can be created before any Session exists and can host several
    Sessions over its lifetime, so neither a Session row nor a sandbox id can
    own the storage name.  The Agent row is the durable platform pointer every
    replacement box can resolve before a conversation is placed.
    """

    from astrabox.persistence.repository.agent_repository import AgentRepository

    key = str(agent_id or "").strip()
    if not key:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message="an Agent runtime's workspace id needs its Agent",
            status_code=500,
        )
    repository = AgentRepository()
    # Pool replenishment and a cold Session start can reach this concurrently.
    # Publishing with CAS is what keeps their boxes on one durable root: two
    # blind writes could each mount a different volume and let the last write
    # silently strand files in the first one.
    for _attempt in range(3):
        row = await repository.get_agent(key)
        if row is None:
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent {key!r} has no row to keep a workspace id on",
                status_code=404,
            )
        existing = str(row.get(WORKSPACE_ID_FIELD) or "").strip()
        if existing:
            return existing
        minted = _minted()
        expected = (
            row.get(WORKSPACE_ID_FIELD)
            if WORKSPACE_ID_FIELD in row
            else {"$exists": False}
        )
        if await repository.compare_and_update_agent(
            key,
            expected={WORKSPACE_ID_FIELD: expected},
            updates={WORKSPACE_ID_FIELD: minted},
        ):
            logger.info(
                "minted a workspace id for agent=%s workspace=%s", key, minted
            )
            return minted
    raise APIError(
        code="NAS_MOUNT_FAILED",
        message=(
            f"agent {key!r} changed repeatedly while its durable workspace "
            "identity was being published"
        ),
        status_code=500,
    )


async def _ensure_assistant_workspace_id(assistant_id: str | None) -> str:
    from astrabox.persistence.repository.assistant_catalog_repository import (
        AssistantCatalogRepository,
    )

    key = str(assistant_id or "").strip()
    if not key:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message="an Assistant's workspace id needs its assistant",
            status_code=500,
        )
    repository = AssistantCatalogRepository()
    row: Any = await repository.get_assistant(key)
    if row is None:
        raise APIError(
            code="ASSISTANT_NOT_FOUND",
            message=f"assistant {key!r} has no row to keep a workspace id on",
            status_code=404,
        )
    existing = str(row.get(WORKSPACE_ID_FIELD) or "").strip()
    if existing:
        return existing
    minted = _minted()
    await repository.update_assistant(key, {WORKSPACE_ID_FIELD: minted})
    logger.info(
        "minted a workspace id for assistant=%s workspace=%s", key, minted
    )
    return minted


__all__ = [
    "WORKSPACE_ID_FIELD",
    "claim_workspace_id",
    "ensure_workspace_id",
    "mint_workspace_id",
]
