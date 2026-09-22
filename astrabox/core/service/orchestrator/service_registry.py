"""Global singleton registry for platform services.

All controllers MUST use these getters instead of directly instantiating
services. Lazy-initialised so importing this module stays cheap.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import threading

from astrabox.persistence.repository.backend import (
    close_direct_mongo_for_current_loop,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.assistant.assistant_service import (
    AssistantService,
)
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService

logger = get_logger(__name__)

_platform_service: AgentPlatformService | None = None
_agent_service: AgentService | None = None
_assistant_service: AssistantService | None = None
_scheduler_platform_service: AgentPlatformService | None = None
_scheduler_agent_service: AgentService | None = None
_scheduler_assistant_service: AssistantService | None = None
_lifecycle_startup_registered = False
_lifecycle_cleanup_registered = False
_lock = asyncio.Lock()
_scheduler_task: asyncio.Task | None = None
_scheduler_loop: asyncio.AbstractEventLoop | None = None
_scheduler_thread: threading.Thread | None = None
_scheduler_thread_lock = threading.Lock()

_LIFECYCLE_STARTUP_HANDLER_NAME = "AgentPlatformAfterBizStartupEventHandler"
_LIFECYCLE_CALLBACK_NAME = "astrabox_resource_cleanup"
_SCHEDULER_LOOP_READY_TIMEOUT_S = 5.0
_SCHEDULER_THREAD_JOIN_TIMEOUT_S = 5.0
# Closing the scheduler-thread services is scheduled onto that thread's loop, so this
# bounds the round trip rather than the bare thread join.
_SCHEDULER_SERVICES_CLOSE_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class _SchedulerContext:
    scene_id: str | None
    module_name: str
    module_version: str


def get_platform_service() -> AgentPlatformService:
    global _platform_service
    if _platform_service is None:
        _platform_service = AgentPlatformService(
            agent_service_getter=get_agent_service,
            assistant_lifecycle_getter=get_assistant_service,
        )
    return _platform_service


def _build_agent_service(platform_service: AgentPlatformService) -> AgentService:
    return AgentService(
        platform_service=platform_service,
        sessions_repo=platform_service._sessions_repo,
        runtime_manager=platform_service._runtime_manager,
        agent_config=platform_service._agent_config,
        turn_service=platform_service._turn_service,
        broker=platform_service._broker,
        agent_repo=platform_service._agent_repo,
    )


def get_agent_service() -> AgentService:
    global _agent_service
    if _agent_service is None:
        _agent_service = _build_agent_service(get_platform_service())
    return _agent_service


def _build_assistant_service(
    platform_service: AgentPlatformService,
) -> AssistantService:
    return AssistantService(
        agent_config=platform_service._agent_config,
        session_kernel=platform_service._session_kernel,
        runtime_manager=platform_service._runtime_manager,
        workspace_service=platform_service._assistant_workspace_service,
        sessions_repo=platform_service._sessions_repo,
        sandbox_lifecycle_service=platform_service._sandbox_lifecycle_service,
        spawn_background_task=platform_service._spawn_background_task,
    )


def get_assistant_service() -> AssistantService:
    global _assistant_service
    if _assistant_service is None:
        _assistant_service = _build_assistant_service(get_platform_service())
    return _assistant_service


async def ensure_schedulers_started() -> None:
    async with _lock:
        platform_service = get_platform_service()
        await platform_service.ensure_bootstrap()
        agent_service = get_agent_service()
        await agent_service.ensure_bootstrap()


def _get_lifecycle_scheduler_platform_service() -> AgentPlatformService:
    global _scheduler_platform_service
    if _scheduler_platform_service is None:
        _scheduler_platform_service = AgentPlatformService(
            agent_service_getter=_get_lifecycle_scheduler_agent_service,
            assistant_lifecycle_getter=_get_lifecycle_scheduler_assistant_service,
        )
    return _scheduler_platform_service


def _get_lifecycle_scheduler_agent_service() -> AgentService:
    global _scheduler_agent_service
    if _scheduler_agent_service is None:
        _scheduler_agent_service = _build_agent_service(
            _get_lifecycle_scheduler_platform_service()
        )
    return _scheduler_agent_service


def _get_lifecycle_scheduler_assistant_service() -> AssistantService:
    global _scheduler_assistant_service
    if _scheduler_assistant_service is None:
        _scheduler_assistant_service = _build_assistant_service(
            _get_lifecycle_scheduler_platform_service()
        )
    return _scheduler_assistant_service


async def _ensure_lifecycle_schedulers_started() -> None:
    platform_service = _get_lifecycle_scheduler_platform_service()
    await platform_service.ensure_bootstrap()
    agent_service = _get_lifecycle_scheduler_agent_service()
    await agent_service.ensure_bootstrap()


def start_schedulers_background(context: _SchedulerContext | None = None) -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if context is None:
            raise RuntimeError("agent platform scheduler context required outside running event loop")
        _start_scheduler_loop_thread(context)
        return
    task = loop.create_task(ensure_schedulers_started())
    _scheduler_task = task

    def _on_done(done_task: asyncio.Task) -> None:
        global _scheduler_task
        if _scheduler_task is done_task:
            _scheduler_task = None
        if done_task.cancelled():
            logger.warning("agent platform scheduler bootstrap task cancelled")
            return
        try:
            done_task.result()
        except Exception as exc:
            logger.warning("agent platform scheduler bootstrap failed: %s", exc, exc_info=True)

    task.add_done_callback(_on_done)


def _start_scheduler_loop_thread(context: _SchedulerContext) -> None:
    global _scheduler_loop
    global _scheduler_thread

    with _scheduler_thread_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        ready = threading.Event()

        def _run_loop() -> None:
            global _scheduler_loop
            global _scheduler_thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with _scheduler_thread_lock:
                _scheduler_loop = loop
            ready.set()

            async def _bootstrap() -> None:
                try:
                    await _ensure_lifecycle_schedulers_started()
                except Exception as exc:
                    logger.warning(
                        "agent platform scheduler bootstrap failed in lifecycle loop: %s",
                        exc,
                        exc_info=True,
                    )

            # The lifecycle loop runs directly in this thread's event loop. The
            # surrounding try/finally drains tasks and closes Mongo on loop stop.
            try:
                loop.create_task(
                    _bootstrap(),
                    name="agent-platform-scheduler-bootstrap",
                )
                loop.run_forever()
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel("astrabox_scheduler_loop_stop")
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                with contextlib.suppress(Exception):
                    loop.run_until_complete(
                        close_direct_mongo_for_current_loop(
                            reason="astrabox_scheduler_loop_stop"
                        )
                    )
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                with _scheduler_thread_lock:
                    if _scheduler_loop is loop:
                        _scheduler_loop = None
                    if _scheduler_thread is threading.current_thread():
                        _scheduler_thread = None

        thread = threading.Thread(
            target=_run_loop,
            name="agent-platform-scheduler-loop",
            daemon=True,
        )
        _scheduler_thread = thread
        thread.start()

    if not ready.wait(_SCHEDULER_LOOP_READY_TIMEOUT_S):
        _stop_scheduler_loop_thread("scheduler_loop_start_timeout")
        raise RuntimeError("agent platform scheduler loop did not start")


def _stop_scheduler_loop_thread(reason: str) -> None:
    global _scheduler_loop
    global _scheduler_thread

    with _scheduler_thread_lock:
        loop = _scheduler_loop
        thread = _scheduler_thread
        _scheduler_loop = None
        _scheduler_thread = None

    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if (
        thread is not None
        and thread.is_alive()
        and thread is not threading.current_thread()
    ):
        thread.join(timeout=_SCHEDULER_THREAD_JOIN_TIMEOUT_S)
        if thread.is_alive():
            logger.warning(
                "agent platform scheduler loop thread did not stop: reason=%s",
                reason,
            )


def _close_service(service: object, *, reason: str) -> None:
    close = getattr(service, "quiesce", None)
    if callable(close):
        close(reason=reason)
        return
    close = getattr(service, "close", None)
    if callable(close):
        close()


def _close_scheduler_services_on_owner_loop(
    *,
    agent_service: AgentService | None,
    platform_service: AgentPlatformService | None,
    reason: str,
) -> None:
    if agent_service is None and platform_service is None:
        return

    with _scheduler_thread_lock:
        loop = _scheduler_loop
        thread = _scheduler_thread

    def _close_services() -> None:
        if agent_service is not None:
            _close_service(agent_service, reason=reason)
        if platform_service is not None:
            _close_service(platform_service, reason=reason)

    if (
        loop is None
        or not loop.is_running()
        or thread is None
        or thread is threading.current_thread()
    ):
        _close_services()
        return

    async def _close_on_owning_loop() -> None:
        # Runs on the loop that owns these services: the close decisions must land
        # there, not on whichever thread happens to be tearing the process down.
        try:
            _close_services()
            if platform_service is not None:
                await platform_service.wait_closed()
        finally:
            from astrabox.seams.sandbox import (
                shutdown_sandbox_providers_for_current_loop,
            )

            await shutdown_sandbox_providers_for_current_loop()

    future = asyncio.run_coroutine_threadsafe(_close_on_owning_loop(), loop)
    try:
        future.result(timeout=_SCHEDULER_SERVICES_CLOSE_TIMEOUT_S)
    except TimeoutError:
        logger.warning("agent platform scheduler services close timed out: reason=%s", reason)
    except BaseException as error:  # noqa: BLE001 — a teardown must not raise
        logger.warning(
            "agent platform scheduler services close failed: reason=%s error=%s",
            reason,
            error,
            exc_info=True,
        )


def close_services_for_lifecycle(reason: str = "lifecycle_cleanup") -> None:
    global _agent_service
    global _assistant_service
    global _platform_service
    global _scheduler_agent_service
    global _scheduler_assistant_service
    global _scheduler_platform_service
    global _scheduler_task
    close_reason = str(reason or "lifecycle_cleanup").strip()
    if not close_reason:
        close_reason = "lifecycle_cleanup"

    scheduler_task = _scheduler_task
    agent_service = _agent_service
    assistant_service = _assistant_service
    platform_service = _platform_service
    scheduler_agent_service = _scheduler_agent_service
    scheduler_assistant_service = _scheduler_assistant_service
    scheduler_platform_service = _scheduler_platform_service
    _scheduler_task = None
    _agent_service = None
    _assistant_service = None
    _platform_service = None
    _scheduler_agent_service = None
    _scheduler_assistant_service = None
    _scheduler_platform_service = None

    if isinstance(scheduler_task, asyncio.Task) and not scheduler_task.done():
        scheduler_task.cancel(close_reason)

    logger.warning(
        "agent platform lifecycle cleanup started: "
        "reason=%s platform_service=%s agent_service=%s assistant_service=%s "
        "scheduler_platform_service=%s scheduler_agent_service=%s "
        "scheduler_assistant_service=%s",
        close_reason,
        platform_service is not None,
        agent_service is not None,
        assistant_service is not None,
        scheduler_platform_service is not None,
        scheduler_agent_service is not None,
        scheduler_assistant_service is not None,
    )

    if agent_service is not None:
        _close_service(agent_service, reason=close_reason)

    if assistant_service is not None:
        _close_service(assistant_service, reason=close_reason)

    if platform_service is not None:
        _close_service(platform_service, reason=close_reason)

    _close_scheduler_services_on_owner_loop(
        agent_service=scheduler_agent_service,
        platform_service=scheduler_platform_service,
        reason=close_reason,
    )

    _stop_scheduler_loop_thread(close_reason)


def quiesce_services(reason: str = "lifecycle_cleanup") -> None:
    close_services_for_lifecycle(reason=reason)


async def run_lifecycle_shutdown(reason: str = "lifespan_shutdown") -> None:
    """Stop the platform — the Community shutdown entry point, and the mirror of
    :func:`run_lifecycle_startup`.

    First make the synchronous decisions in :func:`close_services_for_lifecycle`:
    latch services closed, drop the registry, and cancel in-flight work. Then,
    while this event loop still exists, await cancelled platform tasks before
    provider-owned clients and reconcilers. Channel drives hand back their
    database leases during cancellation. OpenSandbox's official Agent pool releases its
    Redis primary lock only from its async ``shutdown`` method.

    A separate scheduler thread closes its provider resources on its own loop in
    :func:`_close_scheduler_services_on_owner_loop`; this final call covers the
    normal FastAPI/request loop.
    """
    platform_service = _platform_service
    try:
        close_services_for_lifecycle(reason=reason)
        if platform_service is not None:
            await platform_service.wait_closed()
    finally:
        from astrabox.seams.sandbox import (
            shutdown_sandbox_providers_for_current_loop,
        )

        await shutdown_sandbox_providers_for_current_loop()


async def run_lifecycle_startup() -> None:
    """Start the platform schedulers — the Community startup entry point.

    Called from the FastAPI ``lifespan`` startup (see ``astrabox/api/app.py``),
    which runs on the application event loop. That is the *running-loop* case, so
    this delegates to :func:`ensure_schedulers_started` (bootstrap the services on
    this loop).

    The separate-thread scheduler machinery (:func:`start_schedulers_background`
    with a ``_SchedulerContext`` → :func:`_start_scheduler_loop_thread`) is kept
    intact for any caller that bootstraps without a running loop, but the normal
    path is this coroutine, awaited inside the lifespan.
    """
    global _lifecycle_startup_registered
    _lifecycle_startup_registered = True
    await ensure_schedulers_started()
    # Channel spine recovery (expired inbound work items + abandoned outbox
    # rows) is owned by the ChannelSpineReconciler, started after bootstrap
    # by the BootstrapReconciler; its first tick covers boot-time recovery.


def register_lifecycle_cleanup() -> None:
    """Mark lifecycle cleanup as owned by the FastAPI lifespan (no base callback).

    Process restart is a clean teardown, and graceful shutdown runs from the
    FastAPI ``lifespan`` shutdown block, which calls
    :func:`close_services_for_lifecycle` directly. This records the flag for
    :func:`register_lifecycle_hooks` without installing another callback.
    """
    global _lifecycle_cleanup_registered
    if _lifecycle_cleanup_registered:
        return
    _lifecycle_cleanup_registered = True
    logger.info(
        "agent platform lifecycle cleanup owned by FastAPI lifespan "
        "(callback=%s); no base registration in Community",
        _LIFECYCLE_CALLBACK_NAME,
    )


def register_lifecycle_startup() -> None:
    """No-op — startup is driven by the FastAPI lifespan, not this registrar.

    The lifespan awaits :func:`run_lifecycle_startup` directly. This callable
    remains idempotent for the aggregate ``register_lifecycle_hooks`` entry point.
    """
    global _lifecycle_startup_registered
    if _lifecycle_startup_registered:
        return
    _lifecycle_startup_registered = True
    logger.info(
        "agent platform lifecycle startup owned by FastAPI lifespan "
        "(handler=%s)",
        _LIFECYCLE_STARTUP_HANDLER_NAME,
    )


def register_lifecycle_hooks() -> None:
    """Aggregate hook: marks both lifecycle flags via the two no-op registrars.

    Both startup and shutdown are wired through the FastAPI
    ``lifespan`` (``run_lifecycle_startup`` / ``close_services_for_lifecycle``).
    Bootstrap uses this aggregate entry point to record both flags.
    """
    register_lifecycle_startup()
    register_lifecycle_cleanup()
