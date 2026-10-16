"""What every tenancy shares about an Agent's prepared capacity.

``prepared_capacity`` is how many new conversations of an Agent can start at
once on prepared units: a whole prepared sandbox when each conversation gets its
own, a prepared slot in an Agent sandbox when conversations share one. A new
conversation that finds no unit starts cold, which is the defined path for an
exhausted capacity rather than a fallback, and is recorded here so an operator
can see the capacity was too small.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

#: Where an Agent read carries when a new conversation last found no prepared unit.
LAST_MISS_FIELD = "_prepared_capacity_last_miss_at"


async def record_prepared_capacity_miss(agent_id: str, *, tenancy: str, session_id: str) -> None:
    """Count one cold start of an Agent whose prepared capacity was exhausted."""

    from astrabox.observability.metrics import METRIC_PREPARED_CAPACITY_MISSES, increment
    from astrabox.persistence.repository.agent_repository import AgentRepository

    logger.info(
        "prepared capacity exhausted: agent=%s tenancy=%s session=%s starts cold",
        agent_id,
        tenancy,
        session_id,
    )
    increment(METRIC_PREPARED_CAPACITY_MISSES, labels={"tenancy": tenancy})
    await AgentRepository().record_capacity_miss(agent_id)


#: How long past the pool's idle lease an unclaimed unit's binding is kept: a
#: unit is created, prepared and published within the sandbox ready timeout
#: plus its preparation timeout, both well inside this margin.
_UNCLAIMED_BINDING_MARGIN = timedelta(hours=1)


async def release_abandoned_unit_bindings() -> dict[str, int]:
    """Delete the platform-MCP bindings of prepared units past their idle lease.

    A prepared unit publishes its binding before its sandbox exists, and a
    claim moves the binding to the Session. The supplier discards an unclaimed
    unit on its own when its idle lease ends, when the pool shrinks, or when
    the pool retires, and none of those reach the platform, so the binding of
    every unit that was never claimed is collected here once no unit could
    still be waiting that long.
    """

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.persistence.repository.platform_mcp_binding_repository import (
        PlatformMCPBindingRepository,
    )

    lease = timedelta(seconds=int(load_astrabox_settings().sandbox_lease_seconds))
    cutoff = datetime.now(timezone.utc) - lease - _UNCLAIMED_BINDING_MARGIN
    released = await PlatformMCPBindingRepository().delete_unclaimed_prepared_bindings(
        created_before=cutoff.isoformat()
    )
    return {"prepared_unit_bindings_released": released} if released else {}
