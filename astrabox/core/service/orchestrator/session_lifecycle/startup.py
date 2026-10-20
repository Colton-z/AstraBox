"""Starting a session's runtime: from ``CREATING`` to ``READY``.

One startup drives one session under a :class:`~.ownership.RecoveryOwnership`
claim. The session's runtime subject (``runtime_subject.py``) decides whether
it creates a runtime, attaches to one, or reuses a binding its owner already
prepared; the runtime manager does the sandbox work, a prepared-slot claim
included. The ``READY`` write publishes the box and sends the session's
``reader`` notice from the same statement, so a reader in any process starts
on the runtime as soon as it can be used.

A startup that fails leaves the session marked: ``TERMINATED`` with its
error, or ``READY`` carrying the action only the user can take
(``startup_user_action``). A box it created is released, or recorded on the
row's undestroyed ledger when its destruction is not confirmed.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import asdict
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.chunk_processing import extract_agent_session_metadata
from astrabox.core.service.orchestrator.engine.base import bound_engine_client_manifest
from astrabox.core.service.orchestrator.runtime.preparation import require_session_user_prerequisites
from astrabox.core.service.orchestrator.runtime_binding import is_assistant_workspace_bootstrap
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeStartupCleanup,
    RuntimeStartupTarget,
)
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    CALLBACK_SUBJECT_SESSION,
    build_sandbox_callback_url_from_record,
)
from astrabox.core.service.orchestrator.sandbox_names import (
    UNDESTROYED_SANDBOX_IDS,
    keep_name_updates,
    read_undestroyed,
)
from astrabox.core.service.orchestrator.session_lifecycle.ownership import RecoveryOwnership
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    validated_user_action,
)

logger = get_logger(__name__)


class SessionStartup:
    """Runs one session startup to ``READY`` or to a recorded failure."""

    def __init__(
        self,
        *,
        sessions_repo: Any,
        agent_config: Any,
        runtime_manager: Any,
        runtime_subjects: Any,
    ) -> None:
        self._sessions = sessions_repo
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._runtime_subjects = runtime_subjects

    async def run(
        self,
        *,
        session_id: str,
        owner: str | None,
        sandbox_generation: str,
        resume_engine_session_key: str | None,
        replacing: bool = False,
    ) -> dict[str, Any]:
        """Start the session's runtime; the row as the startup left it.

        ``replacing`` marks a start that replaces a runtime the conversation
        lost: its failure leaves the conversation open for a later message.

        ``owner`` and ``sandbox_generation`` are the claim the caller made on
        the ``CREATING`` row. Each startup gets its own sandbox assignment, the
        identity the provider records on the box it creates or claims.
        """
        ownership = RecoveryOwnership(
            self._sessions,
            session_id,
            owner,
            sandbox_generation=sandbox_generation,
            assignment_id=str(uuid.uuid4()),
        )
        session = await ownership.require_current()
        try:
            template = await self._agent_config.resolve_session_harness(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._fail_before_runtime(
                ownership,
                _failure_text(exc, "failed to load the session's configuration"),
                replacing=replacing,
            )
        if template is None:
            return await self._fail_before_runtime(
                ownership,
                "the session's Agent or Assistant configuration was not found",
                replacing=replacing,
            )
        with ownership.bind():
            return await self._start(
                session=session,
                ownership=ownership,
                template=template,
                resume_engine_session_key=resume_engine_session_key,
                replacing=replacing,
            )

    async def _start(
        self,
        *,
        session: dict[str, Any],
        ownership: RecoveryOwnership,
        template: Any,
        resume_engine_session_key: str | None,
        replacing: bool,
    ) -> dict[str, Any]:
        session_id = ownership.session_id
        user_id = str(session.get("user_id") or "").strip()
        permission_mode = str(session.get("permission_mode") or "").strip() or None
        progress = _StartupProgress(ownership)
        target: RuntimeStartupTarget | None = None

        async def release(
            failure_phase: str, created_sandbox_id: str | None = None
        ) -> RuntimeStartupCleanup:
            await ownership.require_current()
            cleanup = await self._runtime_subjects.cleanup_failed_startup_runtime(
                session_id=session_id,
                target=target,
                sandbox_id=created_sandbox_id,
            )
            created = (
                str(created_sandbox_id or "").strip()
                if target is not None and target.action == "create_runtime"
                else ""
            )
            current = await self._sessions.get_session(session_id)
            if not isinstance(current, dict):
                return cleanup
            with contextlib.suppress(BaseException):
                await self._runtime_subjects.converge_startup_failure(
                    session=target.session if target is not None else current,
                    failure_phase=failure_phase,
                    created_sandbox_id=created or None,
                    cleanup=cleanup.destruction,
                )
            return cleanup

        async def fail(
            error_text: str,
            failure_phase: str,
            created_sandbox_id: str | None = None,
            user_action: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            cleanup = await release(failure_phase, created_sandbox_id)
            latest = await self._mark_failed(
                ownership,
                replacing=replacing,
                error_text=error_text,
                leaked_sandbox_id=cleanup.leaked_sandbox_id,
                user_action=user_action,
            )
            return latest or {"session_id": session_id}

        try:
            session = await ownership.require_current()
            await require_session_user_prerequisites(session)
            await ownership.require_current()
            target = await self._runtime_subjects.acquire_startup(
                session_id=session_id,
                template=template,
                sandbox_generation=ownership.sandbox_generation,
                resume_engine_session_key=resume_engine_session_key,
                on_progress=progress.emit,
            )
            plan = target.workspace_plan
            if target.action == "use_ready_binding":
                sandbox_id = str(plan.sandbox_id or "").strip()
                if not sandbox_id:
                    raise APIError(
                        code="RUNTIME_SUBJECT_INVALID",
                        message="A ready runtime binding has no sandbox_id",
                        status_code=500,
                    )
                await progress.drain()
                ready = await self._publish_ready(
                    ownership,
                    {
                        "state": SessionState.READY.value,
                        "sandbox_id": sandbox_id,
                        "expires_at": target.binding_expires_at,
                        "runtime_unavailable": False,
                        "last_error": None,
                        "startup_progress": None,
                        "model_name": self._runtime_manager.resolve_template_model_name(template),
                        "terminal_cwd": plan.cwd,
                    },
                )
                logger.info(
                    "session startup reused a prepared runtime binding: "
                    "session=%s subject=%s sandbox=%s",
                    session_id,
                    plan.subject_kind,
                    sandbox_id,
                )
                return ready

            await ownership.require_current()
            if target.action == "create_runtime":
                await progress.emit("creating_sandbox")
                inner = asyncio.ensure_future(
                    self._runtime_manager.create_runtime(
                        session_id,
                        template,
                        assignment_id=str(ownership.assignment_id),
                        startup_guard=ownership.require_current,
                        user_id=user_id,
                        permission_mode=permission_mode,
                        progress_callback=progress.emit,
                        callback_url=build_sandbox_callback_url_from_record(
                            subject_type=CALLBACK_SUBJECT_SESSION,
                            subject_id=session_id,
                            record=target.session,
                        ),
                        workspace_plan=plan,
                    )
                )
            elif target.action == "attach_runtime":
                inner = asyncio.ensure_future(
                    self._runtime_manager.ensure_runtime(
                        session_id,
                        template,
                        sandbox_id=str(plan.sandbox_id or "").strip(),
                        startup_guard=ownership.require_current,
                        user_id=user_id,
                        permission_mode=permission_mode,
                        session_kind=plan.session_kind,
                        engine_session_key=plan.resume_engine_session_key,
                        workspace_plan=plan,
                    )
                )
            else:
                raise APIError(
                    code="RUNTIME_SUBJECT_INVALID",
                    message=f"Unknown runtime startup action: {target.action!r}",
                    status_code=500,
                )
            runtime = await _settled(inner, session_id)
        except APIError as exc:
            created_sandbox_id = ""
            if isinstance(exc.data, dict):
                created_sandbox_id = str(
                    exc.data.get("sandbox_id") or exc.data.get("leaked_sandbox_id") or ""
                ).strip()
            # A prerequisite only the user can complete is not a runtime fault:
            # the conversation stays open and shows the action, and a later
            # recovery starts the runtime once the provider reports it met.
            user_action = (
                validated_user_action((exc.evidence or {}).get("user_action"))
                if exc.code == TURN_PREPARATION_USER_ACTION_REQUIRED
                else None
            )
            return await fail(
                exc.message, "runtime_start_failed", created_sandbox_id or None, user_action
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_text = f"failed to start remote-agent runtime: {exc}"
            cleanup = await release("runtime_start_failed")
            try:
                latest = await self._mark_failed(
                    ownership,
                    replacing=replacing,
                    error_text=error_text,
                    leaked_sandbox_id=cleanup.leaked_sandbox_id,
                    user_action=None,
                )
            except Exception:
                # Marking the failure is the last line between a failed startup
                # and a session left in CREATING with no error.
                logger.critical(
                    "session start-failed marking itself failed; the session may be "
                    "stuck CREATING: session=%s original_error=%s",
                    session_id,
                    error_text,
                    exc_info=True,
                )
                latest = None
            return latest or {"session_id": session_id}

        await ownership.require_current()
        current = await self._sessions.get_session(session_id)
        if str((current or {}).get("state") or "") not in {
            SessionState.CREATING.value,
            SessionState.READY.value,
        }:
            aborted_sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip() or None
            cleanup = await release("startup_aborted", aborted_sandbox_id)
            # Only a create action owns an allocation to release. An attach
            # points at a longer-lived subject runtime and cleanup returns no
            # destruction verdict for this session.
            keep = keep_name_updates(cleanup.destruction, row=current)
            if keep:
                logger.error(
                    "aborted startup could not confirm the destruction of session=%s sandbox=%s: %s",
                    session_id,
                    aborted_sandbox_id,
                    cleanup.destruction.detail
                    if cleanup.destruction is not None
                    else "cleanup produced no verdict",
                )
                with contextlib.suppress(Exception):
                    await ownership.update(keep)
            return await self._sessions.get_session(session_id) or current or {"session_id": session_id}

        sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip()
        if not sandbox_id:
            return await fail("runtime startup returned no sandbox_id", "ready_precondition_failed")
        if target.renewal_ttl_seconds is not None:
            try:
                await self._runtime_manager.renew_sandbox_by_id(
                    sandbox_id, ttl_seconds=target.renewal_ttl_seconds
                )
            except Exception as exc:
                return await fail(
                    f"failed to renew runtime-subject sandbox session={session_id} "
                    f"sandbox={sandbox_id}: {exc}",
                    "renew_failed",
                    sandbox_id,
                )
        try:
            real_expires_at = await self._runtime_manager.get_sandbox_expires_at(sandbox_id)
            expires_at = real_expires_at.isoformat() if real_expires_at is not None else None
            await self._runtime_subjects.publish_runtime_ready(
                session=target.session,
                template=template,
                sandbox_id=sandbox_id,
                expires_at=expires_at,
                runtime_identity=getattr(runtime, "runtime_identity", None),
            )
        except Exception as exc:
            return await fail(
                f"failed to publish runtime subject readiness: {exc}",
                "ready_publish_failed",
                sandbox_id,
            )
        updates: dict[str, Any] = {
            "state": SessionState.READY.value,
            "sandbox_id": sandbox_id,
            "startup_allocation": None,
            "expires_at": expires_at,
            "runtime_unavailable": False,
            "last_error": None,
            "startup_progress": None,
            "startup_user_action": None,
            "model_name": self._runtime_manager.resolve_template_model_name(template),
        }
        terminal_cwd = str(getattr(runtime, "terminal_cwd", "") or "").strip()
        if terminal_cwd:
            updates["terminal_cwd"] = terminal_cwd
        engine_session_key = str(getattr(runtime, "engine_session_key", "") or "").strip()
        if resume_engine_session_key or engine_session_key:
            updates["engine_session_key"] = engine_session_key or resume_engine_session_key
        runtime_identity = getattr(runtime, "runtime_identity", None)
        if target.persist_runtime_identity_on_session and runtime_identity:
            # A session-owned subject carries its recovery identity on the
            # session; shared subjects publish owner identity through their
            # provider instead.
            updates["runtime_identity"] = runtime_identity
        updates.update(await runtime_initialization_metadata(session_id, runtime))
        await progress.drain()
        ready = await self._publish_ready(ownership, updates)
        self._runtime_manager.forget_published_startup_allocation(session_id, sandbox_id=sandbox_id)
        if is_assistant_workspace_bootstrap(target.session):
            # A workspace's own Session has prepared its box (a first
            # materialization or a profile refresh). Conversations attach on
            # their own and this Session has no reader, so it keeps no client.
            await self._runtime_manager.evict_runtime(session_id)
        return ready

    async def _publish_ready(
        self, ownership: RecoveryOwnership, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Write ``READY`` under the claim, announcing the runtime to every reader host."""
        await ownership.update(updates, notify="reader")
        latest = await self._sessions.get_session(ownership.session_id)
        return latest if isinstance(latest, dict) else {"session_id": ownership.session_id, **updates}

    async def _fail_before_runtime(
        self, ownership: RecoveryOwnership, error_text: str, *, replacing: bool
    ) -> dict[str, Any]:
        """End a startup that failed before it reached the sandbox provider."""
        current = await ownership.require_current()
        with contextlib.suppress(BaseException):
            await self._runtime_subjects.converge_startup_failure(
                session=current,
                failure_phase="startup_precondition_failed",
                created_sandbox_id=None,
                cleanup=None,
            )
        latest = await self._mark_failed(
            ownership,
            replacing=replacing, error_text=error_text, leaked_sandbox_id=None, user_action=None
        )
        if not isinstance(latest, dict):
            raise RuntimeError(error_text)
        return latest

    async def _mark_failed(
        self,
        ownership: RecoveryOwnership,
        *,
        error_text: str,
        leaked_sandbox_id: str | None,
        user_action: dict[str, Any] | None,
        replacing: bool,
    ) -> dict[str, Any] | None:
        current = await self._sessions.get_session(ownership.session_id)
        if not current or str(current.get("state") or "") != SessionState.CREATING.value:
            return current
        # A lost box never ends a conversation: a failed replacement leaves it
        # READY, and the reader fails the messages that were waiting for it.
        stays_open = bool(user_action) or replacing
        updates: dict[str, Any] = {
            "state": SessionState.READY.value if stays_open else SessionState.TERMINATED.value,
            # A message sent while this is set is answered with the action.
            "startup_user_action": user_action,
            "last_error": error_text,
            "runtime_unavailable": True,
            "startup_progress": None,
        }
        leaked = str(leaked_sandbox_id or "").strip()
        if leaked:
            # The pointer alone does not keep this box findable: TERMINATED
            # puts the row outside the dead-binding probe, which skips ended
            # sessions. The ledger records that this deployment created the
            # box and never confirmed it gone, whatever the row's state.
            updates["sandbox_id"] = leaked
            known = read_undestroyed(current)
            if leaked not in known:
                updates[UNDESTROYED_SANDBOX_IDS] = sorted({*known, leaked})
        if replacing and not user_action:
            updates["runtime_start_failed_at"] = utcnow_iso()
        await ownership.update(updates, notify="reader")
        latest = await self._sessions.get_session(ownership.session_id)
        return latest if isinstance(latest, dict) else {**current, **updates}


async def runtime_initialization_metadata(session_id: str, runtime: Any) -> dict[str, Any]:
    """The verified engine manifest and the optional engine init snapshot.

    The manifest is construction-time evidence and is stored with the session,
    so a host restart does not erase the operation vocabulary the page shows.
    Server info adds slash commands, skills and the model, when the engine
    declares it. The caller passes the runtime it just constructed; reading
    the process-local runtime map again would be a second, racy authority.
    """
    try:
        manifest = bound_engine_client_manifest(runtime)
    except TypeError as exc:
        raise APIError(
            code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
            message=(
                "runtime startup completed without a verified engine manifest "
                f"(session={session_id}): {exc}"
            ),
            status_code=502,
        ) from exc
    observation: dict[str, Any] = {
        "observed_at": utcnow_iso(),
        "sandbox_id": runtime.sandbox_id,
        "runtime_generation": None,
        "components": [],
        "reason": "server_info_not_supported",
    }
    metadata: dict[str, Any] = {"engine_capabilities": asdict(manifest), "runtime_versions": observation}
    if not manifest.supports_server_info:
        return metadata
    observation["reason"] = "engine_versions_not_reported"
    info_fn = getattr(getattr(runtime, "engine_client", None), "get_server_info", None)
    if not callable(info_fn):
        raise APIError(
            code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
            message=(
                "runtime declared server-info capability without a callable "
                f"implementation (session={session_id})"
            ),
            status_code=502,
        )
    try:
        info = await info_fn()
    except Exception as exc:
        logger.warning(
            "startup initialization metadata fetch failed session=%s err=%s", session_id, exc
        )
        info = None
        observation["reason"] = "server_info_unavailable"
    if isinstance(info, dict):
        metadata.update(extract_agent_session_metadata(info))
        versions = info.get("runtime_versions")
        if isinstance(versions, dict) and versions:
            components = versions.get("components")
            generation = versions.get("runtime_generation")
            if (
                isinstance(generation, str) and generation
                and isinstance(components, list) and components
                and all(isinstance(item, dict)
                        and isinstance(item.get("component"), str) and item["component"]
                        and isinstance(item.get("source"), str) and item["source"]
                        and (isinstance(item.get("version"), str) and item["version"]
                             or item.get("version") is None and isinstance(item.get("reason"), str) and item["reason"])
                        for item in components)
            ):
                observation.update(
                    runtime_generation=generation, reason=None,
                    components=[{key: item.get(key) for key in ("component", "version", "source", "reason")}
                                for item in components],
                )
            else:
                observation["reason"] = "invalid_engine_version_observation"
                logger.warning("invalid engine version observation session=%s", session_id)
    return metadata


async def _settled(inner: asyncio.Future[Any], session_id: str) -> Any:
    """The runtime ``inner`` produces, even when this startup is cancelled meanwhile.

    The sandbox operation may already hold a box. Abandoning it on a
    cancellation would leave that box with no owner, so the cancellation is
    absorbed and the operation is waited out; the startup then settles the
    session as usual.
    """
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        logger.warning(
            "session startup absorbed external cancel; waiting for runtime creation: session=%s",
            session_id,
        )
        return await inner


class _StartupProgress:
    """Advisory ``startup_progress`` writes, overlapped with provisioning.

    Each new label is written by its own task, so a slow write never holds up
    the sandbox work; :meth:`drain` waits for them before the startup settles,
    so none lands after the ``READY`` write and brings a label back.
    """

    def __init__(self, ownership: RecoveryOwnership) -> None:
        self._ownership = ownership
        self._last = ""
        self._tasks: list[asyncio.Task[None]] = []

    async def emit(self, progress: str) -> None:
        label = str(progress or "").strip()
        if not label or label == self._last:
            return
        self._last = label
        self._tasks.append(asyncio.create_task(self._write(label)))

    async def _write(self, label: str) -> None:
        try:
            await self._ownership.update({"startup_progress": label})
        except Exception as exc:
            logger.warning(
                "startup progress write failed session=%s progress=%s err=%s",
                self._ownership.session_id,
                label,
                exc,
            )

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()


def _failure_text(exc: Exception, fallback: str) -> str:
    if isinstance(exc, APIError):
        return str(exc.message or "").strip() or fallback
    message = str(exc).strip()
    return f"{fallback}: {message}" if message else fallback
