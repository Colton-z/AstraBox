"""Administrator-owned bindings from managed runtimes to Credential Vaults."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.vault_service import VaultService
from astrabox.persistence.repository import AgentRepository
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)

#: The destructive Vault actions this service guards, and how each reads inside
#: the refusal. The caller supplies which one it is attempting so the operator
#: is told about the button they pressed, not about both.
_ACTION_GERUNDS = {"archive": "archiving", "delete": "deleting"}

#: How many holders the message names before it summarises the rest. The full
#: set is always in ``data.holders``; this only keeps the sentence readable.
_HOLDERS_NAMED = 3

logger = get_logger(__name__)


class CredentialBindingService:
    """Persist admin policy; never consult the conversation user's identity."""

    def __init__(
        self,
        *,
        vault_service: VaultService | None = None,
        agent_repo: AgentRepository | None = None,
        assistant_repo: AssistantCatalogRepository | None = None,
        schedule_runtime_reconciliation: Callable[[str], object] | None = None,
    ) -> None:
        self._vaults = vault_service or VaultService()
        self._agents = agent_repo or AgentRepository()
        self._assistants = assistant_repo or AssistantCatalogRepository()
        self._schedule_runtime_reconciliation = schedule_runtime_reconciliation

    async def get_agent_binding(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        agent = await self._agents.get_agent(agent_id)
        if agent is None:
            raise APIError(
                code="AGENT_NOT_FOUND", message="agent not found", status_code=404
            )
        return await self._view(
            user,
            target_type="agent",
            target_id=agent_id,
            vault_ids=self._ids(agent),
        )

    async def set_agent_binding(
        self, user: UserContext, agent_id: str, vault_ids: list[str]
    ) -> dict[str, Any]:
        if await self._agents.get_agent(agent_id) is None:
            raise APIError(
                code="AGENT_NOT_FOUND", message="agent not found", status_code=404
            )
        cleaned = await self._vaults.validate_binding_ids(
            user, vault_ids, target_type="agent"
        )
        await self._agents.update_agent(
            agent_id,
            {
                "credential_vault_ids": cleaned,
                "credentials_updated_by": user.user_id,
                "credentials_updated_at": utcnow_iso(),
            },
        )
        # Binding writes use the same preparation entry as Agent and extension
        # writes. The supplier's existing pool still holds its original recipe.
        try:
            trigger = self._schedule_runtime_reconciliation
            if trigger is None:
                from astrabox.core.service.orchestrator.service_registry import get_agent_service

                trigger = get_agent_service().schedule_runtime_reconciliation
            trigger(agent_id)
        except Exception:
            logger.exception(
                "could not schedule Agent runtime reconciliation after Vault assignment: agent=%s",
                agent_id,
            )
        return await self._view(
            user,
            target_type="agent",
            target_id=agent_id,
            vault_ids=cleaned,
        )

    async def get_assistant_binding(
        self, user: UserContext, assistant_id: str
    ) -> dict[str, Any]:
        assistant = await self._assistants.get_assistant(assistant_id)
        if assistant is None:
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message="assistant not found",
                status_code=404,
            )
        return await self._view(
            user,
            target_type="assistant",
            target_id=assistant_id,
            vault_ids=self._ids(assistant),
        )

    async def set_assistant_binding(
        self, user: UserContext, assistant_id: str, vault_ids: list[str]
    ) -> dict[str, Any]:
        if await self._assistants.get_assistant(assistant_id) is None:
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message="assistant not found",
                status_code=404,
            )
        cleaned = await self._vaults.validate_binding_ids(
            user, vault_ids, target_type="assistant"
        )
        await self._assistants.update_assistant(
            assistant_id,
            {
                "credential_vault_ids": cleaned,
                "credentials_updated_by": user.user_id,
                "credentials_updated_at": utcnow_iso(),
            },
        )
        return await self._view(
            user,
            target_type="assistant",
            target_id=assistant_id,
            vault_ids=cleaned,
        )

    async def list_vault_bindings(
        self, user: UserContext, vault_id: str
    ) -> list[dict[str, Any]]:
        """List every target bound to one authorized Vault in two queries."""

        target = str(vault_id or "").strip()
        await self._vaults.get_vault(user, target)
        agents, assistants = await asyncio.gather(
            self._agents.list_agents_by_credential_vault_id(target),
            self._assistants.list_assistants_by_credential_vault_id(target),
        )
        bindings = [
            {
                "target_type": "agent",
                "target_id": str(agent.get("agent_id") or ""),
                "target_name": str(
                    (agent.get("display_meta") or {}).get("display_name")
                    or agent.get("name")
                    or ""
                ),
                "vault_ids": self._ids(agent),
            }
            for agent in agents
        ]
        bindings.extend(
            {
                "target_type": "assistant",
                "target_id": str(assistant.get("assistant_id") or ""),
                "target_name": str(
                    assistant.get("display_name") or assistant.get("name") or ""
                ),
                "vault_ids": self._ids(assistant),
            }
            for assistant in assistants
        )
        return bindings

    async def ensure_vault_unbound(self, vault_id: str, *, action: str) -> None:
        """Refuse a destructive Vault action while a managed runtime uses it.

        ``action`` is what the caller is attempting, so the refusal can name the
        one operation the operator asked for instead of listing the ones it
        might have been.
        """
        target = str(vault_id or "").strip()
        holders: list[dict[str, str]] = [
            {
                "target_type": "agent",
                "target_id": str(agent.get("agent_id") or ""),
                # An Agent's display name lives under display_meta — the schema
                # writes it there (agent_schema: path "display_meta.display_name")
                # and resolution reads it there. An Assistant keeps its own at the
                # top level, which is why the two branches differ.
                "target_name": str(
                    (agent.get("display_meta") or {}).get("display_name")
                    or agent.get("name")
                    or ""
                ),
            }
            for agent in await self._agents.list_agents_by_credential_vault_id(target)
        ]
        holders.extend(
            {
                "target_type": "assistant",
                "target_id": str(assistant.get("assistant_id") or ""),
                "target_name": str(
                    assistant.get("display_name") or assistant.get("name") or ""
                ),
            }
            for assistant in await self._assistants.list_assistants_by_credential_vault_id(
                target
            )
        )
        if holders:
            self._raise_in_use(holders=holders, action=action)

    async def _view(
        self,
        user: UserContext,
        *,
        target_type: str,
        target_id: str,
        vault_ids: list[str],
    ) -> dict[str, Any]:
        return {
            "target_type": target_type,
            "target_id": target_id,
            "vault_ids": list(vault_ids),
            "vaults": await self._vaults.describe_binding(user, vault_ids),
        }

    @staticmethod
    def _ids(doc: dict[str, Any]) -> list[str]:
        raw = doc.get("credential_vault_ids")
        if not isinstance(raw, list):
            return []
        return [str(value).strip() for value in raw if str(value or "").strip()]

    @staticmethod
    def _raise_in_use(*, holders: list[dict[str, str]], action: str) -> None:
        """Refuse once, naming every holder and the operation that was refused.

        Naming a subset would make the operator unbind, retry, and be refused
        again for the next one. The message carries a readable head of the list
        so it stays a sentence; ``data.holders`` carries all of them for a
        client that wants to render or act on the set.
        """
        gerund = _ACTION_GERUNDS.get(action)
        if gerund is None:  # a caller passed an action this message cannot name
            raise RuntimeError(
                f"unknown Vault action {action!r}; expected one of "
                f"{sorted(_ACTION_GERUNDS)}"
            )
        described = [
            f"{holder['target_type']} '{holder['target_name'] or holder['target_id']}'"
            for holder in holders
        ]
        if len(described) == 1:
            assignment, pronoun = described[0], "it"
        else:
            shown = described[:_HOLDERS_NAMED]
            remaining = len(described) - len(shown)
            listed = ", ".join(shown)
            if remaining:
                listed = f"{listed}, and {remaining} more"
            assignment, pronoun = f"{len(described)} targets: {listed}", "them"
        raise APIError(
            code="VAULT_IN_USE",
            message=(
                f"Credential Vault is still assigned to {assignment}. "
                f"Unbind {pronoun} before {gerund} the Vault."
            ),
            status_code=409,
            data={"action": action, "holders": list(holders)},
        )
