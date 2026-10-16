"""Global singleton registry for platform services.

All controllers MUST use these getters instead of directly instantiating
services. Lazy-initialised so importing this module stays cheap. The FastAPI
lifespan starts the services with :func:`run_lifecycle_startup` and stops
them with :func:`run_lifecycle_shutdown`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.agent.agent_a2a_service import AgentA2AService

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.assistant.assistant_service import (
    AssistantService,
)
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService

logger = get_logger(__name__)

_platform_service: AgentPlatformService | None = None
_agent_service: AgentService | None = None
_agent_a2a_service: AgentA2AService | None = None
_assistant_service: AssistantService | None = None
_lock = asyncio.Lock()


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
        agent_repo=platform_service._agent_repo,
    )


def get_agent_service() -> AgentService:
    global _agent_service
    if _agent_service is None:
        _agent_service = _build_agent_service(get_platform_service())
    return _agent_service


def get_agent_a2a_service() -> AgentA2AService:
    """Share Agent coordination across A2A and Assistant tool entry points."""
    global _agent_a2a_service
    if _agent_a2a_service is None:
        from astrabox.core.service.orchestrator.agent.agent_a2a_service import AgentA2AService

        _agent_a2a_service = AgentA2AService(agent_service=get_agent_service())
    return _agent_a2a_service


def _build_assistant_service(
    platform_service: AgentPlatformService,
) -> AssistantService:
    return AssistantService(
        agent_config=platform_service._agent_config,
        session_lifecycle=platform_service._session_lifecycle,
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


def _close_service(service: object, *, reason: str) -> None:
    close = getattr(service, "quiesce", None)
    if callable(close):
        close(reason=reason)
        return
    close = getattr(service, "close", None)
    if callable(close):
        close()


def close_services_for_lifecycle(reason: str = "lifecycle_cleanup") -> None:
    """Latch every service closed and drop the registry; never raises."""
    global _agent_service
    global _agent_a2a_service
    global _assistant_service
    global _platform_service
    close_reason = str(reason or "").strip() or "lifecycle_cleanup"
    agent_service = _agent_service
    assistant_service = _assistant_service
    platform_service = _platform_service
    _agent_service = None
    _agent_a2a_service = None
    _assistant_service = None
    _platform_service = None
    logger.warning(
        "agent platform lifecycle cleanup started: reason=%s platform_service=%s "
        "agent_service=%s assistant_service=%s",
        close_reason,
        platform_service is not None,
        agent_service is not None,
        assistant_service is not None,
    )
    for service in (agent_service, assistant_service, platform_service):
        if service is not None:
            _close_service(service, reason=close_reason)


async def run_lifecycle_shutdown(reason: str = "lifespan_shutdown") -> None:
    """Stop the platform: the mirror of :func:`run_lifecycle_startup`.

    First make the synchronous decisions in :func:`close_services_for_lifecycle`:
    latch services closed, drop the registry, and cancel in-flight work. Then,
    while this event loop still exists, await cancelled platform tasks before
    provider-owned clients and reconcilers. Channel drives hand back their
    database leases during cancellation. OpenSandbox's official Agent pool
    releases its Redis primary lock only from its async ``shutdown`` method.
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
    """Start the platform services on the application event loop.

    Called from the FastAPI ``lifespan`` startup (see ``astrabox/api/app.py``).
    Channel spine recovery (expired inbound work items and abandoned outbox
    rows) is owned by the ChannelSpineReconciler, which the bootstrap starts;
    its first tick covers boot-time recovery.
    """
    async with _lock:
        await get_platform_service().ensure_bootstrap()
        await get_agent_service().ensure_bootstrap()
