"""Extension catalog and runtime-binding seam.

An extension provider owns the external catalog protocol, its management
surface, and the credential its own MCP gateway requires. The orchestrator
owns Agent authorization and persists only the provider-neutral selection
returned by this contract; the credential reaches the wire through the
sandbox's egress vault, so no selection ever carries a secret.

A deployment selects the provider whose catalog the console offers with
``ASTRABOX_EXTENSION_PROVIDER`` (:func:`selected_extension_provider`). The value
:data:`EXTENSION_PROVIDER_NONE` runs no catalog at all: Agents and Assistants
then use only the MCP servers, Skills and packages written on them, and a
catalog selection stored earlier is refused when it is used rather than left
out.

Every provider declares :class:`ExtensionProviderFeatures`: the name the
console gives its catalog, and whether it opens a management console of its own
for an Agent (:meth:`ExtensionProvider.open_management_console`). The console
renders both from the declaration, so no provider's name or console is assumed.

This module is import-light so an external provider distribution can implement
the contract without importing AstraBox's API or service layers;
``UserContext`` is referenced only under ``TYPE_CHECKING``.
"""

from __future__ import annotations

from astrabox.seams.skills import SkillDescriptor

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing-only import, no runtime dependency
    from astrabox.common.utils.user_context import UserContext


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
    skill_descriptors: tuple[SkillDescriptor, ...] = ()


@dataclass(frozen=True)
class ExtensionProviderFeatures:
    """What a provider tells the console about its catalog.

    ``display_name`` names the catalog wherever the console shows its entries or
    a way into it ("MCP servers from LiteLLM"); it is shown as written, in every
    language. ``management_console`` says the provider opens a management
    console of its own for one Agent through
    :meth:`ExtensionProvider.open_management_console`; the console offers that
    entry only when it is true.
    """

    display_name: str
    management_console: bool = False


@dataclass(frozen=True)
class ManagementConsoleCookie:
    """A cookie the browser carries into a provider's management console.

    Set HttpOnly and SameSite=Lax, and Secure when the request came over HTTPS.
    """

    name: str
    value: str = field(repr=False)
    max_age: int
    path: str = "/"


@dataclass(frozen=True)
class ManagementConsoleSession:
    """Where a browser opens one Agent's management console, and for how long.

    ``open_url`` is a same-origin path, opened in a tab of its own.
    ``cookies`` are set on the response that hands ``open_url`` out; the body
    carries no credential.
    """

    open_url: str
    expires_in: int
    cookies: tuple[ManagementConsoleCookie, ...] = ()


#: The kind of entry a management console is opened for.
ManagementConsoleSection = Literal["mcp", "skills"]


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
    #: What the console shows for this provider's catalog. Required:
    #: :func:`register_extension_provider` refuses a provider without it.
    features: ExtensionProviderFeatures
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

    async def open_management_console(
        self,
        *,
        user: UserContext,
        agent_id: str,
        section: ManagementConsoleSection,
    ) -> ManagementConsoleSession:
        """Open this provider's management console for one Agent.

        Called only for the selected provider, only when its features declare
        ``management_console``, and only after ``user`` has been authorized to
        manage ``agent_id``. ``section`` is the kind of entry the person asked
        to manage. The console the session opens must apply the same Agent
        authorization to every request it serves; a hidden link is not a check.
        """

        _ = (user, agent_id, section)
        raise NotImplementedError(
            f"extension provider {self.name!r} has no management console"
        )


#: The ``ASTRABOX_EXTENSION_PROVIDER`` value that runs no extension catalog.
EXTENSION_PROVIDER_NONE = "none"

_PROVIDERS: dict[str, ExtensionProvider] = {}
_DEFAULT_PROVIDER: str | None = None


def register_extension_provider(provider: ExtensionProvider) -> None:
    """Register one extension provider; one provider may declare the default.

    Refuses a provider whose :class:`ExtensionProviderFeatures` are missing or
    name no catalog, and one that declares a management console without
    implementing :meth:`ExtensionProvider.open_management_console`, so the
    console never offers what the provider cannot open.
    """

    global _DEFAULT_PROVIDER
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("extension provider must have a non-empty name")
    if name == EXTENSION_PROVIDER_NONE:
        raise RuntimeError(
            f"extension provider name {EXTENSION_PROVIDER_NONE!r} is reserved: "
            "ASTRABOX_EXTENSION_PROVIDER uses it to run no extension catalog"
        )
    features = getattr(provider, "features", None)
    if not isinstance(features, ExtensionProviderFeatures):
        raise RuntimeError(
            f"extension provider {name!r} must declare features as "
            "ExtensionProviderFeatures"
        )
    if not str(features.display_name or "").strip():
        raise RuntimeError(f"extension provider {name!r} must declare a display_name")
    opens = getattr(type(provider), "open_management_console", None)
    if features.management_console and (
        opens is None or opens is ExtensionProvider.open_management_console
    ):
        raise RuntimeError(
            f"extension provider {name!r} declares a management console but does "
            "not implement open_management_console"
        )
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


def selected_extension_provider(setting: str | None) -> ExtensionProvider | None:
    """The provider an ``ASTRABOX_EXTENSION_PROVIDER`` value selects.

    ``None`` for :data:`EXTENSION_PROVIDER_NONE`: the deployment runs no
    extension catalog. An empty value selects the declared default, as
    :func:`extension_provider_for_name` does. Raises ``RuntimeError`` for a
    name no installed provider registers, which bootstrap reports at startup.
    """

    wanted = str(setting or "").strip().lower()
    if wanted == EXTENSION_PROVIDER_NONE:
        return None
    return extension_provider_for_name(wanted)


__all__ = [
    "EXTENSION_PROVIDER_NONE",
    "ExtensionCatalog",
    "ExtensionCatalogItem",
    "ExtensionProvider",
    "ExtensionProviderFeatures",
    "ExtensionRuntimeSelection",
    "MCPGatewayCredential",
    "ManagementConsoleCookie",
    "ManagementConsoleSection",
    "ManagementConsoleSession",
    "RuntimeMCPServer",
    "default_extension_provider_name",
    "extension_provider_for_name",
    "register_extension_provider",
    "registered_extension_provider_names",
    "selected_extension_provider",
]
