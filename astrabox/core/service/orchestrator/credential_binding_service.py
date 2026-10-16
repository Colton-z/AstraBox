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
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.agent_repository import AgentReferenceMissing
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
    AssistantVaultMissing,
)
from astrabox.persistence.repository.assistant_defaults_repository import AssistantDefaultsRepository

#: The destructive Vault actions this service guards, and how each reads inside
#: the refusal. The caller supplies which one it is attempting so the operator
#: is told about the button they pressed, not about both.
_ACTION_GERUNDS = {"archive": "archiving", "delete": "deleting"}

#: How many holders the message names before it summarises the rest. The full
#: set is always in ``data.holders``; this only keeps the sentence readable.
_HOLDERS_NAMED = 3

#: How many times an Agent binding re-reads after another edit moved the
#: Agent's version before it gives up.
_BINDING_WRITE_ATTEMPTS = 3

logger = get_logger(__name__)


class CredentialBindingService:
    """Persist admin policy; never consult the conversation user's identity."""

    def __init__(
        self,
        *,
        vault_service: VaultService | None = None,
        agent_repo: AgentRepository | None = None,
        assistant_repo: AssistantCatalogRepository | None = None,
        defaults_repo: AssistantDefaultsRepository | None = None,
        schedule_runtime_reconciliation: Callable[[str], object] | None = None,
    ) -> None:
        self._vaults = vault_service or VaultService()
        self._agents = agent_repo or AgentRepository()
        self._assistants = assistant_repo or AssistantCatalogRepository()
        self._defaults = defaults_repo or AssistantDefaultsRepository()
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
        """Bind exactly ``vault_ids``, in order, to an Agent.

        The binding is part of the Agent's definition, so a changed list moves
        its version. The request replaces the list whole, so a write that loses
        the version race to another edit re-reads and applies it again.
        """
        agent = await self._agents.get_agent(agent_id)
        if agent is None:
            raise APIError(
                code="AGENT_NOT_FOUND", message="agent not found", status_code=404
            )
        cleaned = await self._vaults.validate_binding_ids(
            user, vault_ids, target_type="agent"
        )
        for _ in range(_BINDING_WRITE_ATTEMPTS):
            if self._ids(agent) == cleaned:
                break
            try:
                written = await self._agents.update_definition(
                    agent_id,
                    expected_version=int(agent["version"]),
                    changes={"credential_vault_ids": cleaned},
                    updated_by=user.user_id,
                )
            except AgentReferenceMissing:
                raise APIError(
                    code="VAULT_NOT_FOUND",
                    message="a vault this binding names does not exist",
                    status_code=404,
                ) from None
            if written is not None:
                break
            agent = await self._agents.get_agent(agent_id)
            if agent is None:
                raise APIError(
                    code="AGENT_NOT_FOUND", message="agent not found", status_code=404
                )
        else:
            raise APIError(
                code="AGENT_VERSION_CONFLICT",
                message="agent was modified concurrently; retry the vault binding",
                status_code=409,
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
        assistant = await self._assistants.get_assistant(assistant_id)
        if assistant is None:
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message="assistant not found",
                status_code=404,
            )
        cleaned = await self._vaults.validate_binding_ids(
            user, vault_ids, target_type="assistant"
        )
        # The binding is authored: a compare-and-set on the version read above,
        # and a changed list moves the Assistant's updated_at.
        try:
            bound = await self._assistants.set_vault_bindings(
                assistant_id,
                expected_version=int(assistant["version"]),
                vault_ids=cleaned,
                updated_by=user.user_id,
                touch=self._ids(assistant) != cleaned,
            )
        except AssistantVaultMissing:
            raise APIError(
                code="VAULT_NOT_FOUND",
                message="a Credential Vault in the binding was deleted while it was saved",
                status_code=404,
            ) from None
        if not bound:
            raise APIError(
                code="ASSISTANT_VERSION_CONFLICT",
                message=(
                    f"assistant={assistant_id} was modified concurrently; retry with "
                    "the latest version"
                ),
                status_code=409,
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
        agents, assistants, layers = await asyncio.gather(
            self._agents.list_agents_by_credential_vault_id(target),
            self._assistants.list_assistants_by_credential_vault_id(target),
            self._defaults.list_by_credential_vault_id(target),
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
        bindings.extend(
            {**self._defaults_holder(layer), "vault_ids": self._ids(layer)} for layer in layers
        )
        return bindings

    async def archive_vault(self, user: UserContext, vault_id: str) -> dict[str, Any]:
        """Archive a Vault that no managed runtime uses.

        The use check (:meth:`ensure_vault_unbound`) runs inside the archive's
        transaction while the Vault's row is locked, after the Vault is found
        in the caller's organization, so a cross-organization id never reveals
        whether another organization's runtime uses it.
        """
        target = str(vault_id or "").strip()
        return await self._vaults.archive_vault(
            user,
            target,
            ensure_unbound=lambda: self.ensure_vault_unbound(target, action="archive"),
        )

    async def delete_vault(self, user: UserContext, vault_id: str) -> None:
        """Delete a Vault that no managed runtime uses; checked as in :meth:`archive_vault`."""
        target = str(vault_id or "").strip()
        await self._vaults.delete_vault(
            user,
            target,
            ensure_unbound=lambda: self.ensure_vault_unbound(target, action="delete"),
        )

    @staticmethod
    def _defaults_holder(layer: dict[str, Any]) -> dict[str, str]:
        """A layer of the Assistant defaults, which every Assistant beneath it uses."""

        return {
            "target_type": "assistant_defaults",
            "target_id": str(layer.get("scope") or ""),
            "target_name": "Assistant defaults",
        }

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
        holders.extend(
            self._defaults_holder(layer)
            for layer in await self._defaults.list_by_credential_vault_id(target)
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
