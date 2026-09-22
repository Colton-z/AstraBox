from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.workers.base import KernelWorkerBase
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.runtime.terminal_execution import (
    is_isolated_terminal_execution_id,
)

logger = get_logger(__name__)

_ACTIVE_TERMINAL_STATES = {"RUNNING", "INTERRUPTING"}


class TerminalWorker(KernelWorkerBase):
    channel = "terminal"
    _TERMINAL_SETTLE_MAX_ATTEMPTS = 40
    _TERMINAL_SETTLE_DELAY_S = 0.25
    _EXECUTION_BIND_RETRY_MAX_ATTEMPTS = 20
    _EXECUTION_BIND_RETRY_DELAY_S = 0.1

    def __init__(
        self,
        *,
        worker_id: str,
        sessions_repo: Any,
        terminal_service: Any,
        runtime_manager: Any,
        broker: Any,
        artifacts_repo: Any,
        session_events_repo: Any,
        session_snapshots_repo: Any,
    ) -> None:
        super().__init__(worker_id=worker_id)
        self._sessions_repo = sessions_repo
        self._terminal_service = terminal_service
        self._runtime_manager = runtime_manager
        self._broker = broker
        self._artifacts_repo = artifacts_repo
        self._session_events_repo = session_events_repo
        self._session_snapshots_repo = session_snapshots_repo

    async def run_once(
        self,
        wakeup: WorkerWakeup,
    ) -> WorkerOutcome:
        command_event = await self._session_events_repo.get_command_event(
            wakeup.session_id,
            command_id=wakeup.command_id,
        )
        if not isinstance(command_event, dict):
            raise RuntimeError(
                f"missing command.accepted event session_id={wakeup.session_id} command_id={wakeup.command_id}"
            )

        session = await self._sessions_repo.get_session(wakeup.session_id)
        if not isinstance(session, dict):
            raise RuntimeError(f"missing session {wakeup.session_id}")

        payload = command_event.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"command.accepted payload missing session_id={wakeup.session_id} command_id={wakeup.command_id}"
            )

        user = self._build_user_context(session, payload)
        command_id = str(command_event.get("causation_id") or "").strip()
        correlation_id = str(command_event.get("correlation_id") or "").strip() or command_id
        command_type = str(payload.get("command_type") or "").strip()
        command = str(payload.get("command") or "")
        cwd = str(payload.get("cwd") or "").strip() or None
        if command_type == "InterruptTerminal":
            result = await self._interrupt_terminal_command(
                wakeup=wakeup,
                command_event=command_event,
                session=session,
            )
            return WorkerOutcome(
                session_id=wakeup.session_id,
                channel=self.channel,
                status="idle",
                processed_event_seq=int(result.get("event_seq") or command_event.get("event_seq") or 0),
                metadata={
                    "command_type": command_type,
                    "result": result,
                },
            )

        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        producer_cancelled = False
        producer_error: Exception | None = None
        worker_status = "idle"
        worker_error_text: str | None = None
        terminal_seq = 0
        started = False
        saw_exit = False
        last_event_seq = int(command_event.get("event_seq") or 0)
        last_command_event_seq = int(command_event.get("event_seq") or 0)
        interrupt_requested = False
        bound_execution_id: str | None = None

        async def _publish_broker_event(event: dict[str, Any]) -> None:
            try:
                await self._broker.publish(wakeup.session_id, event)
            except Exception as exc:
                logger.warning(
                    "session kernel terminal broker publish failed session=%s event_type=%s err=%s",
                    wakeup.session_id,
                    str(event.get("type") or "<unknown>"),
                    exc,
                )

        async def _append_terminal_artifact(event: dict[str, Any]) -> None:
            nonlocal terminal_seq
            stored = await self._artifacts_repo.append_terminal_event(
                {
                    "session_id": wakeup.session_id,
                    "command_id": command_id,
                    "terminal_seq": terminal_seq,
                    "turn_id": None,
                    "payload": dict(event),
                    "command": command,
                    "cwd": cwd,
                    "created_at": utcnow_iso(),
                }
            )
            await _publish_broker_event(
                {
                    "type": "terminal_event",
                    "command_id": command_id,
                    "terminal_seq": int(stored.get("terminal_seq") or terminal_seq),
                }
            )
            terminal_seq += 1

        async def _append_terminal_event(event_type: str, payload_doc: dict[str, Any]) -> int:
            event = await self._session_events_repo.append_event(
                {
                    "session_id": wakeup.session_id,
                    "channel": self.channel,
                    "event_type": event_type,
                    "causation_id": command_id,
                    "correlation_id": correlation_id,
                    "payload": payload_doc,
                }
            )
            return int(event.get("event_seq") or 0)

        async def _record_started() -> None:
            nonlocal started, last_event_seq
            if started:
                return
            started = True
            last_event_seq = await _append_terminal_event(
                "terminal.command_started",
                {
                    "command_id": command_id,
                    "command": command,
                    "working_directory": cwd,
                },
            )
            await self._session_snapshots_repo.apply_channel_update(
                wakeup.session_id,
                channel=self.channel,
                event_seq=last_event_seq,
                updates={
                    "terminal_state": "RUNNING",
                    "terminal_exit_reason": None,
                    "active_terminal_command_id": command_id,
                    "active_terminal_execution_id": None,
                },
            )

        async def _record_execution_bound(execution_id: str) -> None:
            nonlocal bound_execution_id, last_event_seq
            execution_value = str(execution_id or "").strip()
            if not execution_value or execution_value == bound_execution_id:
                return
            if not started:
                await _record_started()
            bound_execution_id = execution_value
            last_event_seq = await _append_terminal_event(
                "terminal.execution_bound",
                {
                    "command_id": command_id,
                    "execution_id": execution_value,
                },
            )
            updates: dict[str, Any] = {
                "active_terminal_command_id": command_id,
                "active_terminal_execution_id": execution_value,
            }
            if not is_isolated_terminal_execution_id(execution_value):
                # A box-level PTY is a separate persistent shell that lifecycle
                # cleanup must close. An isolated run reuses the conversation's
                # own shell, whose lifetime is already the isolated session's;
                # persisting its one-run id as a PTY would later address the
                # wrong OpenSandbox endpoint.
                updates["terminal_pty_session_id"] = execution_value
            await self._session_snapshots_repo.apply_channel_update(
                wakeup.session_id,
                channel=self.channel,
                event_seq=last_event_seq,
                updates=updates,
            )

        async def _sync_projected_interrupt() -> bool:
            nonlocal last_event_seq, saw_exit
            snapshot = await self._session_snapshots_repo.get_snapshot(wakeup.session_id)
            if not isinstance(snapshot, dict):
                return False
            last_event_seq = max(
                last_event_seq,
                int(snapshot.get("terminal_event_seq_applied") or 0),
            )
            state = str(snapshot.get("terminal_state") or "").strip()
            reason = str(snapshot.get("terminal_exit_reason") or "").strip()
            if state == "EXITED" and reason == "interrupted":
                saw_exit = True
                return True
            return False

        async def _record_stream_event(event: dict[str, Any]) -> None:
            nonlocal last_event_seq, saw_exit
            event_type = str(event.get("type") or "")
            if event_type == "ack":
                await _record_started()
                await _append_terminal_artifact(event)
                return

            if not started:
                await _record_started()

            if event_type in {"stdout", "stderr", "exit"} and await _sync_projected_interrupt():
                return

            if event_type == "stdout":
                last_event_seq = await _append_terminal_event(
                    "terminal.stdout_observed",
                    {"text": str(event.get("text") or "")},
                )
                await _append_terminal_artifact(event)
                return
            if event_type == "stderr":
                last_event_seq = await _append_terminal_event(
                    "terminal.stderr_observed",
                    {"text": str(event.get("text") or "")},
                )
                await _append_terminal_artifact(event)
                return
            if event_type == "__cwd__":
                # Internal event from terminal_service: persist cwd in snapshot.
                detected_path = str(event.get("path") or "").strip()
                if detected_path:
                    last_event_seq = await _append_terminal_event(
                        "terminal.cwd_changed",
                        {"path": detected_path},
                    )
                    await self._session_snapshots_repo.apply_channel_update(
                        wakeup.session_id,
                        channel=self.channel,
                        event_seq=last_event_seq,
                        updates={"terminal_cwd": detected_path},
                    )
                    await _append_terminal_artifact(
                        {"type": "cwd", "path": detected_path}
                    )
                return
            if event_type == "exit":
                saw_exit = True
                exit_code = int(event.get("exit_code") or 0)
                last_event_seq = await _append_terminal_event(
                    "terminal.command_exited",
                    {"exit_code": exit_code},
                )
                await _append_terminal_artifact(event)
                await self._session_snapshots_repo.apply_channel_update(
                    wakeup.session_id,
                    channel=self.channel,
                    event_seq=last_event_seq,
                    updates={
                        "terminal_state": "EXITED",
                        "terminal_exit_reason": f"exit:{exit_code}",
                        "active_terminal_command_id": None,
                        "active_terminal_execution_id": None,
                    },
                )
                return

            await _append_terminal_artifact(event)

        async def _record_interrupted() -> None:
            nonlocal last_event_seq, saw_exit
            if saw_exit:
                return
            if await _sync_projected_interrupt():
                return
            saw_exit = True
            last_event_seq = await _append_terminal_event(
                "terminal.command_interrupted",
                {"exit_code": 130, "command_id": command_id},
            )
            await _append_terminal_artifact({"type": "exit", "exit_code": 130})
            await self._session_snapshots_repo.apply_channel_update(
                wakeup.session_id,
                channel=self.channel,
                event_seq=last_event_seq,
                updates={
                    "terminal_state": "EXITED",
                    "terminal_exit_reason": "interrupted",
                    "active_terminal_command_id": None,
                    "active_terminal_execution_id": None,
                },
            )

        async def _record_failure(error_text: str) -> None:
            nonlocal last_event_seq, saw_exit
            if saw_exit:
                return
            saw_exit = True
            last_event_seq = await _append_terminal_event(
                "terminal.command_failed",
                {"error_text": error_text},
            )
            await _append_terminal_artifact({"type": "stderr", "text": f"{error_text}\n"})
            await _append_terminal_artifact({"type": "exit", "exit_code": 1})
            await self._session_snapshots_repo.apply_channel_update(
                wakeup.session_id,
                channel=self.channel,
                event_seq=last_event_seq,
                updates={
                    "terminal_state": "FAILED",
                    "terminal_exit_reason": "error",
                    "active_terminal_command_id": None,
                    "active_terminal_execution_id": None,
                },
            )

        async def _maybe_dispatch_interrupt() -> None:
            nonlocal interrupt_requested, last_command_event_seq, last_event_seq
            if interrupt_requested:
                return
            events = await self._session_events_repo.list_events(
                wakeup.session_id,
                after_seq=last_command_event_seq,
                channel="command",
            )
            for event in events:
                last_command_event_seq = max(last_command_event_seq, int(event.get("event_seq") or 0))
                event_payload = event.get("payload")
                if not isinstance(event_payload, dict):
                    continue
                if str(event_payload.get("command_type") or "").strip() != "InterruptTerminal":
                    continue
                interrupt_requested = True
                result = await self._interrupt_terminal_command(
                    wakeup=wakeup,
                    command_event=event,
                    session=session,
                )
                await _sync_projected_interrupt()
                if not producer_task.done():
                    producer_task.cancel()
                last_event_seq = int(result.get("event_seq") or last_event_seq)
                return

        async def _producer() -> None:
            nonlocal producer_cancelled, producer_error
            try:
                async for event in self._terminal_service.run_terminal_command(
                    user,
                    wakeup.session_id,
                    command,
                    cwd,
                    on_execution_started=_record_execution_bound,
                ):
                    await queue.put(dict(event))
            except asyncio.CancelledError:
                producer_cancelled = True
                raise
            except Exception as exc:
                producer_error = exc
            finally:
                await queue.put(sentinel)

        producer_task = asyncio.create_task(_producer())
        try:
            while True:
                await _maybe_dispatch_interrupt()
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if item is sentinel:
                    break
                await _record_stream_event(item)

            try:
                await producer_task
            except asyncio.CancelledError:
                producer_cancelled = True
            except Exception as exc:
                producer_error = exc

            if producer_cancelled:
                await _record_interrupted()
            elif producer_error is not None:
                worker_status = "degraded"
                worker_error_text = str(producer_error)
                await _record_failure(worker_error_text)
            elif not saw_exit:
                worker_status = "degraded"
                worker_error_text = "terminal command ended without exit event"
                await _record_failure(worker_error_text)
        finally:
            if not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(BaseException):
                    await producer_task

        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status=worker_status,
            processed_event_seq=last_event_seq,
            error_text=worker_error_text,
            metadata={
                "command_id": command_id,
                "command": command,
            },
        )

    @staticmethod
    def _build_user_context(session: dict[str, Any], payload: dict[str, Any]) -> UserContext:
        user_id = str(
            payload.get("author_user_id")
            or session.get("user_id")
            or ""
        ).strip()
        return UserContext(user_id=user_id)

    async def _await_terminal_execution_binding(
        self,
        *,
        session_id: str,
        active_command_id: str,
    ) -> str | None:
        async def _read_bound_execution_id() -> str | None:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            if not isinstance(snapshot, dict):
                raise RuntimeError("terminal snapshot unavailable")
            terminal_state = str(snapshot.get("terminal_state") or "").strip()
            current_command_id = str(snapshot.get("active_terminal_command_id") or "").strip()
            if terminal_state not in _ACTIVE_TERMINAL_STATES:
                return None
            if current_command_id != active_command_id:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="interrupt failed: terminal command changed while waiting for execution binding",
                    status_code=409,
                )
            execution_id = str(snapshot.get("active_terminal_execution_id") or "").strip()
            if execution_id:
                return execution_id
            raise RuntimeError("terminal execution binding not ready")

        return await retry_async_call(
            _read_bound_execution_id,
            should_retry_exception=lambda exc: isinstance(exc, RuntimeError),
            max_attempts=self._EXECUTION_BIND_RETRY_MAX_ATTEMPTS,
            wait_seconds=self._EXECUTION_BIND_RETRY_DELAY_S,
            before_sleep=build_retry_warning_before_sleep(
                logger,
                lambda retry_state, exc: (
                    "terminal execution binding retry session=%s command=%s attempt=%s err=%s"
                    % (
                        session_id,
                        active_command_id,
                        retry_state.attempt_number,
                        exc,
                    )
                ),
            ),
        )

    async def _interrupt_terminal_command(
        self,
        *,
        wakeup: WorkerWakeup,
        command_event: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        command_id = str(command_event.get("causation_id") or "").strip()
        correlation_id = str(command_event.get("correlation_id") or "").strip() or command_id
        snapshot = await self._session_snapshots_repo.get_snapshot(wakeup.session_id)
        terminal_state = str((snapshot or {}).get("terminal_state") or "").strip()
        if terminal_state not in _ACTIVE_TERMINAL_STATES:
            if terminal_state == "EXITED" and str((snapshot or {}).get("terminal_exit_reason") or "").strip() == "interrupted":
                return {
                    "session_id": wakeup.session_id,
                    "status": "completed",
                    "event_seq": int((snapshot or {}).get("terminal_event_seq_applied") or command_event.get("event_seq") or 0),
                }
            return {
                "session_id": wakeup.session_id,
                "status": "idle",
                "event_seq": int((snapshot or {}).get("terminal_event_seq_applied") or command_event.get("event_seq") or 0),
            }

        active_command_id = str((snapshot or {}).get("active_terminal_command_id") or "").strip() or None
        if not active_command_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: active terminal command is unknown",
                status_code=409,
            )
        sandbox_id = str((session or {}).get("sandbox_id") or "").strip() or None
        terminal_seq: int | None = None

        async def _append_terminal_artifact(event: dict[str, Any]) -> None:
            nonlocal terminal_seq
            if terminal_seq is None:
                existing_events = await self._artifacts_repo.list_terminal_events(
                    wakeup.session_id,
                    command_id=active_command_id,
                    after_seq=-1,
                )
                terminal_seq = (
                    max(
                        (int(item.get("terminal_seq") or -1) for item in existing_events),
                        default=-1,
                    )
                    + 1
                )
            await self._artifacts_repo.append_terminal_event(
                {
                    "session_id": wakeup.session_id,
                    "command_id": active_command_id,
                    "terminal_seq": terminal_seq,
                    "turn_id": None,
                    "payload": dict(event),
                    "command": active_command_id,
                    "cwd": None,
                    "created_at": utcnow_iso(),
                }
            )
            terminal_seq += 1

        async def _append_terminal_event(event_type: str, payload_doc: dict[str, Any]) -> int:
            event = await self._session_events_repo.append_event(
                {
                    "session_id": wakeup.session_id,
                    "channel": self.channel,
                    "event_type": event_type,
                    "causation_id": command_id,
                    "correlation_id": correlation_id,
                    "payload": payload_doc,
                }
            )
            return int(event.get("event_seq") or 0)

        async def _update_snapshot(
            event_seq: int,
            *,
            terminal_state: str,
            reason: str | None,
            active_execution_id: str | None,
            active_command: str | None,
        ) -> None:
            await self._session_snapshots_repo.apply_channel_update(
                wakeup.session_id,
                channel=self.channel,
                event_seq=event_seq,
                updates={
                    "terminal_state": terminal_state,
                    "terminal_exit_reason": reason,
                    "active_terminal_command_id": active_command,
                    "active_terminal_execution_id": active_execution_id,
                },
            )

        async def _wait_for_settle() -> dict[str, Any] | None:
            for _ in range(self._TERMINAL_SETTLE_MAX_ATTEMPTS):
                current = await self._session_snapshots_repo.get_snapshot(wakeup.session_id)
                current_state = str((current or {}).get("terminal_state") or "").strip()
                if current_state not in _ACTIVE_TERMINAL_STATES:
                    return current
                await asyncio.sleep(self._TERMINAL_SETTLE_DELAY_S)
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="interrupt failed: terminal did not settle",
                status_code=502,
            )

        requested_event, created = await self._session_events_repo.try_claim_event(
            {
                "session_id": wakeup.session_id,
                "channel": self.channel,
                "event_type": "terminal.interrupt_requested",
                "causation_id": active_command_id,
                "correlation_id": correlation_id,
                "payload": {
                    "command_id": active_command_id,
                    "interrupt_command_id": command_id,
                    "sandbox_id": sandbox_id,
                },
            }
        )
        requested_seq = int(requested_event.get("event_seq") or 0)

        if not created:
            settled = await _wait_for_settle()
            settled_reason = str((settled or {}).get("terminal_exit_reason") or "").strip()
            if settled_reason == "interrupt_failed":
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="interrupt failed: terminal interrupt failed",
                    status_code=502,
                )
            return {
                "session_id": wakeup.session_id,
                "status": "completed",
                "event_seq": int((settled or {}).get("terminal_event_seq_applied") or requested_seq),
            }

        await _update_snapshot(
            requested_seq,
            terminal_state="INTERRUPTING",
            reason="interrupt_requested",
            active_execution_id=str((snapshot or {}).get("active_terminal_execution_id") or "").strip() or None,
            active_command=active_command_id,
        )

        try:
            execution_id = await self._await_terminal_execution_binding(
                session_id=wakeup.session_id,
                active_command_id=active_command_id,
            )
            if not execution_id:
                settled = await self._session_snapshots_repo.get_snapshot(wakeup.session_id)
                return {
                    "session_id": wakeup.session_id,
                    "status": "completed",
                    "event_seq": int((settled or {}).get("terminal_event_seq_applied") or requested_seq),
                }
            await self._runtime_manager.interrupt_terminal_execution(
                wakeup.session_id,
                execution_id=execution_id,
                sandbox_id=sandbox_id,
            )
        except Exception as exc:
            failed_seq = await _append_terminal_event(
                "terminal.command_failed",
                {
                    "command_id": active_command_id,
                    "error_text": str(exc),
                    "operation": "interrupt",
                },
            )
            await _update_snapshot(
                failed_seq,
                terminal_state="FAILED",
                reason="interrupt_failed",
                active_execution_id=None,
                active_command=None,
            )
            raise

        interrupted_seq = await _append_terminal_event(
            "terminal.command_interrupted",
            {
                "command_id": active_command_id,
                "exit_code": 130,
                "execution_id": execution_id,
            },
        )
        await _append_terminal_artifact({"type": "exit", "exit_code": 130})
        await _update_snapshot(
            interrupted_seq,
            terminal_state="EXITED",
            reason="interrupted",
            active_execution_id=None,
            active_command=None,
        )
        return {
            "session_id": wakeup.session_id,
            "status": "completed",
            "event_seq": interrupted_seq,
        }
