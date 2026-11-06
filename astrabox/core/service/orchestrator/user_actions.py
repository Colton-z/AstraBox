"""Explicit host-provider operations, usable before a Session allocates compute."""

from __future__ import annotations

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.runtime_preparation import (
    SessionUserActionContext, SessionUserActionState, runtime_preparation_provider,
)

logger = get_logger(__name__)


async def user_action_state(context: SessionUserActionContext, *, action_id: str | None = None) -> dict:
    provider = runtime_preparation_provider()
    if provider is None:
        if action_id is not None:
            raise APIError(code="INVALID_REQUEST", message="No user operation is configured", status_code=400)
        return {**SessionUserActionState(ready=True).public(), "applicable": True}
    async def state(operation):
        try:
            return (await operation).public()
        except Exception:
            logger.exception("user-action provider failed")
            raise APIError(code="SESSION_USER_ACTION_FAILED", status_code=502,
                           message="Unable to determine the session requirements; refresh to try again") from None

    current = await state(provider.read_user_actions(context=context))
    if action_id is not None:
        if action_id not in {action["id"] for action in current["actions"]}:
            raise APIError(code="INVALID_REQUEST", message="This operation is no longer available; refresh its status", status_code=409)
        current = await state(provider.perform_user_action(context=context, action_id=action_id))
    return {**current, "applicable": True}
