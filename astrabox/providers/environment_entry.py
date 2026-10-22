"""Environment entry policy selection through the platform provider loader."""

from __future__ import annotations

import inspect
import os
from functools import lru_cache

from astrabox.common.utils.user_context import UserContext
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.seams.environment_entry import ENTRY_POINT_GROUP, EnvironmentEntryPolicy

_REGISTERED: dict[str, EnvironmentEntryPolicy] = {}


class MembersEnvironmentEntryPolicy:
    """The serving Environment's explicit member list."""

    async def can_enter(self, user: UserContext, *, environment_name: str, operation: str) -> bool:
        return await EnvironmentRepository().has_member(environment_name, user.user_id)


def register_environment_entry_policy(name: str, policy: EnvironmentEntryPolicy) -> None:
    """Register an instance at the composition root, before serving requests."""
    name = name.strip().lower()
    if not name or not inspect.iscoroutinefunction(getattr(policy, "can_enter", None)):
        raise RuntimeError("Environment entry policy requires a name and async can_enter()")
    _REGISTERED[name] = policy
    configured_environment_entry_policy.cache_clear()


@lru_cache(maxsize=1)
def configured_environment_entry_policy() -> EnvironmentEntryPolicy:
    """Resolve the selected policy at startup; unknown names fail construction."""
    from astrabox.providers import load_provider

    name = str(os.getenv("ASTRABOX_ENVIRONMENT_ENTRY_POLICY", "members")).strip().lower()
    if not name:
        raise RuntimeError("ASTRABOX_ENVIRONMENT_ENTRY_POLICY must not be empty")
    policy = _REGISTERED.get(name)
    if policy is None:
        target = load_provider(ENTRY_POINT_GROUP, name)
        policy = target() if isinstance(target, type) else target
    if not isinstance(policy, EnvironmentEntryPolicy) or not inspect.iscoroutinefunction(policy.can_enter):
        raise RuntimeError(f"Environment entry policy {name!r} must implement async can_enter()")
    return policy
