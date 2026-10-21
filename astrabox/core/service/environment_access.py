"""Environment entry permission, independent of Agent and conversation ACLs."""

from __future__ import annotations

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import PLATFORM_ADMIN_ROLE, UserContext
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.environment_repository import EnvironmentRepository

logger = get_logger(__name__)


async def require_environment_access(user: UserContext, *, operation: str) -> None:
    """Require membership or explicit platform-administrator authority.

    The deployment endpoint selects the Environment. Callers cannot change it
    through a resource id or request header. A privileged entry without local
    membership is logged with its actor, destination and operation.
    """
    name = get_persistence().environment_name
    if await EnvironmentRepository().has_member(name, user.user_id):
        return
    if PLATFORM_ADMIN_ROLE in user.roles:
        logger.info(
            "platform administrator entered Environment: actor=%s environment=%s operation=%s",
            user.user_id, name, operation,
        )
        return
    raise APIError(
        code="ENVIRONMENT_ACCESS_DENIED", status_code=403,
        message="this account has not been granted access to this Environment",
        data={"environment_name": name},
    )
