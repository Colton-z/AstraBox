"""Channel account resolver seam — which AstraBox account a participant runs as.

A messaging binding in the ``linked`` execution mode runs each participant's
messages as that participant's own AstraBox account: the Session, its
connectors and its console visibility belong to that account. A
:class:`ChannelAccountResolver` answers which account that is for one
provider-verified participant of one binding. Core asks once per message, at
its first drive, and keeps the answer on the message's record, so recovery
and redelivery run the message as the same account.

The built-in ``links`` resolver (:mod:`astrabox.providers.channel_account_links`)
reads the per-binding links that platform administrators maintain. A
deployment whose directory already knows the mapping — an enterprise staff
directory — registers its own resolver at the
``astrabox.providers.channel_accounts`` entry-point group and selects it with
``ASTRABOX_CHANNEL_ACCOUNT_RESOLVER``. Selection is name-keyed and fails loud
on an unknown name.

This module imports only the standard library.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

#: Entry-point group an external package registers a resolver under.
ENTRY_POINT_GROUP = "astrabox.providers.channel_accounts"
#: Deployment-wide resolver selection.
CHANNEL_ACCOUNT_RESOLVER_ENV = "ASTRABOX_CHANNEL_ACCOUNT_RESOLVER"
#: The built-in resolver: administrator-maintained links per binding.
DEFAULT_CHANNEL_ACCOUNT_RESOLVER = "links"


class ChannelAccountResolver(ABC):
    """Map a verified channel participant to the AstraBox account it runs as."""

    #: Registry key. Required, non-empty; lowercased when registered.
    name: str

    def validate_configuration(self) -> None:
        """Validate deployment configuration at startup.

        A resolver backed by an external directory raises ``RuntimeError``
        here when its configuration is absent, so startup fails before the
        first linked message instead of refusing every one of them.
        """

    @abstractmethod
    async def resolve(
        self, *, deployment_id: str, channel: str, participant: str,
    ) -> str | None:
        """The AstraBox user id ``participant`` runs as on this binding.

        Return ``None`` when the participant is linked to no account: core
        refuses the message and tells the participant. Raise when the source
        of truth cannot answer now; the message is retried, and it never runs
        as another account in the meantime.
        """


_RESOLVERS: dict[str, ChannelAccountResolver] = {}


def register_channel_account_resolver(resolver: ChannelAccountResolver) -> None:
    """Register a resolver under its ``name``. Last registration wins."""
    if not isinstance(resolver, ChannelAccountResolver):
        raise RuntimeError(
            "channel account resolver must subclass ChannelAccountResolver; got "
            f"{type(resolver).__name__}"
        )
    name = str(getattr(resolver, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("channel account resolver must have a non-empty name")
    _RESOLVERS[name] = resolver


def configured_channel_account_resolver_name() -> str:
    """The deployment's selected resolver name."""
    selected = str(os.getenv(CHANNEL_ACCOUNT_RESOLVER_ENV, "") or "").strip().lower()
    return selected or DEFAULT_CHANNEL_ACCOUNT_RESOLVER


def channel_account_resolver() -> ChannelAccountResolver:
    """The selected resolver, or raise listing the registered names."""
    wanted = configured_channel_account_resolver_name()
    resolver = _RESOLVERS.get(wanted)
    if resolver is None:
        raise RuntimeError(
            f"no ChannelAccountResolver registered for name={wanted!r} "
            f"(registered: {sorted(_RESOLVERS)})"
        )
    return resolver


def registered_channel_account_resolver_names() -> list[str]:
    """The registered resolver names, sorted."""
    return sorted(_RESOLVERS)


__all__ = [
    "CHANNEL_ACCOUNT_RESOLVER_ENV",
    "DEFAULT_CHANNEL_ACCOUNT_RESOLVER",
    "ENTRY_POINT_GROUP",
    "ChannelAccountResolver",
    "channel_account_resolver",
    "configured_channel_account_resolver_name",
    "register_channel_account_resolver",
    "registered_channel_account_resolver_names",
]
