"""Assistant-owned credentials accepted only by the platform's A2A ingress."""

from __future__ import annotations

from starlette.exceptions import HTTPException

from astrabox.common.utils.user_context import API_READ_SCOPE, API_WRITE_SCOPE, UserContext
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_platform_mcp_capability,
    verify_platform_mcp_capability,
)
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.repository.platform_mcp_binding_repository import PlatformMCPBindingRepository


def mint_assistant_a2a_capability(binding_id: str, agent_id: str) -> str:
    """Bind a runtime credential to one Assistant workspace and one target Agent."""
    if not agent_id:
        raise ValueError("Assistant A2A capability requires a target Agent")
    return mint_platform_mcp_capability(binding_id, f"a2a:{agent_id}")


async def resolve_assistant_a2a_user(
    binding_id: str, agent_id: str, authorization: str,
) -> UserContext:
    """Resolve the persisted owner after checking a target-bound capability.

    This credential confers no administrator role and cannot name an execution
    user. The Agent service checks the owner's current target access on each call.
    """
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not verify_platform_mcp_capability(
        binding_id, f"a2a:{agent_id}", token.strip(),
    ):
        raise HTTPException(401, "Invalid Assistant A2A credential")
    binding = await PlatformMCPBindingRepository().get_binding(binding_id)
    if not binding or binding.get("scope_kind") != "assistant_workspace":
        raise HTTPException(401, "Assistant workspace is unavailable")
    assistant_id = str(binding.get("assistant_id") or "")
    owner_id = str(binding.get("user_id") or "")
    assistant = await AssistantCatalogRepository().get_assistant(assistant_id)
    if not owner_id or not assistant or assistant.get("owner_id") != owner_id:
        raise HTTPException(401, "Assistant owner is unavailable")
    return UserContext(
        owner_id, org_id=assistant.get("org_id"),
        api_scopes=[API_READ_SCOPE, API_WRITE_SCOPE],
    )
