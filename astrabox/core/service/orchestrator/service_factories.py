"""Service factory overrides — wrap or replace core sub-services, no fork.

Downstream distributions override how ``AgentPlatformService`` builds its
sub-services by registering an entry point at the
``astrabox.service_factories`` group. Each entry resolves to a
``dict[str, ServiceFactory]`` (or a zero-arg callable returning one), keyed
by service name:

    session_service, turn_service, terminal_service, session_file_service,
    expose_port_service, platform_mcp_service, session_title_service,
    session_kernel, session_share_service, admin_service, deployment_service

A ``ServiceFactory`` is ``factory(build_default) -> service``: it receives
the zero-arg closure that builds the stock implementation and returns the
instance to use — call it and wrap the result, or ignore it and build your
own (the returned object must satisfy the stock service's used surface).

``deployment_service`` has one security-sensitive nested-resource contract.  A
replacement must satisfy :class:`DeploymentServiceReplacement`; in particular,
``update`` and ``delete`` receive the route-authorized ``agent_id`` as a
required keyword and must atomically restrict the mutation to an active webhook
owned by that agent. Missing, deleted and foreign webhook ids are the same
404 boundary. This is the pre-0.1 contract freeze for replacement services.

Two overrides for the same name from different distributions are a genuine
deployment conflict and fail loud at first construction — never silently
last-writer-wins. Unknown names fail loud too (they would otherwise rot
silently when a service is renamed).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from astrabox.common.logger.logger_factory import get_logger
from astrabox.providers import _select_entry_points

logger = get_logger(__name__)

SERVICE_FACTORIES_GROUP = "astrabox.service_factories"

#: The overridable construction sites in AgentPlatformService, kept in one
#: place so an unknown-name override fails loud instead of rotting.
KNOWN_SERVICE_NAMES: frozenset[str] = frozenset(
    {
        "session_service",
        "turn_service",
        "terminal_service",
        "session_file_service",
        "expose_port_service",
        "platform_mcp_service",
        "session_title_service",
        "session_kernel",
        "session_share_service",
        "admin_service",
        "deployment_service",
    }
)

ServiceFactory = Callable[[Callable[[], Any]], Any]


@runtime_checkable
class DeploymentServiceReplacement(Protocol):
    """Public used surface for a ``deployment_service`` factory replacement.

    Runtime protocol checks establish method presence; type checking establishes
    the keyword signatures. Implementations own persistence, but scoped update
    and delete MUST enforce the agent boundary themselves rather than
    treating ``agent_id`` as informational.
    """

    async def assert_can_manage_agent(
        self, user: Any, agent_id: str
    ) -> dict[str, Any]: ...

    async def list_for_agent(self, agent_id: str) -> list[dict[str, Any]]: ...

    async def list_manageable(self, user: Any) -> list[dict[str, Any]]: ...

    async def create(
        self,
        *,
        agent_id: str,
        creator_user_id: str,
        scene: str,
        name: str = "",
        prompt_prefix: str = "",
        secret: str | None = None,
        attention_policy: str | None = None,
        channel_config: Any = None,
        credentials: Any = None,
        callback_base_url: str | None = None,
        schedule: Any = None,
    ) -> dict[str, Any]: ...

    async def update(
        self,
        deployment_id: str,
        *,
        agent_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def delete(self, deployment_id: str, *, agent_id: str) -> None: ...

    async def list_runs_for_deployment(
        self,
        deployment_id: str,
        *,
        agent_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    async def trigger_now(
        self, deployment_id: str, *, agent_id: str
    ) -> dict[str, Any]: ...

    async def replay_run(
        self,
        run_id: str,
        *,
        deployment_id: str,
        agent_id: str,
    ) -> dict[str, Any]: ...

    async def start_run_session(
        self, context: dict[str, Any], *, run_id: str
    ) -> str: ...

    async def drive_run_turn(
        self,
        context: dict[str, Any],
        *,
        run_id: str,
        session_id: str,
    ) -> dict[str, Any]: ...

    async def trigger(
        self, deployment_id: str, *, headers: dict[str, str], raw_body: bytes
    ) -> dict[str, Any]: ...

    async def forward_channel_callback(
        self,
        deployment_id: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> Any: ...

_cached: dict[str, ServiceFactory] | None = None


def load_service_factory_overrides(*, refresh: bool = False) -> dict[str, ServiceFactory]:
    """Resolve + merge every ``astrabox.service_factories`` entry, fail-loud.

    Cached after the first call (the group is deployment configuration, not
    per-request state); ``refresh=True`` re-reads it — tests only.
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached

    from astrabox.providers import _check_seams_api_version

    merged: dict[str, ServiceFactory] = {}
    for ep in sorted(
        _select_entry_points(SERVICE_FACTORIES_GROUP).values(), key=lambda e: e.name
    ):
        try:
            target = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"service-factory entry point {ep.name!r} failed to load: {exc}"
            ) from exc
        target = _check_seams_api_version(
            target, group=SERVICE_FACTORIES_GROUP, name=ep.name
        )
        mapping = target() if not isinstance(target, dict) and callable(target) else target
        if not isinstance(mapping, dict):
            raise RuntimeError(
                f"service-factory entry point {ep.name!r} must resolve to a "
                f"dict[str, factory] (or a zero-arg callable returning one); "
                f"got {type(mapping).__name__}"
            )
        for name, factory in mapping.items():
            if name not in KNOWN_SERVICE_NAMES:
                raise RuntimeError(
                    f"service-factory entry point {ep.name!r} overrides unknown "
                    f"service {name!r}; known names: {sorted(KNOWN_SERVICE_NAMES)}"
                )
            if name in merged:
                raise RuntimeError(
                    f"service {name!r} is overridden by more than one "
                    f"distribution (second: entry point {ep.name!r}) — resolve "
                    "the deployment conflict; overrides never silently stack"
                )
            if not callable(factory):
                raise RuntimeError(
                    f"override for service {name!r} (entry point {ep.name!r}) "
                    f"is not callable"
                )
            merged[name] = factory
            logger.info(
                "service factory override registered: %s (from %s)", name, ep.name
            )
    _cached = merged
    return merged
