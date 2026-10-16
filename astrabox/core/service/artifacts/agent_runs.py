"""Page-triggered conversations: a new conversation of the origin, and one message in it.

A run is that message. Its state is the response that answers it, as the
conversation's runtime recorded it, so whatever else happens later in the same
conversation leaves the run's state alone.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.persistence.sql import transaction

from . import limits
from .origin import OriginAccess
from .service import ArtifactService, ArtifactViewer, invalid, opaque_viewer_id

#: Where a run's message came from, on the input the platform records.
_SOURCE = "artifact"


def run_message_id(session_id: str) -> str:
    """The id a run's message is sent under; a run is its conversation's one message."""
    return f"artifact-run:{session_id}"


def run_state(
    session: dict[str, Any], sent: dict[str, Any] | None, response: dict[str, Any] | None
) -> dict[str, Any]:
    """The run's public state from its message and the response to it.

    ``running`` until the response ends, ``waiting_input`` while it waits on
    a person, then ``completed`` or ``failed`` as the runtime ended it. A
    stopped response counts as completed: the run is over and did not fail.
    A run whose conversation was terminated before its response ended has
    failed, since nothing can answer it any more.
    """
    result: dict[str, Any] = {"session_id": session["session_id"]}
    if sent is None:
        # The message was never accepted: the run's start failed after its
        # conversation was created.
        return {**result, "state": "failed", "error": "the run's message was not accepted"}
    if sent["state"] == "failed":
        failure = sent.get("failure") or {}
        return {**result, "state": "failed", "error": str(failure.get("message") or failure.get("code") or "")}
    if session.get("state") == "TERMINATED" and (response is None or response["status"] != "ended"):
        return {**result, "state": "failed", "error": "the run's conversation ended before it answered"}
    if response is None or response["status"] == "running":
        return {**result, "state": "running"}
    if response["status"] == "waiting":
        return {**result, "state": "waiting_input"}
    if response.get("outcome") == "failed":
        error = response.get("error") or {}
        failed = {**result, "state": "failed"}
        if error.get("message"):
            failed["error"] = str(error["message"])
        return failed
    return {**result, "state": "completed"}


class ArtifactAgentRuns:
    def __init__(
        self,
        *,
        artifacts: ArtifactService,
        origin_access: OriginAccess,
        platform: Any,
        agent_service_getter: Any,
        assistant_service_getter: Any,
    ) -> None:
        self._artifacts, self._origin_access, self._platform = artifacts, origin_access, platform
        self._agent_getter, self._assistant_getter = agent_service_getter, assistant_service_getter
        self._facts = SessionRuntimeRepository()

    @staticmethod
    def _limited() -> APIError:
        return APIError(
            code="ARTIFACT_AGENT_RUN_LIMITED",
            message="one active run per artifact and ten runs per hour are allowed",
            status_code=429,
        )

    async def _state(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["session_id"])
        sent = await self._facts.input_by_client_message_id(session_id, run_message_id(session_id))
        response_id = str((sent or {}).get("response_id") or "")
        response = await self._facts.get_response(session_id, response_id) if response_id else None
        return run_state(session, sent, response)

    async def _running(self, session_id: str) -> bool:
        """Whether the run in ``session_id`` is still running or waiting for input."""
        session = await self._platform._sessions_repo.get_session(session_id)
        if not session:
            return False
        return (await self._state(session))["state"] in {"running", "waiting_input"}

    async def _reserve(self, viewer: ArtifactViewer, reservation: str) -> None:
        """Admit one run: one active run per viewer and artifact, ten per viewer an hour.

        A reservation counts as active until its conversation is seen to have
        finished. Run states come from the Sessions, which the budget
        transaction cannot read, so they are read before it.
        """
        key = "agent_budget:" + opaque_viewer_id("agent-budget", viewer.user.user_id)
        budgets = self._artifacts.budgets
        running = {
            session_id: await self._running(session_id)
            for session_id in await budgets.run_session_ids(key)
        }

        async def write(conn: AsyncConnection) -> None:
            await budgets.lock(key, "agent", conn=conn)
            now = utcnow()
            recent, expired = 0, []
            for run in await budgets.runs(key, conn=conn):
                age = (now - parse_iso(run["created_at"])).total_seconds()
                session_id = run["session_id"]
                if session_id:
                    # A run whose conversation started after the reads above
                    # has only just begun.
                    active = running.get(session_id, True)
                else:
                    active = age < limits.AGENT_WINDOW_SECONDS
                if active and run["artifact_id"] == viewer.artifact_id:
                    raise self._limited()
                if age < limits.AGENT_WINDOW_SECONDS:
                    recent += 1
                elif not active:
                    expired.append(run["reservation_id"])
            if recent >= limits.AGENT_PER_HOUR:
                raise self._limited()
            await budgets.drop_runs(expired, conn=conn)
            await budgets.add_run(
                key, reservation, artifact_id=viewer.artifact_id, created_at=now, conn=conn
            )

        await transaction(write)

    async def _settle_reservation(self, reservation: str, session_id: str | None) -> None:
        """Attach the run's conversation, or drop the reservation when none started."""
        budgets = self._artifacts.budgets
        if session_id is None:
            await budgets.drop_runs([reservation])
        else:
            await budgets.settle_run(reservation, session_id)

    async def start(self, viewer: ArtifactViewer, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise invalid("prompt must be non-empty")
        viewer, record, version = await self._artifacts.refresh(viewer)
        self._artifacts.require(viewer, version, "agent")
        harness = await self._origin_access.authorize(viewer, record, run=True)
        if not harness.artifacts_enabled:
            # A run reaches its artifact only through the artifacts tools, and
            # an Agent with Artifacts turned off gives its conversations none.
            # Starting it anyway would report a completed run that could not
            # have read or written the page. Status reads of a run already
            # started stay available.
            raise APIError(
                code="ARTIFACT_AGENT_UNAVAILABLE",
                message="the origin Agent has Artifacts turned off",
                status_code=503,
            )
        reservation = uuid.uuid4().hex
        await self._reserve(viewer, reservation)
        origin = record["origin"]
        try:
            if origin.get("assistant_id"):
                result = await self._assistant_getter().start_conversation(
                    viewer.user, origin["assistant_id"]
                )
            else:
                result = await self._agent_getter().start_conversation(
                    viewer.user, origin["agent_id"]
                )
        except BaseException:
            await self._settle_reservation(reservation, None)
            raise
        sid = result["session_id"]
        grant = {"artifact_id": viewer.artifact_id, "source": "owner"}
        if viewer.level != "owner":
            grant = {"artifact_id": viewer.artifact_id, "source": "share", "token": viewer.token}
        await self._platform._sessions_repo.update_session(
            sid, {"artifact_grant": grant, "artifact_run": {"artifact_id": viewer.artifact_id}}
        )
        await self._settle_reservation(reservation, sid)
        preamble = (
            f"This run was requested by an interactive AstraBox artifact.\n"
            f"Artifact id: {viewer.artifact_id}\nTitle: {record['title']}\nCurrent version: {record['current_version']}\nViewer opaque id: {viewer.id}\n"
            f"Your artifacts tools can read this artifact and read/write its db as the requesting viewer "
            f"under its manifest rules ({viewer.level}); {{self}} means this viewer's opaque id. "
            + (
                "You may update/publish the artifact because this viewer owns it.\n"
                if viewer.level == "owner"
                else "You may not update/publish the artifact or read other artifacts belonging to its author.\n"
            )
            + "Use artifact_guide for the API. Fulfil the page's request below, writing results to its db when appropriate.\n\n"
        )
        # The platform holds the message until the conversation's runtime has
        # started and takes it.
        await self._platform.session_runtime.send_as(
            sid,
            preamble + prompt,
            author_user_id=viewer.user.user_id,
            source=_SOURCE,
            client_message_id=run_message_id(sid),
        )
        return {
            "run_id": sid,
            "session_id": sid,
            "url": f"{self._artifacts._public_base_url}/sessions/{sid}",
        }

    async def status(self, viewer: ArtifactViewer, run_id: str) -> dict[str, Any]:
        viewer, record, version = await self._artifacts.refresh(viewer)
        self._artifacts.require(viewer, version, "agent")
        await self._origin_access.authorize(viewer, record, run=True)
        session = await self._platform._sessions_repo.get_session(run_id)
        if (
            not session
            or viewer.user is None
            or session.get("user_id") != viewer.user.user_id
            or (session.get("artifact_run") or {}).get("artifact_id") != viewer.artifact_id
        ):
            raise APIError(
                code="ARTIFACT_AGENT_RUN_NOT_FOUND",
                message="artifact run not found",
                status_code=404,
            )
        return await self._state(session)
