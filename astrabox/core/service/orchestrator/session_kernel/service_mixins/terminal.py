"""TerminalExecutionMixin — leaf mixin for :class:`SessionKernelService`."""
from __future__ import annotations

import asyncio
import contextlib

from collections.abc import AsyncIterator
from typing import Any
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.session_kernel.workers import WorkerWakeup
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import _ACTIVE_TERMINAL_SNAPSHOT_STATES


class TerminalExecutionMixin:
    """Sandbox terminal command surface: dispatch RunTerminalCommand to the
    TerminalWorker and stream terminal_events from the artifacts repo, plus
    the busy guard."""

    async def run_terminal_command(
        self,
        user: UserContext,
        session_id: str,
        command: str,
        cwd: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        self._require_turn_eligible(session, channel="terminal")
        await self._ensure_terminal_command_allowed(session_id, session=session)
        # If the caller didn't provide a cwd, read the last one from the snapshot
        # so terminal cwd persists across commands.
        if not cwd:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            if snapshot:
                cwd = str(snapshot.get("terminal_cwd") or "").strip() or None
        if not cwd:
            cwd = self._runtime_manager.resolve_session_terminal_cwd(
                session_id,
                sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
                session_kind=require_session_kind(session.get("session_kind")),
                engine_session_key=str(session.get("engine_session_key") or "").strip() or None,
            )
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=session_id,
            command_type="RunTerminalCommand",
            payload={"command": command, "cwd": cwd},
        )
        producer_task = self._spawn_background_task(
            self._build_terminal_worker().run(
                WorkerWakeup(
                    session_id=session_id,
                    channel="terminal",
                    command_id=command_id,
                )
            ),
            name=f"session-kernel-terminal-worker-{session_id}",
        )
        async for event in self._tail_terminal_events(
            session_id,
            command_id=command_id,
            producer_task=producer_task,
        ):
            yield event

    async def _ensure_terminal_command_allowed(
        self,
        session_id: str,
        *,
        session: dict[str, Any],
    ) -> None:
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        terminal_state = str((snapshot or {}).get("terminal_state") or "").strip()
        if terminal_state in _ACTIVE_TERMINAL_SNAPSHOT_STATES:
            raise APIError(
                code="SESSION_BUSY",
                message="terminal is already running",
                status_code=409,
            )

    async def _tail_terminal_events(
        self,
        session_id: str,
        *,
        command_id: str,
        producer_task: asyncio.Task | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        last_seq = -1
        queue = await self._broker.subscribe(session_id)

        async def _read_available_events() -> tuple[list[dict[str, Any]], bool]:
            nonlocal last_seq
            saw_terminal = False
            payloads: list[dict[str, Any]] = []
            events = await self._artifacts_repo.list_terminal_events(
                session_id,
                command_id=command_id,
                after_seq=last_seq,
            )
            for event in events:
                terminal_seq = int(event.get("terminal_seq") or 0)
                last_seq = max(last_seq, terminal_seq)
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("type") or "") == "exit":
                    saw_terminal = True
                payloads.append(dict(payload))
            return payloads, saw_terminal

        try:
            while True:
                payloads, saw_terminal = await _read_available_events()
                for payload in payloads:
                    yield payload
                if saw_terminal:
                    if producer_task is not None:
                        await producer_task
                    return

                if producer_task is not None and producer_task.done():
                    producer_error: Exception | None = None
                    try:
                        await producer_task
                    except Exception as exc:
                        producer_error = exc
                    payloads, saw_terminal = await _read_available_events()
                    for payload in payloads:
                        yield payload
                    if saw_terminal:
                        return
                    if producer_error is not None:
                        raise producer_error
                    return

                try:
                    await asyncio.wait_for(queue.get(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    continue
        finally:
            with contextlib.suppress(Exception):
                await self._broker.unsubscribe(session_id, queue)
