"""AstraBox's own MCP registry, offered through the extension seam.

This is the catalog for a deployment that runs no external extension service:
records an administrator enters through ``/api/v1/admin/mcp-servers``. The
sandbox engine calls each resolved URL directly and its egress sidecar attaches
any matching Vault credential. It is a provider like any other — the orchestrator reaches it through
:class:`~astrabox.seams.extensions.ExtensionProvider` rather than by reading its
collection directly, so it carries no privilege a deployment's own registry
could not also have.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrabox.persistence.repository.mcp_server_repository import MCPServerRepository
from astrabox.seams.extensions import (
    ExtensionCatalog,
    ExtensionCatalogItem,
    ExtensionProvider,
    ExtensionRuntimeSelection,
    RuntimeMCPServer,
    register_extension_provider,
)

#: The transports bundled engines can call directly. A record naming anything
#: else cannot be materialized, so it is not offered.
_SUPPORTED_TRANSPORTS = frozenset({"streamable_http", "sse"})


def _catalog_item(doc: dict[str, Any]) -> ExtensionCatalogItem | None:
    """Normalize one stored record, or ``None`` when it cannot be called."""

    item_id = str(doc.get("mcp_server_id") or "").strip()
    name = str(doc.get("name") or "").strip()
    url = str(doc.get("url") or "").strip()
    transport = str(doc.get("transport") or "").strip()
    if not item_id or not name or not url or transport not in _SUPPORTED_TRANSPORTS:
        return None
    return ExtensionCatalogItem(
        item_id=item_id,
        name=name,
        description=str(doc.get("description") or "").strip() or None,
        transport=transport,
        provider_data={"url": url},
    )


class BuiltinExtensionProvider(ExtensionProvider):
    """Serve the administrator-registered MCP records AstraBox stores itself."""

    name = "builtin"

    def __init__(self, *, repo: MCPServerRepository | None = None) -> None:
        self._repo = repo or MCPServerRepository()

    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        docs = await self._repo.list_for_org(org_id=org_id)
        items = [
            item
            for item in (
                _catalog_item(doc) for doc in docs if doc.get("enabled") is not False
            )
            if item is not None
        ]
        # The registry holds MCP records only. Skills reach the runtime through
        # the Git materializer, which is not a catalog this provider serves.
        return ExtensionCatalog(mcp_servers=tuple(items), skills=())

    async def resolve_mcp_servers(
        self,
        *,
        org_id: str,
        item_ids: Sequence[str],
    ) -> tuple[RuntimeMCPServer, ...]:
        ids = [str(value or "").strip() for value in item_ids]
        ids = [value for value in ids if value]
        if not ids:
            return ()
        # Addressed directly rather than through list_catalog: this runs at
        # every Session start, and the org filter here is what stops one
        # tenant's Agent from resolving another tenant's record.
        docs = await self._repo.list_by_ids(ids, org_id=org_id)
        by_id = {str(doc.get("mcp_server_id") or ""): doc for doc in docs}
        selected: list[ExtensionCatalogItem] = []
        for item_id in ids:
            doc = by_id.get(item_id)
            # This read decides the immutable extension snapshot a new Session
            # gets. Disabling a record excludes subsequent Sessions; a running
            # Session keeps the direct URL captured when it was created.
            if doc is None or doc.get("enabled") is False:
                continue
            item = _catalog_item(doc)
            if item is not None:
                selected.append(item)
        if not selected:
            return ()
        return self.materialize(mcp_servers=tuple(selected), skills=()).mcp_servers

    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        _ = skills
        return ExtensionRuntimeSelection(
            mcp_servers=tuple(
                RuntimeMCPServer(
                    name=item.name,
                    transport=str(item.transport or ""),
                    url=str(item.provider_data.get("url") or ""),
                    credential_target_url=(
                        str(item.provider_data.get("url") or "").strip() or None
                    ),
                    # No headers: a record AstraBox stores carries its upstream
                    # credential in the Session's Vault, keyed by the same URL,
                    # and the egress sidecar attaches it. There is no gateway of
                    # this provider's own to authorize against.
                )
                for item in mcp_servers
            ),
            skill_descriptors=(),
        )


_PROVIDER = BuiltinExtensionProvider()
register_extension_provider(_PROVIDER)


__all__ = ["BuiltinExtensionProvider"]
