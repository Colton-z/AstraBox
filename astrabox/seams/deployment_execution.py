"""Deployment-owned checks before saving a channel's execution identity.

Install a validator on the default DeploymentService through a service factory.
Core validates syntax, provider capabilities and administrator authority first.
The validator may consult its own directory and raise APIError to reject the
write. It must not mutate platform state; accepted messages retain their frozen
identity without calling this validator again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from astrabox.common.utils.user_context import UserContext


@dataclass(frozen=True)
class DeploymentExecutionContext:
    """The normalized candidate identity and the explicit caller of one write."""

    operation: Literal["create", "update"]
    deployment_id: str
    agent_id: str
    assistant_id: str
    scene: str
    execution_mode: str
    execution_user_id: str
    actor: UserContext


DeploymentExecutionValidator = Callable[[DeploymentExecutionContext], Awaitable[None]]
