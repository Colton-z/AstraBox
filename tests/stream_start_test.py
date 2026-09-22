"""Cancelled stream starts finish under the platform's background-task owner.

By the time ``send_message_ai_stream`` (turns.py) sees the ``CancelledError`` from
its shielded wait for the first event, the underlying generator may already
have durably dispatched a command and spawned a worker task -- both are
decoupled from this request's lifetime (``_spawn_background_task`` detaches
from the request-scoped cancel context). Closing the generator at that point
would leave the worker output undrained and the session PROCESSING until
reconciliation. ``drain_cancelled_stream_start`` instead drains the same
generator under the background-task owner so the turn can complete.

`asyncio_mode = "auto"` (see pyproject) runs these bare ``async def test_*``
coroutines directly — no decorator needed.
"""

from __future__ import annotations

import asyncio

import pytest

from astrabox.api.routes import stream_start as stream_start_module
from astrabox.api.routes import turns as turns_module
from astrabox.api.routes.stream_start import (
    drain_cancelled_stream_start,
    spawn_drain_cancelled_stream_start,
)
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService


class _FakeAgen:
    """A minimal stand-in for the ``stream_ai_stream`` async generator: its
    first ``__anext__()`` represents "dispatch the command + spawn the
    worker", later ones represent frames the (already-spawned,
    request-detached) worker produces as the turn runs."""

    def __init__(self, items: list[dict]) -> None:
        self._items = list(items)
        self.closed = False

    def __aiter__(self) -> "_FakeAgen":
        return self

    async def __anext__(self) -> dict:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _QuiesceParticipant:
    def quiesce(self, **_kwargs: str) -> None:
        pass


class _PlatformTaskOwner(AgentPlatformService):
    """Only the real platform task ownership and quiesce methods, without I/O."""

    def __init__(self) -> None:
        self._quiesced_reason = None
        self._bootstrapped = True
        self._background_tasks: set[asyncio.Task] = set()
        participant = _QuiesceParticipant()
        self._session_kernel = participant
        self._runtime_manager = participant
        self._expiration_watcher = participant
        self._channel_spine_reconciler = participant
        self._channel_source_host = participant


class _BlockingAgen:
    def __init__(self) -> None:
        self.first_event_started = asyncio.Event()
        self.release_first_event = asyncio.Event()
        self.first_event_task: asyncio.Task | None = None
        self.first_event_running = False
        self.closed = False

    def __aiter__(self) -> "_BlockingAgen":
        return self

    async def __anext__(self) -> dict:
        self.first_event_task = asyncio.current_task()
        self.first_event_running = True
        self.first_event_started.set()
        try:
            await self.release_first_event.wait()
        finally:
            self.first_event_running = False
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _RoutePlatform(_PlatformTaskOwner):
    def __init__(self) -> None:
        super().__init__()
        self.agen = _BlockingAgen()

    async def must_own_session(self, *_args: object) -> None:
        pass

    def stream_message_events_ds(
        self, *_args: object, **_kwargs: object
    ) -> _BlockingAgen:
        return self.agen


async def test_drain_runs_the_generator_to_completion_in_the_background() -> None:
    # The route handler's wait for the first event was cancelled (client
    # disconnected), but first_event_task itself was never cancelled
    # (asyncio.shield protects it in turns.py), so it can still be awaited
    # here for its result.
    agen = _FakeAgen([{"n": 1}, {"n": 2}, {"n": 3}])
    first_event_task = asyncio.create_task(agen.__anext__())

    await drain_cancelled_stream_start("s1", agen, first_event_task)

    # Every frame was consumed (the turn ran to completion server-side)...
    assert agen._items == []
    # ...and the generator was closed once fully drained.
    assert agen.closed


async def test_drain_swallows_first_event_task_error_and_still_closes_agen() -> None:
    # If dispatch itself blew up (not a disconnect), the drain must not
    # propagate that into the background task, but must still release agen.
    agen = _FakeAgen([{"n": 1}])

    async def _boom() -> dict:
        raise RuntimeError("worker blew up before its first frame")

    first_event_task = asyncio.create_task(_boom())

    await drain_cancelled_stream_start("s1", agen, first_event_task)  # must not raise

    assert agen.closed
    # first_event_task's own exception short-circuits before the drain loop
    # begins, so the generator is never touched.
    assert agen._items == [{"n": 1}]


async def test_spawn_is_owned_by_the_platform_until_done() -> None:
    platform = _PlatformTaskOwner()
    agen = _FakeAgen([])
    first_event_task = asyncio.create_task(agen.__anext__())

    task = spawn_drain_cancelled_stream_start(
        "s2",
        agen,
        first_event_task,
        spawn_background_task=platform._spawn_background_task,
    )

    # Spawning must be fire-and-forget (returns immediately) yet tracked, so
    # the task cannot be garbage-collected mid-flight.
    assert task in platform._background_tasks

    await task

    assert task not in platform._background_tasks
    assert agen.closed


async def test_platform_quiesce_stops_an_in_flight_cancelled_stream_drain() -> None:
    platform = _PlatformTaskOwner()
    agen = _BlockingAgen()
    first_event_task = asyncio.create_task(agen.__anext__())
    task = spawn_drain_cancelled_stream_start(
        "s-quiesce",
        agen,
        first_event_task,
        spawn_background_task=platform._spawn_background_task,
    )
    await agen.first_event_started.wait()

    try:
        platform.quiesce(reason="test-shutdown")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)

        assert task.done()
        assert first_event_task.done()
        assert not agen.first_event_running
        assert agen.closed
    finally:
        agen.release_first_event.set()
        for pending in (task, first_event_task):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, first_event_task, return_exceptions=True)


async def test_quiesced_platform_refuses_handoff_and_route_stops_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _RoutePlatform()

    async def _resolve_user(_request: object) -> object:
        return object()

    monkeypatch.setattr(turns_module, "_svc", lambda: platform)
    monkeypatch.setattr(turns_module, "_resolve_user", _resolve_user)
    request_task = asyncio.create_task(
        turns_module.send_message_ai_stream(
            "s-quiesced",
            object(),  # type: ignore[arg-type]
            {"content": "hello"},
        )
    )
    await platform.agen.first_event_started.wait()

    try:
        platform.quiesce(reason="test-shutdown")
        request_task.cancel()
        with pytest.raises(APIError) as caught:
            await request_task

        assert caught.value.code == "ASTRABOX_RELEASING"
        assert platform._background_tasks == set()
        assert not platform.agen.first_event_running
        assert platform.agen.closed
    finally:
        platform.agen.release_first_event.set()
        cleanup_tasks = tuple(
            task
            for task in (request_task, platform.agen.first_event_task)
            if task is not None
        )
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)


async def test_log_drain_task_done_reports_failures_without_raising() -> None:
    # The done-callback must tolerate every terminal task state: clean exit,
    # a genuine exception, and cancellation -- none of them may raise or
    # leave the task's exception unretrieved.
    async def _ok() -> None:
        return None

    ok_task = asyncio.create_task(_ok())
    await ok_task
    stream_start_module._log_drain_task_done(ok_task)

    async def _boom() -> None:
        raise RuntimeError("background drain crashed")

    failed_task = asyncio.create_task(_boom())
    try:
        await failed_task
    except RuntimeError:
        pass
    stream_start_module._log_drain_task_done(failed_task)

    async def _sleep_forever() -> None:
        await asyncio.sleep(10)

    cancelled_task = asyncio.create_task(_sleep_forever())
    cancelled_task.cancel()
    try:
        await cancelled_task
    except asyncio.CancelledError:
        pass
    stream_start_module._log_drain_task_done(cancelled_task)
