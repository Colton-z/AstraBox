"""The Agent or Assistant a trigger starts conversations on.

A trigger row (a Deployment, a channel work item, a channel conversation
mapping) names exactly one target: ``agent_id`` or ``assistant_id``. Triggers
resolve that target here and start conversations through
:meth:`ConversationTargets.start_conversation`, so channel, schedule and
webhook code never branch on the target kind.

An Agent conversation runs as the initiating user, a platform binding's Agent
owner, or an administrator-chosen execution account. An Assistant is owner-only (docs/domain-model.md §2), so its
conversations always run as its owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)

TARGET_AGENT = "agent"
TARGET_ASSISTANT = "assistant"
_TARGET_FIELDS = {TARGET_AGENT: "agent_id", TARGET_ASSISTANT: "assistant_id"}


def target_ref(row: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return ``(kind, id)`` for the one target a row names, else ``None``.

    A row naming both an Agent and an Assistant is malformed and names no
    target.
    """

    named = [
        (kind, str((row or {}).get(field) or "").strip())
        for kind, field in _TARGET_FIELDS.items()
    ]
    named = [(kind, target_id) for kind, target_id in named if target_id]
    return named[0] if len(named) == 1 else None


def target_fields(kind: str, target_id: str) -> dict[str, str]:
    """The row field that stores a target, e.g. ``{"assistant_id": ...}``."""

    return {_TARGET_FIELDS[kind]: target_id}


@dataclass(frozen=True)
class ConversationTarget:
    """A resolved trigger target and the account its work runs as by default."""

    kind: str
    target_id: str
    owner_user_id: str
    name: str

    @property
    def fields(self) -> dict[str, str]:
        return target_fields(self.kind, self.target_id)


class ConversationTargets:
    """Resolve trigger targets and start their conversations."""

    def __init__(
        self,
        *,
        agent_repo: Any,
        agent_service_getter: Callable[[], Any],
        assistant_service_getter: Callable[[], Any] | None,
        assistant_catalog_repo: AssistantCatalogRepository | None = None,
    ) -> None:
        self._agent_repo = agent_repo
        self._agent_service_getter = agent_service_getter
        self._assistant_service_getter = assistant_service_getter
        self._assistant_catalog_repo = assistant_catalog_repo or AssistantCatalogRepository()

    async def resolve(self, row: dict[str, Any] | None) -> ConversationTarget | None:
        """Resolve the target a row names; ``None`` when it is missing or deleted."""

        ref = target_ref(row)
        if ref is None:
            return None
        kind, target_id = ref
        if kind == TARGET_AGENT:
            agent = await self._agent_repo.get_agent(target_id)
            if not agent:
                return None
            return ConversationTarget(
                kind=kind,
                target_id=target_id,
                owner_user_id=str(agent.get("user_id") or "").strip(),
                name=str(agent.get("name") or target_id),
            )
        assistant = await self._assistant_catalog_repo.get_assistant(target_id)
        if not assistant:
            return None
        return ConversationTarget(
            kind=kind,
            target_id=target_id,
            owner_user_id=str(assistant.get("owner_id") or "").strip(),
            name=str(assistant.get("display_name") or target_id),
        )

    async def may_start_as(self, target: ConversationTarget, user_id: str) -> bool:
        """Whether ``user_id`` may start a conversation on ``target`` by its own rule.

        An Agent applies its access rule to the account without platform
        roles: a messaging participant has not signed in, so only the Agent's
        own audience counts — everyone for a public Agent, otherwise its
        creator, its administrators and its allow-list. An Assistant is its
        owner's alone.
        """
        if target.kind == TARGET_AGENT:
            agent = await self._agent_repo.get_agent(target.target_id)
            return bool(agent) and can_view_agent(agent, user_id, [])
        return user_id == target.owner_user_id

    async def start_conversation(
        self,
        target: ConversationTarget,
        user: UserContext,
        *,
        idempotency_key: str | None = None,
        source_type: str | None = None,
        execution_account: bool = False,
    ) -> dict[str, Any]:
        """Start one conversation on ``target`` as ``user``.

        ``execution_account`` marks an administrator-configured channel
        execution account; only Agents accept one. An Assistant conversation
        is refused unless ``user`` is the Assistant's owner.
        """

        if target.kind == TARGET_AGENT:
            agent_service = self._agent_service_getter()
            if execution_account:
                return await agent_service.start_integration_conversation(
                    user, target.target_id
                )
            options: dict[str, Any] = {}
            if idempotency_key is not None:
                options["idempotency_key"] = idempotency_key
            if source_type is not None:
                options["source_type"] = source_type
            return await agent_service.start_conversation(user, target.target_id, **options)
        if execution_account or user.user_id != target.owner_user_id:
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message=f"assistant={target.target_id} not found",
                status_code=404,
            )
        if self._assistant_service_getter is None:
            raise RuntimeError("Assistant conversations are not available in this process")
        return await self._assistant_service_getter().start_conversation(
            user, target.target_id, idempotency_key=idempotency_key
        )
