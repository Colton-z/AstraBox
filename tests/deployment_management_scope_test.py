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
from astrabox.core.service.orchestrator.service_factories import (
    DeploymentServiceReplacement,
)
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.persistence.repository import deployment_repository as repo_module
from astrabox.persistence.repository.backend import ReturnDocument
from astrabox.persistence.repository.deployment_repository import (
    DeploymentRepository,
)


class _AtomicWebhookRepo:
    """Small stateful stand-in with the repository's guarded-write semantics."""

    def __init__(self, webhook: dict[str, Any] | None) -> None:
        self.webhook = dict(webhook) if webhook is not None else None
        self.delete_before_update = False
        self.get_for_deployment = AsyncMock(side_effect=self._get_for_deployment)
        self.update_for_deployment = AsyncMock(side_effect=self._update_for_deployment)
        self.soft_delete_for_deployment = AsyncMock(
            side_effect=self._soft_delete_for_deployment
        )

    def _matches(self, deployment_id: str, agent_id: str) -> bool:
        return bool(
            self.webhook
            and not self.webhook.get("deleted")
            and self.webhook.get("deployment_id") == deployment_id
            and self.webhook.get("agent_id") == agent_id
        )

    async def _get_for_deployment(
        self, deployment_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        return dict(self.webhook) if self._matches(deployment_id, agent_id) else None

    async def _update_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.delete_before_update and self.webhook is not None:
            self.webhook["deleted"] = True
        if not self._matches(deployment_id, agent_id):
            return None
        self.webhook.update(updates)  # type: ignore[union-attr]
        return dict(self.webhook)  # type: ignore[arg-type]

    async def _soft_delete_for_deployment(
        self, deployment_id: str, agent_id: str
    ) -> bool:
        if not self._matches(deployment_id, agent_id):
            return False
        self.webhook["deleted"] = True  # type: ignore[index]
        return True


def _service(
    webhook: dict[str, Any] | None,
) -> tuple[DeploymentService, _AtomicWebhookRepo]:
    deployment_repo = _AtomicWebhookRepo(webhook)
    service = DeploymentService(
        deployment_repo=deployment_repo,  # type: ignore[arg-type]
        agent_repo=AsyncMock(),
        agent_service_getter=lambda: AsyncMock(),
        stream_message_events_ds=AsyncMock(),
        dispatch_turn_input=AsyncMock(),
        sessions_repo=AsyncMock(),
        spawn_background_task=lambda *args, **kwargs: None,
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),  # management paths never touch the spine
    )
    return service, deployment_repo


async def test_cross_agent_listing_batches_authorization_without_n_plus_one() -> None:
    deployment_repo = AsyncMock()
    deployment_repo.list_active.return_value = [
        {
            "_id": "storage-1",
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
            "user_id": "owner-1",
        },
        "agent-foreign": {
            "agent_id": "agent-foreign",
            "name": "Foreign",
            "user_id": "owner-2",
        },
    }
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: AsyncMock(),
        stream_message_events_ds=AsyncMock(),
        dispatch_turn_input=AsyncMock(),
        sessions_repo=AsyncMock(),
        spawn_background_task=lambda *args, **kwargs: None,
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
                "agent_id": "agent-1",
                "deleted": True,
            },
            "agent-1",
            "deleted",
        ),
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


async def test_deployment_mutation_accepts_its_own_agent() -> None:
    webhook = {
        "deployment_id": "wh-1",
        "agent_id": "agent-1",
        "scene": "hmac",
        "enabled": True,
    }
    service, deployment_repo = _service(webhook)

    updated = await service.update(
        "wh-1", agent_id="agent-1", patch={"enabled": False}
    )
    await service.delete("wh-1", agent_id="agent-1")

    assert updated["enabled"] is False
    assert deployment_repo.webhook is not None and deployment_repo.webhook["deleted"] is True
    deployment_repo.update_for_deployment.assert_awaited_once()
    deployment_repo.soft_delete_for_deployment.assert_awaited_once_with(
        "wh-1", "agent-1"
    )


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
    assert deployment_repo.webhook == {
        "deployment_id": "wh-1",
        "agent_id": "agent-1",
        "scene": "hmac",
        "enabled": True,
        "deleted": True,
    }


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
    assert deployment_repo.soft_delete_for_deployment.await_count == 2


async def test_repository_mutations_put_scope_and_active_guard_in_atomic_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = AsyncMock()
    collection.find_one_and_update.side_effect = [
        {
            "deployment_id": "wh-1",
            "agent_id": "agent-1",
            "enabled": False,
        },
        {
            "deployment_id": "wh-1",
            "agent_id": "agent-1",
            "deleted": True,
        },
    ]
    monkeypatch.setattr(
        repo_module,
        "get_async_collection",
        AsyncMock(return_value=collection),
    )
    repository = object.__new__(DeploymentRepository)
    repository._collection_name = "webhooks-test"

    updated = await repository.update_for_deployment(
        "wh-1",
        "agent-1",
        {
            "enabled": False,
            "deployment_id": "wh-other",
            "agent_id": "agent-other",
            "deleted": True,
        },
    )
    deleted = await repository.soft_delete_for_deployment("wh-1", "agent-1")

    assert updated is not None and updated["enabled"] is False
    assert deleted is True
    update_call, delete_call = collection.find_one_and_update.await_args_list
    expected_filter = {
        "deployment_id": "wh-1",
        "agent_id": "agent-1",
        "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
    }
    assert update_call.args[0] == expected_filter
    assert update_call.args[1] == {"$set": {"enabled": False}}
    assert update_call.kwargs == {"return_document": ReturnDocument.AFTER}
    assert delete_call.args[0] == expected_filter
    assert delete_call.args[1] == {"$set": {"deleted": True}}
    assert delete_call.kwargs == {"return_document": ReturnDocument.AFTER}


class _ReplacementDeploymentService:
    """A full external replacement implementing the frozen used surface."""

    def __init__(self) -> None:
        self.management_calls: list[tuple[Any, ...]] = []

    async def assert_can_manage_agent(
        self, user: Any, agent_id: str
    ) -> dict[str, Any]:
        self.management_calls.append(("authorize", user.user_id, agent_id))
        return {"agent_id": agent_id}

    async def list_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        return []

    async def list_manageable(self, user: Any) -> list[dict[str, Any]]:
        self.management_calls.append(("list", user.user_id))
        return []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    async def update(
        self,
        deployment_id: str,
        *,
        agent_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        self.management_calls.append(
            ("update", deployment_id, agent_id, dict(patch))
        )
        return {"deployment_id": deployment_id, **patch}

    async def delete(self, deployment_id: str, *, agent_id: str) -> None:
        self.management_calls.append(("delete", deployment_id, agent_id))

    async def trigger(
        self, deployment_id: str, *, headers: dict[str, str], raw_body: bytes
    ) -> dict[str, Any]:
        return {"deployment_id": deployment_id}

    async def forward_channel_callback(
        self,
        deployment_id: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> dict[str, Any]:
        self.management_calls.append(
            (
                "channel_callback",
                deployment_id,
                method,
                path,
                query,
                dict(headers),
                raw_body,
            )
        )
        return {"deployment_id": deployment_id}

    async def list_runs_for_deployment(
        self,
        deployment_id: str,
        *,
        agent_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.management_calls.append(
            ("list_runs", deployment_id, agent_id, limit)
        )
        return []

    async def trigger_now(
        self, deployment_id: str, *, agent_id: str
    ) -> dict[str, Any]:
        self.management_calls.append(("trigger_now", deployment_id, agent_id))
        return {"deployment_id": deployment_id}

    async def replay_run(
        self,
        run_id: str,
        *,
        deployment_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        self.management_calls.append(
            ("replay", run_id, deployment_id, agent_id)
        )
        return {"run_id": run_id}

    async def start_run_session(
        self, context: dict[str, Any], *, run_id: str
    ) -> str:
        return run_id

    async def drive_run_turn(
        self,
        context: dict[str, Any],
        *,
        run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        return {"turn_id": run_id}


async def test_public_replacement_contract_receives_route_agent_scope() -> None:
    deployment_service = _ReplacementDeploymentService()
    assert isinstance(deployment_service, DeploymentServiceReplacement)
    assert isinstance(object.__new__(DeploymentService), DeploymentServiceReplacement)
    platform = object.__new__(AgentPlatformService)
    platform._deployment_service = deployment_service
    platform._channel_source_host = AsyncMock()
    user = UserContext(user_id="owner-1")

    await platform.update_agent_deployment(
        user, "agent-1", "wh-1", {"enabled": False}
    )
    await platform.delete_agent_deployment(user, "agent-1", "wh-1")
    await platform.list_deployment_runs(user, "agent-1", "wh-1", limit=25)
    await platform.trigger_deployment_run(user, "agent-1", "wh-1")
    await platform.replay_deployment_run(
        user, "agent-1", "wh-1", "run-1"
    )
    callback = await platform.forward_channel_callback(
        "wh-1",
        method="POST",
        path="/telegram",
        query="signature=ok",
        headers={"content-type": "application/json"},
        raw_body=b"{}",
    )
    assert callback == {"deployment_id": "wh-1"}
    assert platform._channel_source_host.reconcile.await_count == 2

    assert deployment_service.management_calls == [
        ("authorize", "owner-1", "agent-1"),
        ("update", "wh-1", "agent-1", {"enabled": False}),
        ("authorize", "owner-1", "agent-1"),
        ("delete", "wh-1", "agent-1"),
        ("authorize", "owner-1", "agent-1"),
        ("list_runs", "wh-1", "agent-1", 25),
        ("authorize", "owner-1", "agent-1"),
        ("trigger_now", "wh-1", "agent-1"),
        ("authorize", "owner-1", "agent-1"),
        ("replay", "run-1", "wh-1", "agent-1"),
        (
            "channel_callback",
            "wh-1",
            "POST",
            "/telegram",
            "signature=ok",
            {"content-type": "application/json"},
            b"{}",
        ),
    ]
