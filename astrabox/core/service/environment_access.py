"""Environment entry permission, independent of Agent and conversation ACLs."""

from __future__ import annotations

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import PLATFORM_ADMIN_ROLE, UserContext
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.providers.environment_entry import configured_environment_entry_policy

logger = get_logger(__name__)


async def can_enter_environment(user: UserContext | None, *, operation: str) -> bool:
    """Apply the selected entry policy or explicit administrator authority.

    The deployment endpoint selects the Environment. Callers cannot change it
    through a resource id or request header. A privileged entry without local
    membership is logged with its actor, destination and operation.
    """
    if user is None or not user.user_id.strip():
        return False
    name = get_persistence().environment_name
    if PLATFORM_ADMIN_ROLE in user.roles:
        if not await EnvironmentRepository().has_member(name, user.user_id):
            logger.info(
                "platform administrator entered Environment: actor=%s environment=%s operation=%s",
                user.user_id, name, operation,
            )
        return True
    try:
        allowed = await configured_environment_entry_policy().can_enter(
            user, environment_name=name, operation=operation,
        )
        if not isinstance(allowed, bool):
            raise TypeError("Environment entry policy must return a bool")
        return allowed
    except Exception as exc:
        logger.exception("Environment entry policy failed: actor=%s environment=%s", user.user_id, name)
        raise APIError(
            code="ENVIRONMENT_ENTRY_POLICY_UNAVAILABLE", status_code=503,
            message="the Environment entry policy could not verify access",
        ) from exc


async def require_environment_access(user: UserContext | None, *, operation: str) -> None:
    """Reject an unauthenticated identity or an entry the selected policy denies."""
    if user is None or not user.user_id.strip():
        raise APIError(code="UNAUTHORIZED", status_code=401, message="an authenticated identity is required")
    if await can_enter_environment(user, operation=operation):
        return
    raise APIError(
        code="ENVIRONMENT_ACCESS_DENIED", status_code=403,
        message="this account has not been granted access to this Environment",
        data={"environment_name": get_persistence().environment_name},
    )
