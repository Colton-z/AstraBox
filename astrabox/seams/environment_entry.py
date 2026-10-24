"""Deployment-selected Environment authorization, separate from resource ACLs.

The platform supplies a verified identity, its database-bound Environment and
the operation. A policy decides entry only; it neither grants roles or personal
credentials nor replaces Agent, Assistant or conversation authorization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from astrabox.common.utils.user_context import UserContext

ENTRY_POINT_GROUP = "astrabox.environment.entry"


@runtime_checkable
class EnvironmentEntryPolicy(Protocol):
    """A read-only decision for an authenticated identity, evaluated per entry.

    Return a bool. Denials remain denials; exceptions or an invalid result
    refuse access as a policy failure. Implementations may consult their own
    identity service but must not infer trust from request headers or mutate
    core membership. Replicas select the same policy and authoritative source.
    """

    async def can_enter(self, user: UserContext, *, environment_name: str, operation: str) -> bool:
        """Decide entry to the serving Environment, without changing resource ACLs."""
        ...
