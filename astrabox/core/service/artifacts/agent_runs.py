"""Page-triggered conversations use platform Session creation and turn dispatch."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, utcnow, utcnow_iso
from astrabox.core.service.orchestrator.agent.agent_service import _AGENT_STARTUP_TIMEOUT_SECONDS

from . import limits
from .origin import OriginAccess
from .service import ArtifactService, ArtifactViewer, invalid, opaque_viewer_id


def run_state(session: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = snapshot or {}
    run = session.get("artifact_run", {})
    error = (
        run.get("error")
        or snapshot.get("last_turn_error")
        or snapshot.get("last_error")
        or session.get("last_error")
    )
    state = snapshot.get("conversation_state") or session.get("state")
    lifecycle = snapshot.get("session_lifecycle_state") or session.get("state")
    turn_status = snapshot.get("last_turn_status") or session.get("last_turn_status")
    if error or turn_status == "FAILED" or lifecycle in {"FAILED", "DELETED"}:
        result = {"state": "failed", "session_id": session["session_id"]}
        if error:
            result["error"] = str(error)
        return result
    if lifecycle == "TERMINATED":
        completed = turn_status == "COMPLETED" or run.get("finished")
        return {
            "state": "completed" if completed else "failed",
            "session_id": session["session_id"],
        }
    if state in {"WAITING_FOR_INTERACTION", "WAITING_INPUT"}:
        return {"state": "waiting_input", "session_id": session["session_id"]}
    completed = state in {"READY", "IDLE"} and (turn_status == "COMPLETED" or run.get("finished"))
    return {"state": "completed" if completed else "running", "session_id": session["session_id"]}


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

    @staticmethod
    def _limited() -> APIError:
        return APIError(
            code="ARTIFACT_AGENT_RUN_LIMITED",
            message="one active run per artifact and ten runs per hour are allowed",
            status_code=429,
        )

    async def _reserve(self, viewer: ArtifactViewer, reservation: str) -> str:
        key = "agent_budget:" + opaque_viewer_id("agent-budget", viewer.user.user_id)

        async def write(tx: Any) -> None:
            budget = await self._artifacts.viewers.lock(key, transaction=tx)
            now = utcnow()
            entries = []
            for entry in (budget or {}).get("runs", []):
                age = (now - parse_iso(entry["created_at"])).total_seconds()
                sid = entry.get("session_id")
                active = False
                if sid:
                    session = await tx.collection("sessions").find_one({"session_id": sid})
                    snapshot = await tx.collection("session_snapshots").find_one(
                        {"session_id": sid}
                    )
                    active = bool(
                        session
                        and run_state(session, snapshot)["state"] in {"running", "waiting_input"}
                    )
                else:
                    active = age < limits.AGENT_WINDOW_SECONDS
                if active and entry["artifact_id"] == viewer.artifact_id:
                    raise self._limited()
                if age < limits.AGENT_WINDOW_SECONDS or active:
                    entries.append(entry)
            recent = sum(
                (now - parse_iso(e["created_at"])).total_seconds() < limits.AGENT_WINDOW_SECONDS
                for e in entries
            )
            if recent >= limits.AGENT_PER_HOUR:
                raise self._limited()
            entries.append(
                {
                    "reservation": reservation,
                    "artifact_id": viewer.artifact_id,
                    "created_at": utcnow_iso(),
                }
            )
            row = {"doc_id": key, "runs": entries}
            if budget:
                await self._artifacts.viewers.put(row, transaction=tx)
            else:
                await self._artifacts.viewers.insert(row, transaction=tx)

        try:
            await self._artifacts.transaction(write)
        except Exception as exc:
            from astrabox.persistence.repository.backend import DuplicateKeyError

            if isinstance(exc, DuplicateKeyError):
                raise self._limited() from exc
            raise
        return key

    async def _settle_reservation(self, key: str, reservation: str, session_id: str | None) -> None:
        async def write(tx: Any) -> None:
            budget = await self._artifacts.viewers.lock(key, transaction=tx)
            entries = budget["runs"]
            if session_id is None:
                entries = [e for e in entries if e["reservation"] != reservation]
            else:
                for entry in entries:
                    if entry["reservation"] == reservation:
                        entry["session_id"] = session_id
            await self._artifacts.viewers.put({"doc_id": key, "runs": entries}, transaction=tx)

        await self._artifacts.transaction(write)

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
        budget_key = await self._reserve(viewer, reservation)
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
            await self._settle_reservation(budget_key, reservation, None)
            raise
        sid = result["session_id"]
        grant = {"artifact_id": viewer.artifact_id, "source": "owner"}
        if viewer.level != "owner":
            grant = {"artifact_id": viewer.artifact_id, "source": "share", "token": viewer.token}
        await self._platform._sessions_repo.update_session(
            sid, {"artifact_grant": grant, "artifact_run": {"artifact_id": viewer.artifact_id}}
        )
        await self._platform._platform_mcp_binding_repo.attach_session_grant(sid, grant)
        await self._settle_reservation(budget_key, reservation, sid)
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
        self._platform._spawn_background_task(
            self._drive(viewer, sid, preamble + prompt), name=f"artifact-agent-{sid}"
        )
        return {
            "run_id": sid,
            "session_id": sid,
            "url": f"{self._artifacts._public_base_url}/sessions/{sid}",
        }

    async def _drive(self, viewer: ArtifactViewer, session_id: str, prompt: str) -> None:
        try:
            deadline = asyncio.get_running_loop().time() + _AGENT_STARTUP_TIMEOUT_SECONDS
            while True:
                session = await self._platform.get_session(viewer.user, session_id)
                if session.get("state") != "CREATING":
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise APIError(
                        code="ARTIFACT_AGENT_UNAVAILABLE",
                        message="origin conversation startup timed out",
                        status_code=503,
                    )
                await asyncio.sleep(0.25)
            # The Agent MCP facade sends through this same Session kernel stream path.
            async for _ in self._platform.stream_message_events_ds(
                viewer.user, session_id, prompt, client_message_id=str(uuid.uuid4())
            ):
                pass
            row = await self._platform._sessions_repo.get_session(session_id)
            await self._platform._sessions_repo.update_session(
                session_id, {"artifact_run": {**row["artifact_run"], "finished": True}}
            )
        except Exception as exc:
            row = await self._platform._sessions_repo.get_session(session_id)
            if row:
                await self._platform._sessions_repo.update_session(
                    session_id, {"artifact_run": {**row["artifact_run"], "error": str(exc)}}
                )
            raise

    async def status(self, viewer: ArtifactViewer, run_id: str) -> dict[str, Any]:
        viewer, record, version = await self._artifacts.refresh(viewer)
        self._artifacts.require(viewer, version, "agent")
        await self._origin_access.authorize(viewer, record, run=True)
        session = await self._platform._sessions_repo.get_session(run_id)
        if (
            not session
            or viewer.user is None
            or session.get("user_id") != viewer.user.user_id
            or session.get("artifact_run", {}).get("artifact_id") != viewer.artifact_id
        ):
            raise APIError(
                code="ARTIFACT_AGENT_RUN_NOT_FOUND",
                message="artifact run not found",
                status_code=404,
            )
        snapshot = await self._platform._session_snapshots_repo.get_snapshot(run_id)
        return run_state(session, snapshot)
