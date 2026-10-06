"""Authorization for spending the origin's model or starting its Agent."""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_access import can_view_agent

from .service import ArtifactViewer


def origin_authorized(
    user: UserContext | None,
    level: str,
    owner_id: str,
    origin: dict[str, Any],
    agent: dict[str, Any] | None,
    *,
    run: bool,
) -> bool:
    if user is None:
        return False
    if origin.get("assistant_id"):
        return user.user_id == owner_id
    return bool(
        agent
        and (not run or level in {"interact", "owner"})
        and can_view_agent(agent, user.user_id, user.roles)
    )


class OriginAccess:
    def __init__(self, *, agent_config: Any, agent_repo: Any) -> None:
        self._agent_config, self._agent_repo = agent_config, agent_repo

    async def authorize(
        self, viewer: ArtifactViewer, record: dict[str, Any], *, run: bool = False
    ) -> Any:
        origin = record["origin"]
        agent = (
            await self._agent_repo.get_agent(origin["agent_id"]) if origin.get("agent_id") else None
        )
        assistant_id = origin.get("assistant_id")
        assistant = (
            await self._agent_config._assistant_repo.get_assistant(assistant_id)
            if assistant_id
            else None
        )
        if (assistant_id and not assistant) or (not assistant_id and not agent):
            if run:
                raise APIError(
                    code="ARTIFACT_AGENT_UNAVAILABLE",
                    message="origin is unavailable",
                    status_code=503,
                )
            raise APIError(
                code="ARTIFACT_SAMPLE_UNAVAILABLE", message="origin is unavailable", status_code=503
            )
        if not origin_authorized(
            viewer.user, viewer.level, record["owner_id"], origin, agent, run=run
        ):
            if run:
                raise APIError(
                    code="ARTIFACT_AGENT_DENIED",
                    message="viewer cannot run the origin Agent",
                    status_code=403,
                )
            raise APIError(
                code="ARTIFACT_SAMPLE_DENIED",
                message="viewer cannot use the origin model",
                status_code=403,
            )
        assert viewer.user is not None
        harness = (
            await self._agent_config._resolve_assistant_harness(assistant_id)
            if assistant_id
            else await self._agent_config.resolve_agent_harness(
                origin["agent_id"], viewer_user_id=viewer.user.user_id, viewer_roles=viewer.user.roles
            )
        )
        if harness is None:
            if run:
                raise APIError(
                    code="ARTIFACT_AGENT_UNAVAILABLE",
                    message="origin is unavailable",
                    status_code=503,
                )
            raise APIError(
                code="ARTIFACT_SAMPLE_UNAVAILABLE", message="origin is unavailable", status_code=503
            )
        return harness
