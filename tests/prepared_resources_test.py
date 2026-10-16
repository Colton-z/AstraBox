"""Prepared resource synchronization over real repository CAS boundaries."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime import preparation as sync
from astrabox.persistence.repository import agent_repository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.agents import Agent
from astrabox.seams import runtime_preparation as seam
from tests._agent_ledger import set_runtime
from tests.open_sandbox_agent_pool_test import _template

#: The runtime record column each preparation generation key is stored in.
_GENERATION_COLUMNS = {
    "_prewarm_revision": "prewarm_revision",
    "_client_pool_epoch": "client_pool_epoch",
    "_runtime_generation": "runtime_generation",
    "_sandbox_generation": "sandbox_generation",
}


async def _store_generation(agent_id: str, generation: dict) -> None:
    """Record a preparation generation: the Agent's version and its runtime fields."""
    await execute(
        update(Agent.__table__)
        .where(Agent.__table__.c.agent_id == agent_id)
        .values(version=generation["version"])
    )
    await set_runtime(agent_id, **{
        column: generation[key] for key, column in _GENERATION_COLUMNS.items()
    })


async def _pending(agent_id: str, *, provider: str, operation_id: str) -> None:
    """Leave an operation of ``provider`` pending past its deadline, as a lost host would."""
    await set_runtime(
        agent_id,
        resources_operation_provider=provider,
        resources_operation_id=operation_id,
        resources_operation_deadline=0,
    )


@pytest.fixture
async def repo(database):
    template = _template(version=1, prewarm_revision="first")
    await EnvironmentRepository().put(
        "env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    await agent_repository.AgentRepository().create_agent({
        "agent_id": template.agent_id, "name": template.agent_id, "model": "model",
        "environment_name": "env", "created_by": "original-creator",
    })
    await _store_generation(template.agent_id, sync._generation(vars(template)))
    return agent_repository.AgentRepository(), template


class Provider(seam.RuntimePreparationProvider):
    name = "deployment-resources"

    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.finish.set()
        self.contexts = []
        self.writes = []
        self.failure = None
        self.recovered = False
        self.operations = []

    async def synchronize(self, *, context):
        self.contexts.append(context)
        self.started.set()
        await self.finish.wait()
        if self.failure:
            raise self.failure
        value = await context.sandbox.files.read_bytes("/declaration")
        if not self.writes or self.writes[-1] != value:
            self.writes.append(value)

    async def recover(self, *, operation):
        self.operations.append(operation)
        return self.recovered


@pytest.fixture
def provider(monkeypatch):
    value = Provider()
    monkeypatch.setattr(seam, "_PROVIDER", value)
    return value


def box(content=b"first"):
    return SimpleNamespace(files=SimpleNamespace(read_bytes=AsyncMock(return_value=content)))


async def synchronize(repo, template, sandbox=None):
    await sync.synchronize_prepared_resources(
        template, sandbox or box(), timeout_seconds=1, agent_repo=repo,
    )


async def advance(repo, template, **changes):
    updated = copy.deepcopy(template)
    vars(updated).update(changes)
    await _store_generation(updated.agent_id, sync._generation(vars(updated)))
    return updated


async def test_obsolete_callback_cannot_follow_new_generation(repo, provider):
    store, template = repo
    provider.finish.clear()
    old = asyncio.create_task(synchronize(store, template, box(b"old")))
    await provider.started.wait()
    updated = await advance(store, template, version=2, client_pool_epoch="new")
    with pytest.raises(APIError, match="Another resource"):
        await synchronize(store, updated, box(b"new"))
    provider.finish.set()
    with pytest.raises(APIError) as stale:
        await old
    assert stale.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    await synchronize(store, updated, box(b"new"))
    with pytest.raises(APIError) as late:
        await synchronize(store, template, box(b"old"))
    assert late.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    assert provider.writes == [b"old", b"new"]


@pytest.mark.parametrize("failure", [RuntimeError("private remote secret"), asyncio.CancelledError()])
async def test_failure_preserves_uncertainty_until_provider_fences_it(repo, provider, failure):
    store, template = repo
    provider.failure = failure
    with pytest.raises((APIError, asyncio.CancelledError)) as failed:
        await synchronize(store, template)
    assert "private remote secret" not in str(failed.value)
    pending = (await store.get_agent(template.agent_id))[sync._OPERATION]
    assert pending["operation_id"] == provider.contexts[0].operation.operation_id
    provider.failure = None
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "RESOURCE_SYNC_UNRESOLVED"
    assert provider.writes == []
    provider.recovered = True
    await synchronize(store, template)
    assert provider.operations[-1].operation_id == pending["operation_id"]
    assert provider.writes == [b"first"]
    assert (await store.get_agent(template.agent_id)).get(sync._OPERATION) is None


async def test_expired_guard_is_not_permission_to_repeat_external_writes(repo, provider):
    store, template = repo
    await _pending(template.agent_id, provider=provider.name, operation_id="lost-host")
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "RESOURCE_SYNC_UNRESOLVED"
    assert provider.contexts == []
    provider.recovered = True
    await synchronize(store, template)
    assert provider.operations[-1].operation_id == "lost-host"
    assert provider.writes == [b"first"]


async def test_changed_provider_cannot_recover_another_providers_operation(repo, provider):
    store, template = repo
    await _pending(template.agent_id, provider="other", operation_id="lost-host")
    provider.recovered = True
    with pytest.raises(APIError) as blocked:
        await synchronize(store, template)
    assert blocked.value.code == "RESOURCE_SYNC_UNRESOLVED"
    assert provider.operations == []
    assert provider.contexts == []


async def test_timeout_does_not_release_uncertain_operation(repo, provider):
    store, template = repo
    provider.finish.clear()
    with pytest.raises(APIError) as failed:
        await sync.synchronize_prepared_resources(template, box(), timeout_seconds=0.01, agent_repo=store)
    assert failed.value.code == "RESOURCE_SYNC_FAILED"
    assert (await store.get_agent(template.agent_id))[sync._OPERATION]["deadline"] == 0


async def test_two_replicas_racing_to_claim_one_agent_do_not_duplicate_the_identity(repo, provider):
    store, template = repo
    provider.finish.clear()
    tasks = [asyncio.create_task(synchronize(agent_repository.AgentRepository(), template)) for _ in range(4)]
    try:
        await provider.started.wait()
        # Let every contender reach the durable CAS while the winner is held.
        await asyncio.wait_for(asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED), timeout=5)
        provider.finish.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert any(result is None for result in results)
        assert all(result is None or isinstance(result, APIError) and result.code == "RESOURCE_SYNC_BUSY" for result in results)
        assert provider.writes == [b"first"]
        assert (await store.get_agent(template.agent_id)).get(sync._OPERATION) is None
    finally:
        provider.finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_provider_cannot_mutate_the_snapshot_used_by_generation_checks(repo, provider):
    store, template = repo

    async def mutate(*, context):
        context.config["version"] = 2
        await _store_generation(template.agent_id, {**sync._generation(vars(template)), "version": 2})

    provider.synchronize = mutate
    with pytest.raises(APIError) as stale:
        await synchronize(store, template)
    assert stale.value.code == "AGENT_RUNTIME_GENERATION_CONFLICT"
    assert template.version == 1
