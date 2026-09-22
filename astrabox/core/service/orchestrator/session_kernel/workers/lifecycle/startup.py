"""Session-startup / sandbox-provisioning orchestration.

Everything that turns a ``StartSessionStartup`` command into a READY Session:
loading the template, recording progress, acquiring the runtime subject,
executing its typed create/attach/reuse action, absorbing cancellation while a
sandbox operation settles, publishing owner readiness, and writing the final
Session state. Owner-specific lifecycle decisions stay behind the
runtime-subject provider.

The ``_run_session_startup_direct`` progress trio
(``_do_progress``/``_emit_progress``/``_drain_progress``) and the
``_run_startup_command`` record trio
(``_record_progress``/``_record_ready``/``_record_failed``) keep their state
on explicit holders, ``_StartupProgressState`` / ``_StartupEventCursor``
(``state.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from typing import Any

from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.chunk_processing import (
    extract_agent_session_metadata,
)
from astrabox.core.service.orchestrator.engine.base import (
    bound_engine_client_manifest,
)
from astrabox.core.service.orchestrator.sandbox_names import (
    UNDESTROYED_SANDBOX_IDS,
    keep_name_updates,
    read_undestroyed,
)
from astrabox.core.service.orchestrator.runtime_subject import RuntimeStartupCleanup
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    CALLBACK_SUBJECT_SESSION,
    build_sandbox_callback_url_from_record,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import RecoveryOwnership
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.state import (
    _StartupEventCursor,
    _StartupProgressState,
)
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)

logger = get_logger(__name__)


class _StartupOrchestrationMixin:
    """Session-startup / sandbox-provisioning orchestration mixin for
    :class:`SessionLifecycleWorker`."""

    async def _run_startup_command(
        self,
        *,
        wakeup: WorkerWakeup,
        command_event: dict[str, Any],
        payload: dict[str, Any],
    ) -> WorkerOutcome:
        session = await self._run_session_repo_op_with_retry(
            lambda: self._sessions_repo.get_session(wakeup.session_id),
            operation="startup:get_session",
            session_id=wakeup.session_id,
        )
        if not isinstance(session, dict):
            raise RuntimeError(f"missing session {wakeup.session_id}")

        # Display/grouping label only; the runtime configuration resolves by
        # identity (agent_id / workspace_ref) via resolve_session_harness below.
        recovery_owner = str(payload.get("recovery_owner") or "").strip() or None
        generation = str(payload.get("sandbox_generation") or "").strip()
        if not generation:
            raise RuntimeError("startup command has no sandbox generation")
        ownership = RecoveryOwnership(
            self._sessions_repo,
            wakeup.session_id,
            recovery_owner,
            sandbox_generation=generation,
            assignment_id=str(command_event.get("causation_id") or "").strip() or None,
        )
        await ownership.require_current()

        template_name = str(
            payload.get("template_name")
            or session.get("template_name")
            or ""
        ).strip()

        user = self._build_user_context(session, payload)
        correlation_id = str(command_event.get("correlation_id") or "").strip() or None
        causation_id = str(command_event.get("causation_id") or "").strip() or None
        permission_mode = str(
            payload.get("permission_mode")
            or session.get("permission_mode")
            or ""
        ).strip() or None

        try:
            template = await self._run_startup_mongo_op_with_retry(
                session_id=wakeup.session_id,
                operation="startup:resolve_harness",
                op=lambda: self._session_service._agent_config.resolve_session_harness(session),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._finalize_startup_failure(
                ownership=ownership,
                session_id=wakeup.session_id,
                template_name=template_name,
                permission_mode=permission_mode,
                causation_id=causation_id,
                correlation_id=correlation_id,
                error_text=self._startup_failure_error_text(
                    exc,
                    fallback_message="failed to load startup template",
                ),
                leaked_sandbox_id=None,
            )
        if template is None:
            return await self._finalize_startup_failure(
                ownership=ownership,
                session_id=wakeup.session_id,
                template_name=template_name,
                permission_mode=permission_mode,
                causation_id=causation_id,
                correlation_id=correlation_id,
                error_text=f"template '{template_name}' not found in whitelist",
                leaked_sandbox_id=None,
            )
        cursor = _StartupEventCursor(
            last_event_seq=int(command_event.get("event_seq") or 0)
        )

        async def _record_progress(progress: str) -> None:
            latest = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(wakeup.session_id),
                operation="startup:progress_get_session",
                session_id=wakeup.session_id,
            )
            event = await self._session_events_repo.append_event(
                {
                    "session_id": wakeup.session_id,
                    "channel": self.channel,
                    "event_type": "session.startup_progressed",
                    "causation_id": causation_id,
                    "correlation_id": correlation_id,
                    "payload": {
                        "command_type": "StartSessionStartup",
                        "template_name": template_name,
                        "progress": progress,
                    },
                }
            )
            cursor.last_event_seq = int(event.get("event_seq") or cursor.last_event_seq)
            await self._project_snapshot(
                session_id=wakeup.session_id,
                event_seq=cursor.last_event_seq,
                session=latest or session,
                fallback_permission_mode=permission_mode,
            )

        async def _record_ready(latest_session: dict[str, Any]) -> None:
            event = await self._project_startup_terminal_event_with_retry(
                session_id=wakeup.session_id,
                event_type="session.startup_completed",
                causation_id=causation_id,
                correlation_id=correlation_id,
                payload={
                    "command_type": "StartSessionStartup",
                    "template_name": template_name,
                    "state": str(latest_session.get("state") or "").strip() or None,
                    "sandbox_id": str(latest_session.get("sandbox_id") or "").strip() or None,
                    "permission_mode": str(
                        latest_session.get("permission_mode") or permission_mode or ""
                    ).strip() or None,
                },
                session=latest_session,
                fallback_permission_mode=permission_mode,
            )
            cursor.last_event_seq = int(event.get("event_seq") or cursor.last_event_seq)

        async def _record_failed(
            latest_session: dict[str, Any],
            error_text: str,
            leaked_sandbox_id: str | None,
        ) -> None:
            event = await self._project_startup_terminal_event_with_retry(
                session_id=wakeup.session_id,
                event_type="session.startup_failed",
                causation_id=causation_id,
                correlation_id=correlation_id,
                payload={
                    "command_type": "StartSessionStartup",
                    "template_name": template_name,
                    "permission_mode": permission_mode,
                    "error_text": error_text,
                    "leaked_sandbox_id": leaked_sandbox_id,
                    "state": str(latest_session.get("state") or "").strip() or None,
                },
                session=latest_session,
                fallback_permission_mode=permission_mode,
            )
            cursor.last_event_seq = int(event.get("event_seq") or cursor.last_event_seq)

        with ownership.bind() if ownership is not None else contextlib.nullcontext():
            result = await self._run_session_startup_direct(
                session_id=wakeup.session_id,
                assignment_id=str(causation_id or ""),
                ownership=ownership,
                template=template,
                user_id=user.user_id,
                permission_mode=permission_mode,
                resume_session_id=str(payload.get("resume_session_id") or "").strip() or None,
                on_progress=_record_progress,
                on_ready=_record_ready,
                on_failed=_record_failed,
            )
        latest = result.get("session") if isinstance(result, dict) else None
        if not isinstance(latest, dict):
            latest = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(wakeup.session_id),
                operation="startup:get_latest_session",
                session_id=wakeup.session_id,
            )
        if not isinstance(latest, dict):
            raise RuntimeError(f"missing startup session {wakeup.session_id}")
        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=cursor.last_event_seq,
            metadata={
                "command_type": "StartSessionStartup",
                "result": dict(latest),
                "startup_status": str((result or {}).get("status") or "").strip() or None,
            },
        )

    async def _run_session_startup_direct(
        self,
        *,
        session_id: str,
        assignment_id: str,
        template: Any,
        user_id: str,
        permission_mode: str | None,
        resume_session_id: str | None,
        on_progress,
        on_ready,
        on_failed,
        ownership: RecoveryOwnership | None = None,
    ) -> dict[str, Any]:
        assignment = str(assignment_id or "").strip()
        if not assignment:
            raise APIError(
                code="SANDBOX_ASSIGNMENT_INVALID",
                message="session startup command has no durable assignment id",
                status_code=500,
            )
        progress_state = _StartupProgressState()
        target = None

        async def _do_progress(normalized: str) -> None:
            # startup_progress + the journal/snapshot projection are advisory UI hints, not a
            # truth-owner — a plain update (no read-back verify) is enough, and any failure is
            # logged, never fatal to startup.
            try:
                await self._run_session_repo_op_with_retry(
                    lambda: (ownership.update({"startup_progress": normalized}) if ownership is not None
                             else self._sessions_repo.update_session(session_id, {"startup_progress": normalized})),
                    operation="startup:progress_update",
                    session_id=session_id,
                )
                if on_progress is not None:
                    await on_progress(normalized)
            except Exception as exc:  # noqa: BLE001 — advisory progress, never fail startup
                logger.warning(
                    "startup progress write failed session=%s progress=%s err=%s",
                    session_id, normalized, exc,
                )

        async def _emit_progress(progress: str) -> None:
            normalized = str(progress or "").strip()
            if not normalized or normalized == progress_state.last_progress:
                return
            progress_state.last_progress = normalized
            # Fire-and-forget: progress writes (~0.3-0.5s each of blocking Mongo) overlap
            # provision + runner launch off the critical path. They are drained right before the
            # READY settle so a late one can never clobber startup_progress=None on a READY row.
            progress_state.progress_tasks.append(
                asyncio.create_task(_do_progress(normalized))
            )

        async def _drain_progress() -> None:
            if progress_state.progress_tasks:
                await asyncio.gather(
                    *progress_state.progress_tasks, return_exceptions=True
                )
                progress_state.progress_tasks.clear()

        async def _cleanup_and_converge_runtime_subject_failure(
            failure_phase: str,
            created_sandbox_id: str | None = None,
        ) -> RuntimeStartupCleanup:
            if ownership is not None:
                await ownership.require_current()
            cleanup = await self._runtime_subjects.cleanup_failed_startup_runtime(
                session_id=session_id,
                target=target,
                sandbox_id=created_sandbox_id,
            )
            resolved_created_sandbox_id = (
                str(created_sandbox_id or "").strip()
                if target is not None and target.action == "create_runtime"
                else ""
            )
            current = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(session_id),
                operation="startup:subject_failure_get_session",
                session_id=session_id,
            )
            if not isinstance(current, dict):
                return cleanup
            with contextlib.suppress(BaseException):
                await self._runtime_subjects.converge_startup_failure(
                    session=target.session if target is not None else current,
                    failure_phase=failure_phase,
                    created_sandbox_id=resolved_created_sandbox_id or None,
                    cleanup=cleanup.destruction,
                )
            return cleanup

        try:
            if ownership is not None:
                await ownership.require_current()
            target = await self._runtime_subjects.acquire_startup(
                session_id=session_id,
                template=template,
                sandbox_generation=(
                    ownership.sandbox_generation if ownership is not None else None
                ),
                resume_engine_session_key=resume_session_id,
                on_progress=_emit_progress,
            )
            active_session = target.session
            workspace_plan = target.workspace_plan

            if target.action == "use_ready_binding":
                sandbox_id = str(workspace_plan.sandbox_id or "").strip()
                if not sandbox_id:
                    raise APIError(
                        code="RUNTIME_SUBJECT_INVALID",
                        message="A ready runtime binding has no sandbox_id",
                        status_code=500,
                    )
                startup_updates: dict[str, Any] = {
                    "state": SessionState.READY.value,
                    "sandbox_id": sandbox_id,
                    "expires_at": target.binding_expires_at,
                    "runtime_unavailable": False,
                    "last_error": None,
                    "startup_progress": None,
                    "model_name": self._runtime_manager.resolve_template_model_name(template),
                    "terminal_cwd": workspace_plan.cwd,
                }
                await _drain_progress()
                latest = await self._update_session_with_settle_retry(
                    ownership=ownership,
                    session_id=session_id,
                    updates=startup_updates,
                    expected_fields={
                        "state": SessionState.READY.value,
                        "sandbox_id": sandbox_id,
                        "startup_progress": None,
                        "runtime_unavailable": False,
                    },
                )
                ready_session = latest or {**active_session, **startup_updates}
                logger.info(
                    "session startup reused a prepared runtime binding: "
                    "session=%s subject=%s sandbox=%s",
                    session_id,
                    workspace_plan.subject_kind,
                    sandbox_id,
                )
                if on_ready is not None:
                    await on_ready(ready_session)
                return {
                    "status": "ready",
                    "session": ready_session,
                    "error_text": None,
                    "leaked_sandbox_id": None,
                }

            if ownership is not None:
                await ownership.require_current()
            if target.action == "create_runtime":
                await _emit_progress("creating_sandbox")
                inner = asyncio.ensure_future(
                    self._runtime_manager.create_runtime(
                        session_id,
                        template,
                        assignment_id=assignment,
                        **({"startup_guard": ownership.require_current} if ownership is not None else {}),
                        user_id=user_id,
                        permission_mode=permission_mode,
                        progress_callback=_emit_progress,
                        callback_url=build_sandbox_callback_url_from_record(
                            subject_type=CALLBACK_SUBJECT_SESSION,
                            subject_id=session_id,
                            record=active_session,
                        ),
                        workspace_plan=workspace_plan,
                    )
                )
            elif target.action == "attach_runtime":
                inner = asyncio.ensure_future(
                    self._runtime_manager.ensure_runtime(
                        session_id,
                        template,
                        sandbox_id=str(workspace_plan.sandbox_id or "").strip(),
                        **({"startup_guard": ownership.require_current} if ownership is not None else {}),
                        user_id=user_id,
                        permission_mode=permission_mode,
                        session_kind=workspace_plan.session_kind,
                        workspace_plan=workspace_plan,
                    )
                )
            else:
                raise APIError(
                    code="RUNTIME_SUBJECT_INVALID",
                    message=f"Unknown runtime startup action: {target.action!r}",
                    status_code=500,
                )
            try:
                runtime = await asyncio.shield(inner)
            except asyncio.CancelledError as cancel_exc:
                if str(cancel_exc) == "explicit":
                    inner.cancel()
                    with contextlib.suppress(Exception):
                        await inner
                    raise
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()
                logger.warning(
                    "session startup absorbed external cancel; waiting for runtime "
                    "creation: session=%s",
                    session_id,
                )
                runtime = await inner
        except APIError as exc:
            created_sandbox_id = ""
            if isinstance(exc.data, dict):
                created_sandbox_id = str(
                    exc.data.get("sandbox_id")
                    or exc.data.get("leaked_sandbox_id")
                    or ""
                ).strip()
            cleanup = await _cleanup_and_converge_runtime_subject_failure(
                failure_phase="runtime_start_failed",
                created_sandbox_id=created_sandbox_id or None,
            )
            leaked_sandbox_id = cleanup.leaked_sandbox_id
            latest = await self._mark_session_start_failed_direct(
                ownership=ownership,
                session_id=session_id,
                error_message=exc.message,
                leaked_sandbox_id=leaked_sandbox_id,
            )
            if isinstance(latest, dict) and on_failed is not None:
                await on_failed(latest, exc.message, leaked_sandbox_id)
            return {
                "status": "failed",
                "session": latest or {"session_id": session_id},
                "error_text": exc.message,
                "leaked_sandbox_id": leaked_sandbox_id,
            }
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error_text = f"failed to start remote-agent runtime: {exc}"
            cleanup = await _cleanup_and_converge_runtime_subject_failure(
                failure_phase="runtime_start_failed",
            )
            leaked_sandbox_id = cleanup.leaked_sandbox_id
            try:
                latest = await self._mark_session_start_failed_direct(
                    ownership=ownership,
                    session_id=session_id,
                    error_message=error_text,
                    leaked_sandbox_id=leaked_sandbox_id,
                )
            except BaseException:
                # The failure-marking write is the last line between a failed
                # startup and a session left in CREATING with no error. If it
                # also fails, leave a loud trail instead of a silent task death.
                logger.critical(
                    "session start-failed marking itself failed — session may be "
                    "stuck CREATING: session=%s original_error=%s",
                    session_id,
                    error_text,
                    exc_info=True,
                )
                latest = None
            if isinstance(latest, dict) and on_failed is not None:
                await on_failed(latest, error_text, leaked_sandbox_id)
            return {
                "status": "failed",
                "session": latest or {"session_id": session_id},
                "error_text": error_text,
                "leaked_sandbox_id": leaked_sandbox_id,
            }

        if ownership is not None:
            await ownership.require_current()
        current = await self._run_session_repo_op_with_retry(
            lambda: self._sessions_repo.get_session(session_id)
        )
        current_state = str((current or {}).get("state") or "")
        if current_state not in {
            SessionState.CREATING.value,
            SessionState.READY.value,
        }:
            aborted_sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip() or None
            cleanup = await _cleanup_and_converge_runtime_subject_failure(
                failure_phase="startup_aborted",
                created_sandbox_id=aborted_sandbox_id,
            )
            # Only a create action owns an allocation to release. An attach
            # action points at a longer-lived subject runtime and cleanup
            # returns no destruction verdict for this Session.
            keep = keep_name_updates(cleanup.destruction, row=current)
            if keep:
                logger.error(
                    "aborted startup could not confirm the destruction of "
                    "session=%s sandbox=%s: %s",
                    session_id,
                    aborted_sandbox_id,
                    (
                        cleanup.destruction.detail
                        if cleanup.destruction is not None
                        else "cleanup produced no verdict"
                    ),
                )
                with contextlib.suppress(Exception):
                    await ownership.update(keep) if ownership is not None else await self._sessions_repo.update_session(session_id, keep)
            latest = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(session_id),
                operation="startup:get_aborted_session",
                session_id=session_id,
            )
            return {
                "status": "aborted",
                "session": latest or current or {"session_id": session_id},
                "error_text": None,
                "leaked_sandbox_id": cleanup.leaked_sandbox_id,
            }

        real_expires_at = None
        sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip() or None
        if not sandbox_id:
            error_text = "runtime startup returned no sandbox_id"
            cleanup = await _cleanup_and_converge_runtime_subject_failure(
                failure_phase="ready_precondition_failed",
            )
            leaked_sandbox_id = cleanup.leaked_sandbox_id
            latest = await self._mark_session_start_failed_direct(
                ownership=ownership,
                session_id=session_id,
                error_message=error_text,
                leaked_sandbox_id=leaked_sandbox_id,
            )
            if isinstance(latest, dict) and on_failed is not None:
                await on_failed(latest, error_text, leaked_sandbox_id)
            return {
                "status": "failed",
                "session": latest or {"session_id": session_id},
                "error_text": error_text,
                "leaked_sandbox_id": leaked_sandbox_id,
            }
        if target.renewal_ttl_seconds is not None:
            try:
                await self._runtime_manager.renew_sandbox_by_id(
                    sandbox_id,
                    ttl_seconds=target.renewal_ttl_seconds,
                )
            except Exception as exc:
                cleanup = await _cleanup_and_converge_runtime_subject_failure(
                    failure_phase="renew_failed",
                    created_sandbox_id=sandbox_id,
                )
                leaked_sandbox_id = cleanup.leaked_sandbox_id
                error_text = (
                    "failed to renew runtime-subject sandbox "
                    f"session={session_id} sandbox={sandbox_id}: {exc}"
                )
                latest = await self._mark_session_start_failed_direct(
                    ownership=ownership,
                    session_id=session_id,
                    error_message=error_text,
                    leaked_sandbox_id=leaked_sandbox_id,
                )
                if isinstance(latest, dict) and on_failed is not None:
                    await on_failed(latest, error_text, leaked_sandbox_id)
                return {
                    "status": "failed",
                    "session": latest or {"session_id": session_id},
                    "error_text": error_text,
                    "leaked_sandbox_id": leaked_sandbox_id,
                }
        try:
            real_expires_at = await self._runtime_manager.get_sandbox_expires_at(sandbox_id)
            expires_at_iso = (
                real_expires_at.isoformat() if real_expires_at is not None else None
            )
            await self._runtime_subjects.publish_runtime_ready(
                session=active_session,
                sandbox_id=sandbox_id,
                expires_at=expires_at_iso,
                runtime_identity=getattr(runtime, "runtime_identity", None),
            )
        except Exception as exc:
            cleanup = await _cleanup_and_converge_runtime_subject_failure(
                failure_phase="ready_publish_failed",
                created_sandbox_id=sandbox_id,
            )
            leaked_sandbox_id = cleanup.leaked_sandbox_id
            error_text = f"failed to publish runtime subject readiness: {exc}"
            latest = await self._mark_session_start_failed_direct(
                ownership=ownership,
                session_id=session_id,
                error_message=error_text,
                leaked_sandbox_id=leaked_sandbox_id,
            )
            if isinstance(latest, dict) and on_failed is not None:
                await on_failed(latest, error_text, leaked_sandbox_id)
            return {
                "status": "failed",
                "session": latest or {"session_id": session_id},
                "error_text": error_text,
                "leaked_sandbox_id": leaked_sandbox_id,
            }
        startup_updates: dict[str, Any] = {
            "state": SessionState.READY.value,
            "sandbox_id": sandbox_id,
            "startup_allocation": None,
            "expires_at": expires_at_iso,
            "runtime_unavailable": False,
            "last_error": None,
            "startup_progress": None,
            "model_name": self._runtime_manager.resolve_template_model_name(template),
        }
        terminal_cwd = str(getattr(runtime, "terminal_cwd", "") or "").strip()
        if terminal_cwd:
            startup_updates["terminal_cwd"] = terminal_cwd
        runtime_engine_session_key = str(getattr(runtime, "engine_session_key", "") or "").strip() or None
        if resume_session_id or runtime_engine_session_key:
            startup_updates["engine_session_key"] = runtime_engine_session_key or resume_session_id
        startup_runtime_identity = getattr(runtime, "runtime_identity", None)
        if target.persist_runtime_identity_on_session and startup_runtime_identity:
            # A Session-owned subject carries its recovery identity on the Session.
            # Shared subjects publish owner identity through their provider instead.
            startup_updates["runtime_identity"] = startup_runtime_identity
        startup_updates.update(
            await self._fetch_runtime_initialization_metadata(session_id, runtime)
        )
        # Drain the fire-and-forget progress writes before the terminal READY settle so none can
        # land after it and clobber startup_progress=None. They overlapped provision+runner-launch,
        # so this is typically already complete (~free).
        await _drain_progress()
        latest = await self._update_session_with_settle_retry(
            ownership=ownership,
            session_id=session_id,
            updates=startup_updates,
            expected_fields={
                "state": SessionState.READY.value,
                "startup_progress": None,
                "runtime_unavailable": False,
                "startup_allocation": None,
            },
        )
        self._runtime_manager.forget_published_startup_allocation(
            session_id,
            sandbox_id=sandbox_id,
        )
        if isinstance(latest, dict) and on_ready is not None:
            await on_ready(latest)
        return {
            "status": "ready",
            "session": latest or {
                "session_id": session_id,
                **startup_updates,
            },
            "error_text": None,
            "leaked_sandbox_id": None,
        }

    async def _mark_session_start_failed_direct(
        self,
        *,
        session_id: str,
        error_message: str,
        leaked_sandbox_id: str | None,
        ownership: RecoveryOwnership | None = None,
    ) -> dict[str, Any] | None:
        current = await self._run_session_repo_op_with_retry(
            lambda: self._sessions_repo.get_session(session_id)
        )
        if not current or str(current.get("state") or "") != SessionState.CREATING.value:
            return current
        updates: dict[str, Any] = {
            "state": SessionState.TERMINATED.value,
            "last_error": error_message,
            "runtime_unavailable": True,
            "startup_progress": None,
        }
        leaked = str(leaked_sandbox_id or "").strip()
        if leaked:
            # Two writes, because `sandbox_id` alone is not enough to keep this
            # box findable. TERMINATED puts the row outside
            # `list_dead_binding_probe_candidates` (its query excludes terminal
            # states), so the pointer would name a box nothing ever probes
            # again. The ledger survives that: it records "this deployment
            # created this box and never confirmed it gone", independent of
            # what state the row is in or where its pointer later moves.
            updates["sandbox_id"] = leaked
            known = read_undestroyed(current)
            if leaked not in known:
                updates[UNDESTROYED_SANDBOX_IDS] = sorted({*known, leaked})
        latest = await self._update_session_with_settle_retry(
            session_id=session_id,
            updates=updates,
            ownership=ownership,
            expected_fields={
                "state": SessionState.TERMINATED.value,
                "runtime_unavailable": True,
                "startup_progress": None,
            },
        )
        return latest or {**current, **updates}

    @staticmethod
    def _startup_failure_error_text(
        exc: Exception,
        *,
        fallback_message: str,
    ) -> str:
        if isinstance(exc, APIError):
            message = str(exc.message or "").strip()
            return message or fallback_message
        if is_mongo_transient_error(exc):
            return "mongodb timeout/unavailable, please retry"
        message = str(exc).strip()
        if not message:
            return fallback_message
        return f"{fallback_message}: {message}"

    async def _finalize_startup_failure(
        self,
        *,
        session_id: str,
        template_name: str,
        permission_mode: str | None,
        causation_id: str | None,
        correlation_id: str | None,
        error_text: str,
        leaked_sandbox_id: str | None,
        ownership: RecoveryOwnership | None = None,
    ) -> WorkerOutcome:
        if ownership is not None:
            await ownership.require_current()
        with contextlib.suppress(BaseException):
            current = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(session_id),
                operation="startup:precondition_failure_get_session",
                session_id=session_id,
            )
            if isinstance(current, dict):
                await self._runtime_subjects.converge_startup_failure(
                    session=current,
                    failure_phase="startup_precondition_failed",
                    created_sandbox_id=None,
                    cleanup=None,
                )
        latest = await self._mark_session_start_failed_direct(
            session_id=session_id,
            error_message=error_text,
            leaked_sandbox_id=leaked_sandbox_id,
            ownership=ownership,
        )
        if not isinstance(latest, dict):
            latest = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(session_id),
                operation="startup:get_failed_session",
                session_id=session_id,
            )
        if not isinstance(latest, dict):
            raise RuntimeError(error_text)
        event = await self._project_startup_terminal_event_with_retry(
            session_id=session_id,
            event_type="session.startup_failed",
            causation_id=causation_id,
            correlation_id=correlation_id,
            payload={
                "command_type": "StartSessionStartup",
                "template_name": template_name,
                "permission_mode": permission_mode,
                "error_text": error_text,
                "leaked_sandbox_id": leaked_sandbox_id,
                "state": str(latest.get("state") or "").strip() or None,
            },
            session=latest,
            fallback_permission_mode=permission_mode,
        )
        event_seq = int(event.get("event_seq") or 0)
        return WorkerOutcome(
            session_id=session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=event_seq,
            metadata={
                "command_type": "StartSessionStartup",
                "result": dict(latest),
                "startup_status": "failed",
            },
        )

    async def _find_existing_startup_terminal_event(
        self,
        *,
        session_id: str,
        event_type: str,
        causation_id: str | None,
    ) -> dict[str, Any] | None:
        if not causation_id:
            return None
        events = await self._session_events_repo.list_events(
            session_id,
            channel=self.channel,
            causation_id=causation_id,
            limit=50,
        )
        for event in events:
            if str(event.get("event_type") or "").strip() == event_type:
                return event
        return None

    async def _append_startup_terminal_event_once(
        self,
        *,
        session_id: str,
        event_type: str,
        causation_id: str | None,
        correlation_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = await self._find_existing_startup_terminal_event(
            session_id=session_id,
            event_type=event_type,
            causation_id=causation_id,
        )
        if isinstance(existing, dict):
            return existing
        return await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": self.channel,
                "event_type": event_type,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "payload": dict(payload),
            }
        )

    async def _project_startup_terminal_event_with_retry(
        self,
        *,
        session_id: str,
        event_type: str,
        causation_id: str | None,
        correlation_id: str | None,
        payload: dict[str, Any],
        session: dict[str, Any],
        fallback_permission_mode: str | None,
    ) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            event = await self._append_startup_terminal_event_once(
                session_id=session_id,
                event_type=event_type,
                causation_id=causation_id,
                correlation_id=correlation_id,
                payload=payload,
            )
            event_seq = int(event.get("event_seq") or 0)
            await self._project_snapshot(
                session_id=session_id,
                event_seq=event_seq,
                session=session,
                fallback_permission_mode=fallback_permission_mode,
            )
            return event

        return await self._run_startup_mongo_op_with_retry(
            session_id=session_id,
            operation=f"startup:project_terminal:{event_type}",
            op=_run,
        )

    async def _fetch_runtime_initialization_metadata(
        self,
        session_id: str,
        runtime: Any,
    ) -> dict[str, Any]:
        """Fetch the verified manifest and optional engine init snapshot.

        The manifest is construction-time evidence and is persisted with the
        Session so a host restart does not erase the UI's operation vocabulary.
        Optional server info adds slash commands, skills, and model metadata.
        The caller passes the runtime it just constructed; consulting the
        process-local live map again would create a second, racy authority.
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
        metadata = {"engine_capabilities": asdict(manifest)}
        if not manifest.supports_server_info:
            return metadata
        engine_client = getattr(runtime, "engine_client", None)
        info_fn = getattr(engine_client, "get_server_info", None)
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
                "startup initialization metadata fetch failed session=%s err=%s",
                session_id,
                exc,
            )
            info = None
        if isinstance(info, dict):
            metadata.update(extract_agent_session_metadata(info))
        return metadata
