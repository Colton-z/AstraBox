"""Webhook management stays inside the agent named by the route.

The management endpoints are nested under ``/agents/{agent_id}``.
Authorizing that agent must not grant mutation rights over a webhook that
belongs to a different agent merely because its id is known.  The write
fence is repeated atomically in the repository so deletion/update races cannot
turn the service's visibility check into a TOCTOU authorization gap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.providers import channel_generic  # noqa: F401  (registers generic_json)


class _AtomicWebhookRepo:
    """Small stateful stand-in with the repository's guarded-write semantics."""

    def __init__(self, webhook: dict[str, Any] | None) -> None:
        self.webhook = dict(webhook) if webhook is not None else None
        self.delete_before_update = False
        self.get_for_deployment = AsyncMock(side_effect=self._get_for_deployment)
        self.update_for_deployment = AsyncMock(side_effect=self._update_for_deployment)
        self.delete_for_deployment = AsyncMock(side_effect=self._delete_for_deployment)

    def _matches(
        self, deployment_id: str, agent_id: str, owner_user_id: str, owner_org_id: str
    ) -> bool:
        row = self.webhook or {}
        # The repository fences a personal row by its owner and every other
        # row by not being personal.
        scope_matches = (
            row.get("scope") == "personal"
            and (row.get("owner_user_id"), row.get("owner_org_id"))
            == (owner_user_id, owner_org_id)
            if owner_user_id
            else row.get("scope") != "personal"
        )
        return bool(
            row
            and row.get("deployment_id") == deployment_id
            and row.get("agent_id") == agent_id
            and scope_matches
        )

    async def _get_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        *,
        assistant_id: str = "",
        owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> dict[str, Any] | None:
        if not self._matches(deployment_id, agent_id, owner_user_id, owner_org_id):
            return None
        return dict(self.webhook)  # type: ignore[arg-type]

    async def _update_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        updates: dict[str, Any],
        *,
        assistant_id: str = "",
        owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> dict[str, Any] | None:
        if self.delete_before_update:
            self.webhook = None
        if not self._matches(deployment_id, agent_id, owner_user_id, owner_org_id):
            return None
        self.webhook.update(updates)  # type: ignore[union-attr]
        return dict(self.webhook)  # type: ignore[arg-type]

    async def _delete_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        *,
        assistant_id: str = "",
        owner_user_id: str = "",
        owner_org_id: str = "",
    ) -> bool:
        if not self._matches(deployment_id, agent_id, owner_user_id, owner_org_id):
            return False
        self.webhook = None
        return True


def _service(
    webhook: dict[str, Any] | None,
) -> tuple[DeploymentService, _AtomicWebhookRepo]:
    deployment_repo = _AtomicWebhookRepo(webhook)
    service = DeploymentService(
        deployment_repo=deployment_repo,  # type: ignore[arg-type]
        agent_repo=AsyncMock(),
        agent_service_getter=lambda: AsyncMock(),
        sessions_repo=AsyncMock(),
        session_runtime=AsyncMock(),
        session_facts=AsyncMock(),
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),  # management paths never touch the spine
    )
    return service, deployment_repo


async def test_cross_agent_listing_batches_authorization_without_n_plus_one() -> None:
    deployment_repo = AsyncMock()
    deployment_repo.list_active.return_value = [
        {
            "deployment_id": "dep-mine",
            "agent_id": "agent-mine",
            "secret": "write-only",
        },
        {
            "deployment_id": "dep-foreign",
            "agent_id": "agent-foreign",
        },
    ]
    agent_repo = AsyncMock()
    agent_repo.list_agents_by_ids.return_value = {
        "agent-mine": {
            "agent_id": "agent-mine",
            "name": "Mine",
            "created_by": "owner-1",
        },
        "agent-foreign": {
            "agent_id": "agent-foreign",
            "name": "Foreign",
            "created_by": "owner-2",
        },
    }
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: AsyncMock(),
        sessions_repo=AsyncMock(),
        session_runtime=AsyncMock(),
        session_facts=AsyncMock(),
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),
    )

    listed = await service.list_manageable(UserContext(user_id="owner-1"))

    assert listed == [
        {
            "deployment_id": "dep-mine",
            "agent_id": "agent-mine",
            "agent_name": "Mine",
        }
    ]
    deployment_repo.list_active.assert_awaited_once_with()
    agent_repo.list_agents_by_ids.assert_awaited_once_with(
        ["agent-mine", "agent-foreign"]
    )
    agent_repo.get_agent.assert_not_awaited()


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    ("webhook", "route_agent", "case"),
    [
        (None, "agent-1", "missing"),
        (
            {
                "deployment_id": "wh-1",
                "agent_id": "agent-victim",
                "enabled": True,
            },
            "agent-attacker",
            "foreign",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_deployment_mutation_hides_every_invisible_resource(
    operation: str,
    webhook: dict[str, Any] | None,
    route_agent: str,
    case: str,
) -> None:
    del case  # parametrization id documents the invisible-resource class
    service, deployment_repo = _service(webhook)
    before = dict(deployment_repo.webhook) if deployment_repo.webhook is not None else None

    with pytest.raises(APIError) as exc:
        if operation == "update":
            await service.update(
                "wh-1",
                agent_id=route_agent,
                patch={"enabled": False},
            )
        else:
            await service.delete("wh-1", agent_id=route_agent)

    assert exc.value.code == "NOT_FOUND"
    assert exc.value.message == "deployment not found"
    assert exc.value.status_code == 404
    assert deployment_repo.webhook == before


async def test_update_fails_closed_if_delete_wins_after_visibility_read() -> None:
    service, deployment_repo = _service(
        {
            "deployment_id": "wh-1",
            "agent_id": "agent-1",
            "scene": "hmac",
            "enabled": True,
        }
    )
    deployment_repo.delete_before_update = True

    with pytest.raises(APIError) as exc:
        await service.update(
            "wh-1", agent_id="agent-1", patch={"enabled": False}
        )

    assert exc.value.code == "NOT_FOUND" and exc.value.status_code == 404
    assert deployment_repo.webhook is None


async def test_duplicate_delete_is_the_same_not_found_boundary() -> None:
    service, deployment_repo = _service(
        {
            "deployment_id": "wh-1",
            "agent_id": "agent-1",
            "enabled": True,
        }
    )

    await service.delete("wh-1", agent_id="agent-1")
    with pytest.raises(APIError) as exc:
        await service.delete("wh-1", agent_id="agent-1")

    assert exc.value.code == "NOT_FOUND"
    assert exc.value.message == "deployment not found"
    assert exc.value.status_code == 404
    assert deployment_repo.delete_for_deployment.await_count == 2


@pytest.mark.parametrize("patch", [
    {"execution_user_id": "bob"},
    {"execution_mode": "owner"},
    {"execution_mode": "linked"},
])
async def test_agent_owner_cannot_change_an_administrator_delegated_execution_account(patch) -> None:
    service, repo = _service({
        "deployment_id": "dep", "agent_id": "agent", "scene": "channel:generic_json",
        "execution_user_id": "alice",
    })
    with pytest.raises(APIError) as error:
        await service.update(
            "dep", agent_id="agent", patch=patch, actor=UserContext(user_id="owner"),
        )
    assert error.value.status_code == 403
    assert repo.webhook["execution_user_id"] == "alice"
    repo.update_for_deployment.assert_not_awaited()


@pytest.mark.parametrize("roles", [[], ["admin"]])
async def test_platform_passes_execution_account_authority_to_the_deployment_service(roles) -> None:
    service, repo = _service({
        "deployment_id": "dep", "agent_id": "agent", "scene": "channel:generic_json",
        "execution_user_id": "alice",
    })
    service._agent_repo.get_agent.return_value = {"created_by": "owner"}
    platform = object.__new__(AgentPlatformService)
    platform._deployment_service = service
    platform._channel_source_host = AsyncMock()
    user = UserContext(user_id="owner", roles=roles)
    if roles:
        result = await platform.update_agent_deployment(
            user, "agent", "dep", {"execution_user_id": "bob"},
        )
        assert result["execution_user_id"] == "bob"
        assert repo.webhook["execution_user_id"] == "bob"
        platform._channel_source_host.reconcile.assert_awaited_once()
    else:
        with pytest.raises(APIError) as error:
            await platform.update_agent_deployment(
                user, "agent", "dep", {"execution_user_id": "bob"},
            )
        assert error.value.status_code == 403
        assert repo.webhook["execution_user_id"] == "alice"
        repo.update_for_deployment.assert_not_awaited()
        platform._channel_source_host.reconcile.assert_not_awaited()


@pytest.mark.parametrize("value", [None, 7, "alice\nbob"])
async def test_invalid_execution_accounts_are_not_coerced_into_delegation(value) -> None:
    service, repo = _service({
        "deployment_id": "dep", "agent_id": "agent", "scene": "channel:generic_json",
    })
    with pytest.raises(APIError) as error:
        await service.update(
            "dep", agent_id="agent", patch={"execution_user_id": value},
            actor=UserContext(user_id="admin", roles=["admin"]),
        )
    assert error.value.status_code == 400
    repo.update_for_deployment.assert_not_awaited()
