"""Administrator-maintained links from a trigger's participants to accounts.

A messaging trigger that runs as linked accounts runs each participant's
messages as the AstraBox account linked to that participant. Platform
administrators keep the links, per trigger; the built-in ``links`` account
resolver reads them (:mod:`astrabox.providers.channel_account_links`).

The links live beside the Deployment rather than in it, so a replacement
deployment service keeps its contract; the caller hands in the binding it
already authorized. A deleted binding's links are deleted with it.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_access import is_platform_admin
from astrabox.persistence.repository.channel_repository import ChannelRepository
from astrabox.seams.channel import EXECUTION_LINKED, channel_if_registered, channel_scene_name

#: Longest participant or user id a link stores.
_LINK_FIELD_LIMIT = 512


def _field(value: Any, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or not text.isprintable() or len(text) > _LINK_FIELD_LIMIT:
        raise APIError(
            code="INVALID_REQUEST",
            message=(
                f"{name} must be a non-empty single-line string of at most "
                f"{_LINK_FIELD_LIMIT} characters"
            ),
            status_code=400,
        )
    return text


def _render(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "participant": str(link.get("participant") or ""),
        "user_id": str(link.get("user_id") or ""),
        "linked_by": str(link.get("linked_by") or ""),
        "linked_at": str(link.get("linked_at") or ""),
    }


class ChannelAccountLinkService:
    """List, set and remove one trigger's participant account links."""

    def __init__(self, repository: ChannelRepository | None = None) -> None:
        self._repository = repository or ChannelRepository()

    @staticmethod
    def _authorize(binding: dict[str, Any], actor: UserContext) -> str:
        """The binding's id, once the actor and the binding may hold links."""
        if not is_platform_admin(getattr(actor, "roles", [])):
            raise APIError(
                code="FORBIDDEN",
                message="only an administrator may link messaging participants to accounts",
                status_code=403,
            )
        channel = channel_scene_name(str(binding.get("scene") or ""))
        provider = channel_if_registered(channel) if channel is not None else None
        if (
            provider is None
            or not binding.get("agent_id")
            or EXECUTION_LINKED not in provider.describe().execution_modes
        ):
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    "account links apply only to an Agent's messaging trigger whose "
                    "platform can run as linked accounts"
                ),
                status_code=400,
            )
        return str(binding["deployment_id"])

    async def links(self, binding: dict[str, Any], *, actor: UserContext) -> list[dict[str, Any]]:
        deployment_id = self._authorize(binding, actor)
        return [_render(link) for link in await self._repository.list_account_links(deployment_id)]

    async def link(
        self, binding: dict[str, Any], *, participant: Any, user_id: Any, actor: UserContext,
    ) -> dict[str, Any]:
        """Link one participant to the account its messages run as.

        A message already admitted keeps the account its first drive kept;
        the link decides messages whose first drive is still ahead.
        """
        deployment_id = self._authorize(binding, actor)
        link = await self._repository.put_account_link(
            deployment_id=deployment_id,
            participant=_field(participant, "participant"),
            user_id=_field(user_id, "user_id"),
            linked_by=actor.user_id,
        )
        return _render(link)

    async def unlink(
        self, binding: dict[str, Any], *, participant: Any, actor: UserContext,
    ) -> dict[str, Any]:
        deployment_id = self._authorize(binding, actor)
        name = _field(participant, "participant")
        deleted = await self._repository.delete_account_link(
            deployment_id=deployment_id, participant=name,
        )
        return {"participant": name, "deleted": deleted}
