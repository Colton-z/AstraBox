"""Extension catalog and runtime-binding seam.

An extension provider owns the external catalog protocol, its management
surface, and the credential its own MCP gateway requires. The orchestrator
owns Agent authorization and persists only the provider-neutral selection
returned by this contract; the credential reaches the wire through the
sandbox's egress vault, so no selection ever carries a secret.

This module is import-light so an external provider distribution can implement
the contract without importing AstraBox's API or service layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExtensionCatalogItem:
    """One selectable catalog item plus provider-private source metadata."""

    item_id: str
    name: str
    description: str | None = None
    transport: str | None = None
    provider_data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def public_view(self) -> dict[str, Any]:
        """Return the stable fields exposed by AstraBox's catalog API."""

        return {
            "id": self.item_id,
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
        }


@dataclass(frozen=True)
class ExtensionCatalog:
    """The selectable MCP-server and Skill entries from one provider."""

    mcp_servers: tuple[ExtensionCatalogItem, ...] = ()
    skills: tuple[ExtensionCatalogItem, ...] = ()


@dataclass(frozen=True)
class MCPGatewayCredential:
    """What the egress sidecar attaches to one provider's MCP host.

    The sidecar attaches ``header`` itself on requests to ``base_url`` matching
    ``path_glob``; nothing about the credential enters the sandbox, not even a
    placeholder. The runtime reads this when it writes the sandbox's vault —
    never :meth:`ExtensionProvider.materialize`, which is why a runtime binding
    stays secret-free while the call still arrives authorized.

    ``value`` is the raw credential. ``Authorization`` is presented as a Bearer
    token; every other supported name is attached as that exact request header.
    """

    header: str
    value: str = field(repr=False)
    base_url: str
    path_glob: str = "/*/mcp"


@dataclass(frozen=True)
class RuntimeMCPServer:
    """One selected MCP server in the runtime's provider-neutral shape."""

    name: str
    transport: str
    url: str
    credential_target_url: str | None = None
    #: Fixed, non-secret headers the engine sends to this server. Provider and
    #: upstream credentials belong to the egress Vault, not this runtime shape.
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionRuntimeSelection:
    """Runtime material generated from selected catalog entries."""

    mcp_servers: tuple[RuntimeMCPServer, ...] = ()
    skill_descriptors: tuple[str, ...] = ()


class ExtensionProvider(ABC):
    """External catalog, runtime materialization, and gateway credential.

    Every catalog AstraBox can assign from is one of these, including the
    catalog AstraBox itself stores. The orchestrator holds no privileged path
    to any of them: it records which provider an assignment came from and asks
    that provider to resolve it, so a deployment's own MCP registry plugs in
    through the ``astrabox.providers.extensions`` entry point without changing
    resolution, assignment, or the console.
    """

    name: str
    is_default: bool = False

    @abstractmethod
    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        """Return the entries ``org_id`` may select, normalized to the contract.

        A provider whose catalog is not partitioned by tenant returns the same
        entries for every ``org_id``; one that stores its own records is
        expected to scope them, because an assignment carries no other
        tenancy check.
        """

    @abstractmethod
    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        """Translate selected entries into secret-free runtime bindings."""

    async def resolve_mcp_servers(
        self,
        *,
        org_id: str,
        item_ids: Sequence[str],
    ) -> tuple[RuntimeMCPServer, ...]:
        """Return runtime bindings for ``item_ids``, dropping ones now absent.

        Session start calls this per provider, so an Agent assigned nothing
        from a given provider never reaches it — which is what lets a
        deployment run one provider's catalog while another is unreachable.
        An entry that has been deleted or disabled upstream is omitted rather
        than raised: losing a server is the upstream's decision, and the
        session still starts with the rest.

        The default walks the catalog. A provider that can address entries
        directly should override this to avoid listing everything to select a
        few.
        """
        catalog = await self.list_catalog(org_id=org_id)
        by_id = {item.item_id: item for item in catalog.mcp_servers}
        selected = tuple(
            by_id[item_id] for item_id in item_ids if item_id in by_id
        )
        if not selected:
            return ()
        return self.materialize(mcp_servers=selected, skills=()).mcp_servers

    def mcp_gateway_credential(self) -> MCPGatewayCredential | None:
        """The credential the egress sidecar attaches to this provider's host.

        ``None`` means this provider's servers need no credential of its own —
        either they are open, or each one carries a credential AstraBox stores
        against its ``credential_target_url``.
        """

        return None


_PROVIDERS: dict[str, ExtensionProvider] = {}
_DEFAULT_PROVIDER: str | None = None


def register_extension_provider(provider: ExtensionProvider) -> None:
    """Register one extension provider; one provider may declare the default."""

    global _DEFAULT_PROVIDER
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("extension provider must have a non-empty name")
    _PROVIDERS[name] = provider
    if bool(getattr(provider, "is_default", False)):
        if _DEFAULT_PROVIDER not in {None, name}:
            raise RuntimeError(
                "multiple default extension providers registered: "
                f"{_DEFAULT_PROVIDER!r} and {name!r}"
            )
        _DEFAULT_PROVIDER = name


def default_extension_provider_name() -> str:
    """Return the declared default, or the sole provider, and fail on ambiguity."""

    if _DEFAULT_PROVIDER:
        return _DEFAULT_PROVIDER
    if len(_PROVIDERS) == 1:
        return next(iter(_PROVIDERS))
    raise RuntimeError(
        "extension provider name is required: "
        f"{len(_PROVIDERS)} registered ({sorted(_PROVIDERS)})"
    )


def extension_provider_for_name(name: str | None) -> ExtensionProvider:
    """Resolve one provider by name, using only an explicitly declared default."""

    wanted = str(name or "").strip().lower() or default_extension_provider_name()
    provider = _PROVIDERS.get(wanted)
    if provider is None:
        raise RuntimeError(
            f"no ExtensionProvider registered for name={wanted!r} "
            f"(registered: {sorted(_PROVIDERS)})"
        )
    return provider


def registered_extension_provider_names() -> list[str]:
    """Return registered provider names for diagnostics and configuration UI."""

    return sorted(_PROVIDERS)


__all__ = [
    "ExtensionCatalog",
    "ExtensionCatalogItem",
    "ExtensionProvider",
    "ExtensionRuntimeSelection",
    "MCPGatewayCredential",
    "RuntimeMCPServer",
    "default_extension_provider_name",
    "extension_provider_for_name",
    "register_extension_provider",
    "registered_extension_provider_names",
]
