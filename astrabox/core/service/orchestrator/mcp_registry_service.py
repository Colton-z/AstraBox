"""Administrator MCP registry and explicit Agent assignments."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    assignments_for_provider,
    replace_provider_assignments,
)
from astrabox.persistence.repository import AgentRepository, MCPServerRepository
from astrabox.providers.builtin_extensions import BuiltinExtensionProvider

#: The provider these records are assigned under. This service is the built-in
#: catalog's management surface; resolution reaches the same records through the
#: extension seam, so both sides must agree on the name.
BUILTIN_PROVIDER = BuiltinExtensionProvider.name

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_TRANSPORT_ALIASES = {
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "streamable-http": "streamable_http",
    "sse": "sse",
}


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


class MCPRegistryService:
    """Manage remote MCP metadata and the MCP ids selected for each Agent."""

    def __init__(
        self,
        *,
        repo: MCPServerRepository | None = None,
        agent_repo: AgentRepository | None = None,
    ) -> None:
        self._repo = repo or MCPServerRepository()
        self._agents = agent_repo or AgentRepository()

    async def create_server(
        self,
        user: UserContext,
        *,
        name: str,
        url: str,
        transport: str,
        description: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        org_id = self._org_id(user)
        normalized_name = self._normalize_name(name)
        if await self._repo.get_by_name(org_id=org_id, name=normalized_name):
            raise APIError(
                code="MCP_SERVER_NAME_EXISTS",
                message=f"an MCP server named '{normalized_name}' already exists",
                status_code=409,
            )
        doc = await self._repo.create(
            {
                "org_id": org_id,
                "name": normalized_name,
                "description": self._normalize_description(description),
                "url": self._normalize_url(url),
                "transport": self._normalize_transport(transport),
                "enabled": bool(enabled),
                "created_by": user.user_id,
                "updated_by": user.user_id,
            }
        )
        return self._view(doc)

    async def list_servers(self, user: UserContext) -> list[dict[str, Any]]:
        return [
            self._view(doc)
            for doc in await self._repo.list_for_org(org_id=self._org_id(user))
        ]

    async def get_server(
        self, user: UserContext, mcp_server_id: str
    ) -> dict[str, Any]:
        return self._view(await self._must_access(user, mcp_server_id))

    async def update_server(
        self,
        user: UserContext,
        mcp_server_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        current = await self._must_access(user, mcp_server_id)
        clean: dict[str, Any] = {}
        if "name" in updates:
            clean["name"] = self._normalize_name(updates.get("name"))
        if "url" in updates:
            clean["url"] = self._normalize_url(updates.get("url"))
        if "transport" in updates:
            clean["transport"] = self._normalize_transport(updates.get("transport"))
        if "description" in updates:
            clean["description"] = self._normalize_description(
                updates.get("description")
            )
        if "enabled" in updates:
            if not isinstance(updates.get("enabled"), bool):
                raise _invalid("enabled must be true or false")
            clean["enabled"] = updates["enabled"]
        if not clean:
            raise _invalid("provide at least one field to update")

        new_name = str(clean.get("name") or current.get("name") or "")
        if new_name != current.get("name"):
            duplicate = await self._repo.get_by_name(
                org_id=self._org_id(user), name=new_name
            )
            if duplicate and duplicate.get("mcp_server_id") != mcp_server_id:
                raise APIError(
                    code="MCP_SERVER_NAME_EXISTS",
                    message=f"an MCP server named '{new_name}' already exists",
                    status_code=409,
                )
        clean["updated_by"] = user.user_id
        updated = await self._repo.update(mcp_server_id, clean)
        if updated is None:
            raise self._not_found(mcp_server_id)
        return self._view(updated)

    async def delete_server(self, user: UserContext, mcp_server_id: str) -> None:
        await self._must_access(user, mcp_server_id)
        await self._repo.delete(mcp_server_id)

    async def get_agent_assignment(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        agent = await self._must_access_agent(user, agent_id)
        ids = assignments_for_provider(
            agent.get(AGENT_MCP_ASSIGNMENTS_FIELD), BUILTIN_PROVIDER
        )
        servers = await self._repo.list_by_ids(ids, org_id=self._org_id(user))
        by_id = {str(doc.get("mcp_server_id") or ""): doc for doc in servers}
        return {
            "agent_id": agent_id,
            "mcp_server_ids": ids,
            "mcp_servers": [
                self._view(by_id[mcp_id]) for mcp_id in ids if mcp_id in by_id
            ],
            "missing_mcp_server_ids": [mcp_id for mcp_id in ids if mcp_id not in by_id],
        }

    async def set_agent_assignment(
        self,
        user: UserContext,
        agent_id: str,
        mcp_server_ids: list[str],
    ) -> dict[str, Any]:
        ids = self._normalize_ids(mcp_server_ids)
        org_id = self._org_id(user)
        servers = await self._repo.list_by_ids(ids, org_id=org_id)
        found = {str(doc.get("mcp_server_id") or "") for doc in servers}
        missing = [mcp_id for mcp_id in ids if mcp_id not in found]
        if missing:
            raise _invalid(f"unknown MCP server ids: {missing}")

        for _ in range(3):
            agent = await self._must_access_agent(user, agent_id)
            version = self._version(agent)
            applied = await self._agents.compare_and_update_agent(
                agent_id,
                expected={"version": version},
                updates={
                    # Only the built-in rows are replaced. An Agent may also be
                    # assigned servers from another catalog, and this API does
                    # not show or own those.
                    AGENT_MCP_ASSIGNMENTS_FIELD: replace_provider_assignments(
                        agent.get(AGENT_MCP_ASSIGNMENTS_FIELD), BUILTIN_PROVIDER, ids
                    ),
                    "version": version + 1,
                    "updated_by": user.user_id,
                },
            )
            if applied:
                return await self.get_agent_assignment(user, agent_id)
        raise APIError(
            code="AGENT_VERSION_CONFLICT",
            message="agent was modified concurrently; retry the MCP assignment",
            status_code=409,
        )

    async def _must_access(
        self, user: UserContext, mcp_server_id: str
    ) -> dict[str, Any]:
        target = str(mcp_server_id or "").strip()
        doc = await self._repo.get(target) if target else None
        if not doc or str(doc.get("org_id") or "") != self._org_id(user):
            raise self._not_found(target)
        return doc

    async def _must_access_agent(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        target = str(agent_id or "").strip()
        agent = await self._agents.get_agent(target) if target else None
        if agent is None:
            raise APIError(
                code="AGENT_NOT_FOUND", message="agent not found", status_code=404
            )
        agent_org = str(agent.get("org_id") or "").strip() or default_org_id()
        if agent_org != self._org_id(user):
            raise APIError(
                code="AGENT_NOT_FOUND", message="agent not found", status_code=404
            )
        return agent

    @staticmethod
    def _org_id(user: UserContext) -> str:
        return str(getattr(user, "org_id", None) or "").strip() or default_org_id()

    @staticmethod
    def _normalize_name(raw: Any) -> str:
        name = str(raw or "").strip()
        if not _MCP_NAME_RE.fullmatch(name):
            raise _invalid(
                "name must be 1-63 characters using letters, numbers, '.', '_' or '-'"
            )
        return name

    @staticmethod
    def _normalize_description(raw: Any) -> str | None:
        if raw is None:
            return None
        value = str(raw).strip()
        if len(value) > 500:
            raise _invalid("description must be 500 characters or fewer")
        return value or None

    @staticmethod
    def _normalize_transport(raw: Any) -> str:
        transport = _TRANSPORT_ALIASES.get(str(raw or "").strip().lower())
        if transport is None:
            raise _invalid("transport must be 'streamable_http' or 'sse'")
        return transport

    @staticmethod
    def _normalize_url(raw: Any) -> str:
        value = str(raw or "").strip()
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise _invalid("url must be an absolute http or https URL")
        if parsed.username or parsed.password:
            raise _invalid("url must not contain a username or password; use Vault")
        if parsed.fragment:
            raise _invalid("url must not contain a fragment")
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path or "",
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _normalize_ids(raw: Any) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise _invalid("mcp_server_ids must be a list")
        result: list[str] = []
        seen: set[str] = set()
        for value in raw:
            mcp_id = str(value or "").strip()
            if not mcp_id or mcp_id in seen:
                continue
            seen.add(mcp_id)
            result.append(mcp_id)
        return result

    @staticmethod
    def _version(agent: dict[str, Any]) -> int:
        try:
            version = int(agent.get("version") or 1)
        except (TypeError, ValueError):
            return 1
        return max(version, 1)

    @staticmethod
    def _not_found(mcp_server_id: str) -> APIError:
        return APIError(
            code="MCP_SERVER_NOT_FOUND",
            message=f"MCP server '{mcp_server_id}' not found",
            status_code=404,
        )

    @staticmethod
    def _view(doc: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in doc.items() if not key.startswith("_")}


__all__ = ["MCPRegistryService"]
