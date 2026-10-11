"""The built-in channel account resolver (``links``).

Reads the links platform administrators maintain for each messaging binding:
one participant, one AstraBox account. Links belong to one binding, so a
participant linked on one binding has no account on another.
"""

from __future__ import annotations

from astrabox.persistence.repository.channel_repository import ChannelRepository
from astrabox.seams.channel_accounts import (
    DEFAULT_CHANNEL_ACCOUNT_RESOLVER,
    ChannelAccountResolver,
    register_channel_account_resolver,
)


class LinkTableChannelAccountResolver(ChannelAccountResolver):
    """Resolve a participant through its binding's administrator-made link."""

    name = DEFAULT_CHANNEL_ACCOUNT_RESOLVER

    def __init__(self, repository: ChannelRepository | None = None) -> None:
        self._repository = repository or ChannelRepository()

    async def resolve(
        self, *, deployment_id: str, channel: str, participant: str,
    ) -> str | None:
        _ = channel
        link = await self._repository.get_account_link(
            deployment_id=deployment_id, participant=participant,
        )
        return str(link["user_id"]) if link and link.get("user_id") else None


register_channel_account_resolver(LinkTableChannelAccountResolver())
