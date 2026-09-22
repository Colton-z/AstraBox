"""OpenSandbox's SDK client pool behind the platform-owned Agent recipe.

The provider tests pin only supplier responsibilities: distributed pool
construction, prepared-only acquisition, honest misses, lifecycle visibility,
and cleanup of creates lost before SDK publication. The platform tests pin the
other half of the boundary: AstraBox supplies the complete box recipe, mounts
the Agent's durable root, supplies its complete credential plan, and
adopts an acquired box into the Agent runtime that owns the shared box.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opensandbox.exceptions import (
    PoolEmptyException,
    PoolStateStoreUnavailableException,
)
from opensandbox.pool import AcquirePolicy

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_workspace_plan import RuntimeWorkspacePlan
from astrabox.providers.open_sandbox import executor as executor_module
from astrabox.providers.open_sandbox.agent_pool import (
    OpenSandboxClientPoolRegistry,
    _client_pool_assignment_id,
    _client_pool_session_id,
)
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.egress_credentials import (
    EgressCredential,
    HTTPBasicEgressCredential,
    HTTPBasicEgressCredentialSet,
    MCPHeaderEgressCredential,
    ModelEgressCredential,
    ModelEgressCredentialSubstitution,
    SandboxEgressCredentialPlan,
)
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.sandbox import (
    SandboxClientPoolMember,
    SandboxClientPoolSpec,
    SandboxClientPoolStatus,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.seams.storage import StorageMountPlan


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "agent_prewarm_redis_url": "redis://redis.test:6379/0",
        "sandbox_credential_vault_enabled": False,
        "sandbox_ready_timeout_seconds": 30,
        "sandbox_request_timeout_seconds": 15,
        "sandbox_lease_seconds": 3600,
        "sandbox_workspace_volume": "astrabox-workspaces",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _template(**overrides: Any) -> SimpleNamespace:
    values = {
        "agent_id": "agent-1",
        "engine_kind": "claude_code",
        "prewarm_enabled": True,
        "runtime_generation": "a" * 64,
        "sandbox_generation": "a" * 64,
        "client_pool_epoch": "b" * 32,
        "sandbox_backend": "open_sandbox",
        "sandbox_tenancy": "agent",
        "sandbox_permission_level": "advanced",
        "runtime_template_name": "astrabox/sandbox-claude-code:test",
        "networking": {"type": "limited", "allow_mcp_servers": True},
        "skills": [],
        "plugin_repos": [],
        "mcp_servers": {},
        "model_config": {
            "base_url": "https://model.test",
            "api_key": "real-model-secret",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _spec(**overrides: Any) -> SandboxClientPoolSpec:
    values = {
        "pool_name": "astrabox-agent-agent-1-aaaaaaaaaaaa",
        "creation_image": "astrabox/sandbox-claude-code:test",
        "max_idle": 1,
        "idle_timeout_seconds": 3600,
        "preparation_timeout_seconds": 600,
    }
    values.update(overrides)
    return SandboxClientPoolSpec(**values)


@pytest.fixture(autouse=True)
def _stable_provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.load_astrabox_settings",
        lambda: _settings(),
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(return_value=None),
    )


class _FakeSdkSandbox:
    def __init__(self, sandbox_id: str = "sbx-warm") -> None:
        self.id = sandbox_id
        self.metadata_updates: list[dict[str, str]] = []
        self.killed = 0
        self.closed = 0

    async def patch_metadata(self, updates: dict[str, str]) -> None:
        self.metadata_updates.append(dict(updates))

    async def kill(self) -> None:
        self.killed += 1

    async def close(self) -> None:
        self.closed += 1


class _FakePool:
    def __init__(self, kwargs: dict[str, Any], *, acquired: Any) -> None:
        self.kwargs = kwargs
        self.acquired = acquired
        self.started = 0
        self.acquire_calls: list[dict[str, Any]] = []
        self.shutdown_calls: list[bool] = []

    async def start(self) -> None:
        self.started += 1

    async def acquire(self, **kwargs: Any) -> Any:
        self.acquire_calls.append(dict(kwargs))
        if isinstance(self.acquired, BaseException):
            raise self.acquired
        return self.acquired

    async def shutdown(self, *, graceful: bool) -> None:
        self.shutdown_calls.append(graceful)

    async def snapshot(self) -> Any:
        return SimpleNamespace(
            lifecycle_state=SimpleNamespace(value="RUNNING"),
            idle_count=1,
            max_idle=1,
            failure_count=0,
            backoff_active=False,
            last_error=None,
            in_flight_operations=0,
        )


class _FakeStateStore:
    def __init__(
        self,
        *,
        max_idle: int | None = None,
        idle_count: int = 0,
        idle_sandbox_ids: tuple[str, ...] = (),
    ) -> None:
        self.max_idle = max_idle
        self.idle_count = idle_count
        self.idle_sandbox_ids = idle_sandbox_ids

    async def get_max_idle(self, _pool_name: str) -> int | None:
        return self.max_idle

    async def snapshot_counters(self, _pool_name: str) -> Any:
        return SimpleNamespace(idle_count=self.idle_count)

    async def snapshot_idle_entries(self, _pool_name: str) -> list[Any]:
        return [SimpleNamespace(sandbox_id=sandbox_id) for sandbox_id in self.idle_sandbox_ids]


def _pool_factory(
    pools: list[_FakePool],
    *,
    acquired: Any,
) -> Any:
    def factory(**kwargs: Any) -> _FakePool:
        pool = _FakePool(kwargs, acquired=acquired)
        pools.append(pool)
        return pool

    return factory


async def _creator(_member: SandboxClientPoolMember) -> OpenSandboxHandle:
    return OpenSandboxHandle(_FakeSdkSandbox("created"))


async def _preparer(_handle: Any) -> None:
    return None


# Provider-owned SDK pool mechanism.


async def test_registry_builds_the_official_pool_and_acquires_prepared_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeSdkSandbox()
    pools: list[_FakePool] = []
    members: list[SandboxClientPoolMember] = []
    prepared: list[str] = []

    async def create(member: SandboxClientPoolMember) -> OpenSandboxHandle:
        members.append(member)
        return OpenSandboxHandle(_FakeSdkSandbox("created-by-platform"))

    async def prepare(handle: Any) -> None:
        prepared.append(handle.sandbox_id)

    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=_pool_factory(pools, acquired=sdk),
    )
    spec = _spec()

    await registry.ensure(spec, creator=create, preparer=prepare)
    created_sdk = await pools[0].kwargs["sandbox_creator"](
        SimpleNamespace(connection_config=object())
    )
    await pools[0].kwargs["warmup_sandbox_preparer"](sdk)
    acquired = await registry.acquire(spec)

    assert len(pools) == 1 and pools[0].started == 1
    assert pools[0].kwargs["max_idle"] == 1
    assert pools[0].kwargs["creation_spec"].image == spec.creation_image
    assert pools[0].acquire_calls == [
        {
            "sandbox_timeout": timedelta(seconds=3600),
            "policy": AcquirePolicy.FAIL_FAST,
        }
    ]
    assert created_sdk.id == "created-by-platform"
    assert len(members) == 1
    assert members[0].pool_name == spec.pool_name
    assert members[0].member_index == 0
    assert prepared == ["sbx-warm"]
    assert acquired is not None and acquired.sandbox_id == "sbx-warm"
    # Supplier acquisition transfers an opaque box. Session ownership is a
    # later platform operation, not something the SDK adapter guesses here.
    assert sdk.metadata_updates == []


async def test_concurrent_ensure_starts_one_local_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools: list[_FakePool] = []
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=_pool_factory(pools, acquired=_FakeSdkSandbox()),
    )

    await asyncio.gather(
        *(registry.ensure(_spec(), creator=_creator, preparer=_preparer) for _ in range(8))
    )

    assert len(pools) == 1
    assert pools[0].started == 1


async def test_only_pool_empty_is_an_ordinary_cold_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools: list[_FakePool] = []
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=_pool_factory(pools, acquired=PoolEmptyException()),
    )
    await registry.ensure(_spec(), creator=_creator, preparer=_preparer)

    assert await registry.acquire(_spec()) is None
    assert pools[0].acquire_calls[0]["policy"] is AcquirePolicy.FAIL_FAST


@pytest.mark.parametrize(
    "failure",
    [
        PoolStateStoreUnavailableException("redis refused the state read"),
        ConnectionError("coordination transport disconnected"),
    ],
)
async def test_coordination_failures_fail_loud_instead_of_cold_creating(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    pools: list[_FakePool] = []
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=_pool_factory(pools, acquired=failure),
    )
    await registry.ensure(_spec(), creator=_creator, preparer=_preparer)

    with pytest.raises(APIError) as caught:
        await registry.acquire(_spec())

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert caught.value.status_code == 503


async def test_acquire_refuses_a_pool_not_started_in_this_event_loop() -> None:
    with pytest.raises(APIError) as caught:
        await OpenSandboxClientPoolRegistry(state_store=_FakeStateStore()).acquire(_spec())

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert "not started" in caught.value.message


async def test_registry_refuses_capacity_without_distinct_recovery_identities() -> None:
    spec = _spec(max_idle=2)
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())

    with pytest.raises(APIError) as caught:
        await registry.ensure(spec, creator=_creator, preparer=_preparer)

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNSUPPORTED"
    assert "exactly one" in caught.value.message
    assert registry._pools == {}


async def test_registry_exposes_its_local_official_pool_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools: list[_FakePool] = []
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=_pool_factory(pools, acquired=_FakeSdkSandbox()),
    )
    await registry.ensure(_spec(), creator=_creator, preparer=_preparer)

    status = await registry.describe(_spec().pool_name)

    assert status == SandboxClientPoolStatus(
        pool_name=_spec().pool_name,
        lifecycle_state="RUNNING",
        ready=True,
        idle_count=1,
        max_idle=1,
        failure_count=0,
        backoff_active=False,
        in_flight_operations=0,
        last_error=False,
    )


async def test_status_reads_shared_inventory_without_starting_a_scheduler() -> None:
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore(max_idle=1, idle_count=1))

    status = await registry.describe(_spec().pool_name)

    assert status.ready is True
    assert status.idle_count == 1
    assert status.max_idle == 1
    assert status.lifecycle_state is None
    assert status.failure_count is None
    assert registry._pools == {}


async def test_status_names_a_namespace_that_has_not_started() -> None:
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())

    status = await registry.describe(_spec().pool_name)

    assert status.lifecycle_state == "NOT_STARTED"
    assert status.ready is False
    assert status.idle_count == 0
    assert registry._pools == {}


async def test_failed_pool_start_is_not_published_as_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StartFailedPool(_FakePool):
        async def start(self) -> None:
            raise ConnectionError("redis refused the pool state write")

    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=lambda **kwargs: _StartFailedPool(kwargs, acquired=_FakeSdkSandbox()),
    )

    with pytest.raises(APIError) as caught:
        await registry.ensure(_spec(), creator=_creator, preparer=_preparer)

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert registry._pools == {}


async def test_creator_failure_surfaces_as_pool_unavailability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    creator = AsyncMock(side_effect=RuntimeError("sandbox API rejected create"))
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())

    with caplog.at_level("ERROR"):
        with pytest.raises(APIError) as caught:
            await registry._creator(_spec(), creator)(SimpleNamespace(connection_config=object()))

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert "RuntimeError" in caplog.text
    assert "sandbox API rejected create" in caplog.text


async def test_creator_preserves_a_platform_refusal() -> None:
    refusal = APIError(
        code="SANDBOX_ISOLATION_UNSUPPORTED",
        message="the candidate cannot isolate conversations",
        status_code=409,
    )
    creator = AsyncMock(side_effect=refusal)

    with pytest.raises(APIError) as caught:
        await OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())._creator(
            _spec(), creator
        )(SimpleNamespace(connection_config=object()))

    assert caught.value is refusal


# Historical create-publication crash recovery.


def test_client_pool_member_identity_is_unique_per_create_and_durable_per_slot() -> None:
    pool_name = _spec().pool_name
    first_session = _client_pool_session_id(pool_name)
    second_session = _client_pool_session_id(pool_name)

    assert first_session != second_session
    assert len(first_session) <= 63
    assert re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?",
        first_session,
    )
    assert _client_pool_assignment_id(pool_name) == _client_pool_assignment_id(pool_name)
    assert _client_pool_assignment_id(pool_name) != _client_pool_assignment_id("another-pool")


async def test_creator_reclaims_an_unpublished_member_before_replenishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    descriptor = SimpleNamespace(
        sandbox_id="unpublished-warmup",
        session_id=_client_pool_session_id(spec.pool_name),
    )
    lookup = AsyncMock(side_effect=[descriptor, descriptor])
    destroy_box = AsyncMock(
        return_value=SandboxDestruction.confirmed_gone(
            "unpublished-warmup",
            detail="the control plane reports the sandbox absent",
        )
    )
    create = AsyncMock(return_value=OpenSandboxHandle(_FakeSdkSandbox("replacement")))
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        lookup,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._assignment_release_settle_seconds",
        lambda: 0,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.executor.destroy_open_sandbox_box",
        destroy_box,
    )

    result = await OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())._creator(
        spec, create
    )(SimpleNamespace(connection_config=object()))

    assert result.id == "replacement"
    assert lookup.await_count == 2
    assert all(
        call.args == (_client_pool_assignment_id(spec.pool_name),)
        for call in lookup.await_args_list
    )
    destroy_box.assert_awaited_once()
    assert destroy_box.await_args.args[1] == "unpublished-warmup"
    create.assert_awaited_once()


async def test_creator_waits_for_an_acquired_member_to_finish_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    descriptor = SimpleNamespace(
        sandbox_id="member-being-adopted",
        session_id=_client_pool_session_id(spec.pool_name),
    )
    lookup = AsyncMock(side_effect=[descriptor, None])
    destroy_box = AsyncMock()
    create = AsyncMock(return_value=OpenSandboxHandle(_FakeSdkSandbox("replacement")))
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        lookup,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.executor.destroy_open_sandbox_box",
        destroy_box,
    )

    result = await OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())._creator(
        spec, create
    )(SimpleNamespace(connection_config=object()))

    assert result.id == "replacement"
    assert lookup.await_count == 2
    destroy_box.assert_not_awaited()


async def test_creator_never_reclaims_a_member_still_in_official_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    descriptor = SimpleNamespace(
        sandbox_id="published-warmup",
        session_id=_client_pool_session_id(spec.pool_name),
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(return_value=descriptor),
    )
    create = AsyncMock()

    with pytest.raises(APIError) as caught:
        await OpenSandboxClientPoolRegistry(
            state_store=_FakeStateStore(idle_sandbox_ids=("published-warmup",))
        )._creator(spec, create)(SimpleNamespace(connection_config=object()))

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    create.assert_not_awaited()


async def test_creator_refuses_a_durable_assignment_owned_by_another_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(
            return_value=SimpleNamespace(
                sandbox_id="foreign-warmup",
                session_id=_client_pool_session_id("another-pool"),
            )
        ),
    )
    create = AsyncMock()

    with pytest.raises(APIError) as caught:
        await OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())._creator(spec, create)(
            SimpleNamespace(connection_config=object())
        )

    assert caught.value.code == "SANDBOX_ASSIGNMENT_CONFLICT"
    create.assert_not_awaited()


async def test_retire_shuts_down_then_destroys_namespace_and_unpublished_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    pools: list[_FakePool] = []

    class _OrderedPool(_FakePool):
        async def shutdown(self, *, graceful: bool) -> None:
            await super().shutdown(graceful=graceful)
            events.append("shutdown")

    def factory(**kwargs: Any) -> _OrderedPool:
        pool = _OrderedPool(kwargs, acquired=_FakeSdkSandbox())
        pools.append(pool)
        return pool

    class _Manager:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def destroy(self, name: str, options: object = None) -> Any:
            assert name == _spec().pool_name
            assert options is not None
            events.append("destroy-namespace")
            return SimpleNamespace(killed_idle_count=0)

    descriptor = SimpleNamespace(
        sandbox_id="unpublished-warmup",
        session_id=_client_pool_session_id(_spec().pool_name),
    )

    async def destroy_unpublished(*_args: Any) -> SandboxDestruction:
        events.append("destroy-unpublished")
        return SandboxDestruction.confirmed_gone(
            "unpublished-warmup",
            detail="gone",
        )

    destroy_box = AsyncMock(side_effect=destroy_unpublished)
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._config.sdk_connection_config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.SandboxPoolManagerAsync",
        _Manager,
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(return_value=descriptor),
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._assignment_release_settle_seconds",
        lambda: 0,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.executor.destroy_open_sandbox_box",
        destroy_box,
    )
    registry = OpenSandboxClientPoolRegistry(
        state_store=_FakeStateStore(),
        pool_factory=factory,
    )
    await registry.ensure(_spec(), creator=_creator, preparer=_preparer)

    await registry.retire(_spec().pool_name)

    assert events == ["shutdown", "destroy-namespace", "destroy-unpublished"]
    assert pools[0].shutdown_calls == [True]


async def test_retire_uses_a_short_nonzero_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Manager:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def destroy(self, pool_name: str, options: object = None) -> Any:
            captured["pool_name"] = pool_name
            captured["options"] = options
            return SimpleNamespace(killed_idle_count=0)

    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.SandboxPoolManagerAsync",
        _Manager,
    )
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())
    monkeypatch.setattr(registry, "_connection_config", lambda: object())

    await registry.retire(_spec().pool_name)

    assert captured["pool_name"] == _spec().pool_name
    options = captured["options"]
    assert options is not None
    ttl = getattr(options, "tombstone_ttl", None)
    assert isinstance(ttl, timedelta) and ttl.total_seconds() > 0
    assert ttl <= timedelta(hours=1)


async def test_retire_does_not_destroy_a_member_being_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SimpleNamespace(
        sandbox_id="member-being-adopted",
        session_id=_client_pool_session_id(_spec().pool_name),
    )
    manager = SimpleNamespace(
        destroy=AsyncMock(return_value=SimpleNamespace(killed_idle_count=0))
    )
    lookup = AsyncMock(side_effect=[descriptor, None])
    destroy_box = AsyncMock()
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.SandboxPoolManagerAsync",
        lambda **_kwargs: manager,
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        lookup,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.executor.destroy_open_sandbox_box",
        destroy_box,
    )
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())
    monkeypatch.setattr(registry, "_connection_config", lambda: object())

    await registry.retire(_spec().pool_name)

    assert lookup.await_count == 2
    destroy_box.assert_not_awaited()


async def test_retire_fails_loud_when_an_unpublished_member_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(destroy=AsyncMock(return_value=SimpleNamespace(killed_idle_count=0)))
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.SandboxPoolManagerAsync",
        lambda **_kwargs: manager,
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(
            return_value=SimpleNamespace(
                sandbox_id="surviving-warmup",
                session_id=_client_pool_session_id(_spec().pool_name),
            )
        ),
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool._assignment_release_settle_seconds",
        lambda: 0,
    )
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.executor.destroy_open_sandbox_box",
        AsyncMock(
            return_value=SandboxDestruction.unconfirmed(
                "surviving-warmup",
                detail="the control plane did not confirm deletion",
            )
        ),
    )
    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())
    monkeypatch.setattr(registry, "_connection_config", lambda: object())

    with pytest.raises(APIError) as caught:
        await registry.retire(_spec().pool_name)

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert caught.value.data["leaked_sandbox_id"] == "surviving-warmup"


# Provider seam delegation.


async def test_open_sandbox_provider_delegates_the_complete_client_pool_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = OpenSandboxHandle(_FakeSdkSandbox())
    status = SandboxClientPoolStatus(
        pool_name=_spec().pool_name,
        lifecycle_state="RUNNING",
        ready=True,
        idle_count=1,
        max_idle=1,
    )
    registry = SimpleNamespace(
        ensure=AsyncMock(),
        acquire=AsyncMock(return_value=handle),
        describe=AsyncMock(return_value=status),
        retire=AsyncMock(),
    )
    provider = OpenSandboxSandboxProvider()
    monkeypatch.setattr(provider, "_client_pool_registry", lambda: registry)

    await provider.ensure_client_pool(_spec(), creator=_creator, preparer=_preparer)
    acquired = await provider.acquire_client_pool(_spec())
    described = await provider.describe_client_pool(_spec().pool_name)
    await provider.retire_client_pool(_spec().pool_name)

    registry.ensure.assert_awaited_once_with(_spec(), creator=_creator, preparer=_preparer)
    registry.acquire.assert_awaited_once_with(_spec())
    registry.describe.assert_awaited_once_with(_spec().pool_name)
    registry.retire.assert_awaited_once_with(_spec().pool_name)
    assert acquired is handle
    assert described is status


async def test_provider_shutdown_closes_this_event_loops_registry() -> None:
    provider = OpenSandboxSandboxProvider()
    registry = AsyncMock()
    loop_key = id(asyncio.get_running_loop())
    provider._client_pool_registries[loop_key] = registry

    await provider.shutdown_current_loop_resources()

    registry.shutdown.assert_awaited_once_with()
    assert loop_key not in provider._client_pool_registries


async def test_provider_shutdown_waits_for_an_in_flight_destroy() -> None:
    provider = OpenSandboxSandboxProvider()
    destroy_started = asyncio.Event()
    finish_destroy = asyncio.Event()

    async def destroy() -> None:
        destroy_started.set()
        await finish_destroy.wait()

    destroy_task = asyncio.create_task(destroy())
    executor_module._RELEASES_IN_FLIGHT.add(destroy_task)
    destroy_task.add_done_callback(executor_module._RELEASES_IN_FLIGHT.discard)
    shutdown_task: asyncio.Task[Any] | None = None
    try:
        await destroy_started.wait()
        shutdown_task = asyncio.create_task(provider.shutdown_current_loop_resources())
        await asyncio.sleep(0)

        assert not shutdown_task.done()
        finish_destroy.set()
        await shutdown_task
        assert destroy_task.done()
    finally:
        finish_destroy.set()
        if shutdown_task is not None and not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(
            destroy_task,
            *(task for task in (shutdown_task,) if task is not None),
            return_exceptions=True,
        )
        executor_module._RELEASES_IN_FLIGHT.discard(destroy_task)


# Platform-owned pool composition and claim.


class _PlatformProvider:
    name = "open_sandbox"
    supports_client_pool = True

    def __init__(self) -> None:
        self.created_specs: list[Any] = []
        self.events: list[str] = []
        self.acquired: Any = None
        self.confirmed: list[str] = []

    async def create_sandbox(self, spec: Any) -> OpenSandboxHandle:
        self.created_specs.append(spec)
        return OpenSandboxHandle(_FakeSdkSandbox("base-box"))

    async def find_sandbox_by_assignment(self, _assignment_id: str) -> None:
        return None

    async def acquire_client_pool(self, _spec: Any) -> Any:
        self.events.append("acquire")
        return self.acquired

    async def adopt_sandbox_identity(
        self,
        _handle: Any,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        self.events.append(f"adopt:{session_id}:{assignment_id}")

    async def read_isolation_capability(self, sandbox_id: str) -> Any:
        self.events.append(f"isolation:{sandbox_id}")
        return SimpleNamespace(available=True, detail=None)

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        self.confirmed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="gone")


class _Adapter:
    def sandbox_request(self, *, template: Any, model_access: Any) -> Any:
        return SimpleNamespace(
            credential=object(),
            required_network_hosts=("engine.vendor.test",),
            cwd="/workspace",
            entrypoint=("/opt/astrabox/start",),
            env={"PUBLIC_SETTING": "yes"},
            credential_env_var="MODEL_API_KEY",
            cwd_env_var="ASTRABOX_WORKSPACE",
            publish_ports=(8000,),
            wait_for_inbox_service_port=8000,
        )

    @property
    def capabilities(self) -> Any:
        return SimpleNamespace(session_log=None)


class _RuntimeManager(RemoteAgentRuntimeManager):
    def __init__(self) -> None:
        super().__init__(sessions_repo=SimpleNamespace(record_startup_allocation=AsyncMock()))

    def resolve_model_access(self, config: dict[str, Any]) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration=dict(config),
            base_url="https://model.test",
            model_name="model",
            credential="real-model-secret",
            credential_kind="bearer",
            endpoint_provider="test",
        )


async def test_disabled_agent_has_no_sdk_base_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())

    assert (
        await client_pool.build_agent_client_pool_plan(
            _template(prewarm_enabled=False), runtime_manager=_RuntimeManager()
        )
        is None
    )


async def test_agent_client_pool_requires_distributed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_pool,
        "load_astrabox_settings",
        lambda: _settings(agent_prewarm_redis_url=""),
    )

    with pytest.raises(APIError) as caught:
        await client_pool.build_agent_client_pool_plan(
            _template(), runtime_manager=_RuntimeManager()
        )

    assert caught.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert "REDIS" in caught.value.message.upper()


def _real_credential_plan() -> SandboxEgressCredentialPlan:
    return SandboxEgressCredentialPlan(
        model=(
            ModelEgressCredential(
                name="model",
                secret_value="real-model-secret",
                credential_header="Authorization",
                base_url="https://model.test",
                request_methods=("POST",),
                request_paths=("/v1/responses",),
                substitutions=(
                    ModelEgressCredentialSubstitution(
                        name="prepared-model",
                        secret_value="real-substitution-secret",
                        placeholder="prepared-placeholder",
                    ),
                ),
            ),
        ),
        environment=(
            EgressCredential(
                credential_id="environment-1",
                secret_name="SERVICE_TOKEN",
                secret_value="real-environment-secret",
                placeholder="environment-placeholder",
            ),
        ),
        mcp=(
            MCPHeaderEgressCredential(
                name="mcp-1",
                server_url="https://mcp.test",
                headers={"Authorization": "real-mcp-secret"},
            ),
        ),
        http_basic=(
            HTTPBasicEgressCredentialSet(
                scope_id="agent-vault",
                credentials=(HTTPBasicEgressCredential(
                    credential_id="git-1",
                    url="https://github.com/example/private-skills",
                    username="git-user",
                    password="real-git-secret",
                ),),
            ),
        ),
    )


@pytest.fixture
def _storage_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from astrabox.core.service.orchestrator.runtime.storage import mounts as storage_mounts

    async def backing(_assignment: str, mounts: Any) -> StorageMountPlan:
        return StorageMountPlan("backing-volume", mounts)

    async def routed(_assignment: str, plan: StorageMountPlan) -> StorageMountPlan:
        return StorageMountPlan("routed-volume", plan.mounts)

    monkeypatch.setattr(
        storage_mounts, "storage_provider",
        lambda: SimpleNamespace(provision_mounts=backing),
    )
    monkeypatch.setattr(storage_mounts.workspace_router, "provision_mounts", routed)
    monkeypatch.setattr(storage_mounts.workspace_router, "attach_sandbox", AsyncMock())


async def test_platform_creator_mounts_storage_and_applies_the_complete_credential_plan(
    monkeypatch: pytest.MonkeyPatch,
    _storage_routes: None,
) -> None:
    provider = _PlatformProvider()
    mounts = (("/home/agents/agent-1", "agents/agent-workspace"),)
    plan_mounts = AsyncMock(return_value=mounts)
    workspace_ready = AsyncMock()
    agent_mcp = AsyncMock(return_value=None)
    credential_delivery: dict[str, Any] = {}

    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: provider)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: _Adapter())
    monkeypatch.setattr(provisioning, "plan_workspace_mounts", plan_mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", workspace_ready)
    monkeypatch.setattr(
        provisioning,
        "workspace_ref_for_subject",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    def resolve_delivery(**kwargs: Any) -> tuple[str, None, SandboxEgressCredentialPlan]:
        credential_delivery.update(kwargs)
        return "model-placeholder", None, _real_credential_plan()

    monkeypatch.setattr(provisioning, "resolve_model_credential_delivery", resolve_delivery)

    async def prepared_environment(
        _template: Any,
        *,
        slot_id: str,
        vault_enabled: bool,
        vault_write: Any,
    ) -> tuple[Any, dict[str, str]]:
        assert slot_id == "pool-assignment"
        assert vault_enabled is False
        return vault_write, {"SERVICE_TOKEN": "environment-placeholder"}

    monkeypatch.setattr(
        provisioning,
        "resolve_prepared_environment_credentials",
        prepared_environment,
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.mcp_credentials.resolve_agent_mcp_credential_plan",
        agent_mcp,
    )

    template = _template(
        plugin_repos=[
            {
                "url": "https://github.com/anthropics/financial-services.git",
                "protocol": "https",
            }
        ],
        mcp_servers={
            "keyvex": {
                "type": "http",
                "url": "https://mcp.keyvex.com",
            }
        },
    )
    plan = await client_pool.build_agent_client_pool_plan(
        template, runtime_manager=_RuntimeManager()
    )
    assert plan is not None
    member = SandboxClientPoolMember(
        pool_name=plan.spec.pool_name,
        member_index=0,
        session_id="supplier-member-session",
        assignment_id="pool-assignment",
    )
    handle = await plan.creator(member)

    assert handle.sandbox_id == "base-box"
    assert len(provider.created_specs) == 1
    create_spec = provider.created_specs[0]
    assert create_spec.session_id == "supplier-member-session"
    assert create_spec.assignment_id == "pool-assignment"
    assert create_spec.workspace_mounts == mounts
    assert create_spec.workspace_volume == "routed-volume"
    assert create_spec.permission_level == "advanced"
    assert create_spec.resource_limits == {"cpu": "4", "memory": "4Gi"}
    assert create_spec.resource_requests == {"cpu": "200m", "memory": "768Mi"}
    plan_mounts.assert_awaited_once_with(
        subject_kind="deployment_runtime",
        session_id="",
        workspace_id=None,
        agent_id="agent-1",
        assistant_id=None,
        user_id=None,
        runtime_identity=None,
    )
    assert create_spec.env["MODEL_API_KEY"] == "model-placeholder"
    assert create_spec.env["SERVICE_TOKEN"] == "environment-placeholder"
    assert "real-model-secret" not in create_spec.env.values()
    assert "real-environment-secret" not in create_spec.env.values()
    assert "real-mcp-secret" not in create_spec.env.values()
    assert "real-git-secret" not in create_spec.env.values()
    vault = create_spec.vault_write
    assert vault is not None
    assert vault == _real_credential_plan()
    workspace_ready.assert_awaited_once()
    agent_mcp.assert_awaited_once_with(template=template, vault_enabled=False)
    assert credential_delivery["required_hosts"] == (
        "engine.vendor.test",
        "github.com",
    )
    assert credential_delivery["mcp_hosts"] == ("mcp.keyvex.com",)


async def test_platform_creator_cleans_up_a_box_whose_mount_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    _storage_routes: None,
) -> None:
    provider = _PlatformProvider()
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: provider)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: _Adapter())
    monkeypatch.setattr(
        provisioning,
        "plan_workspace_mounts",
        AsyncMock(return_value=(("/home/agents/agent-1", "agents/workspace"),)),
    )
    monkeypatch.setattr(
        provisioning,
        "workspace_is_ready",
        AsyncMock(side_effect=RuntimeError("durable mount is absent")),
    )
    monkeypatch.setattr(
        provisioning,
        "workspace_ref_for_subject",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_model_credential_delivery",
        lambda **_kwargs: ("model-placeholder", None, None),
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_prepared_environment_credentials",
        AsyncMock(return_value=(None, {})),
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.mcp_credentials.resolve_agent_mcp_credential_plan",
        AsyncMock(return_value=None),
    )
    plan = await client_pool.build_agent_client_pool_plan(
        _template(), runtime_manager=_RuntimeManager()
    )
    assert plan is not None

    with pytest.raises(RuntimeError, match="durable mount is absent"):
        await plan.creator(
            SandboxClientPoolMember(
                pool_name=plan.spec.pool_name,
                member_index=0,
                session_id="supplier-member-session",
                assignment_id="pool-assignment",
            )
        )

    assert provider.confirmed == ["base-box"]


async def test_platform_preparer_refuses_a_box_without_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()

    async def unavailable(_sandbox_id: str) -> Any:
        return SimpleNamespace(available=False, detail="user namespaces disabled")

    provider.read_isolation_capability = unavailable  # type: ignore[method-assign]
    plugins = AsyncMock()
    skills = AsyncMock()
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: provider)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: _Adapter())
    monkeypatch.setattr(client_pool, "resolve_runtime_profile", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(client_pool, "probe_sandbox_runtime_profile", AsyncMock())
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_plugin_cache", plugins)
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_skill_cache", skills)

    plan = await client_pool.build_agent_client_pool_plan(
        _template(), runtime_manager=_RuntimeManager()
    )
    assert plan is not None
    with pytest.raises(APIError) as caught:
        await plan.preparer(OpenSandboxHandle(_FakeSdkSandbox("base-box")))

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert "user namespaces disabled" in caught.value.message
    plugins.assert_not_awaited()
    skills.assert_not_awaited()


async def test_parallel_preparation_preserves_and_reports_the_original_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from astrabox.providers.open_sandbox.agent_pool import logger as pool_logger

    monkeypatch.setattr(pool_logger, "handlers", [*pool_logger.handlers, caplog.handler])
    provider = _PlatformProvider()
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: provider)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: _Adapter())
    monkeypatch.setattr(client_pool, "resolve_runtime_profile", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(client_pool, "probe_sandbox_runtime_profile", AsyncMock())
    monkeypatch.setattr(
        client_pool,
        "prepare_agent_runtime_plugin_cache",
        AsyncMock(side_effect=RuntimeError("clone failed without a secret")),
    )
    skills = AsyncMock()
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_skill_cache", skills)
    plan = await client_pool.build_agent_client_pool_plan(
        _template(), runtime_manager=_RuntimeManager()
    )
    assert plan is not None

    registry = OpenSandboxClientPoolRegistry(state_store=_FakeStateStore())
    with caplog.at_level("WARNING", logger=pool_logger.name):
        with pytest.raises(RuntimeError, match="^clone failed without a secret$"):
            await registry._preparer(plan.spec, plan.preparer)(_FakeSdkSandbox("base-box"))

    assert f"pool={plan.spec.pool_name}" in caplog.text
    assert "sandbox=base-box" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "error=clone failed without a secret" in caplog.text
    skills.assert_awaited_once()


async def test_platform_acquisition_adopts_box_into_the_agent_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()
    handle = OpenSandboxHandle(_FakeSdkSandbox("pooled-box"))
    provider.acquired = handle
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    acquired = await client_pool.acquire_agent_client_pool(
        _template(),
        runtime_manager=_RuntimeManager(),
        session_id="session-7",
        assignment_id="assignment-7",
    )

    assert acquired is not None
    assert acquired.sandbox is handle
    assert acquired.sandbox_id == "pooled-box"
    assert provider.events == [
        "acquire",
        "adopt:agent-runtime:agent-1:assignment-7",
        "isolation:pooled-box",
    ]


async def test_empty_platform_acquisition_returns_a_cold_miss_without_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    acquired = await client_pool.acquire_agent_client_pool(
        _template(),
        runtime_manager=_RuntimeManager(),
        session_id="session-7",
        assignment_id="assignment-7",
    )

    assert acquired is None
    assert provider.events == ["acquire"]


async def test_platform_destroys_an_acquired_box_when_adoption_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()
    provider.acquired = OpenSandboxHandle(_FakeSdkSandbox("pooled-box"))

    async def fail_adoption(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("metadata handoff failed")

    provider.adopt_sandbox_identity = fail_adoption  # type: ignore[method-assign]
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    with pytest.raises(RuntimeError, match="metadata handoff failed"):
        await client_pool.acquire_agent_client_pool(
            _template(),
            runtime_manager=_RuntimeManager(),
            session_id="session-7",
            assignment_id="assignment-7",
        )

    assert provider.confirmed == ["pooled-box"]


async def test_platform_destroys_an_acquired_box_that_lost_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()
    provider.acquired = OpenSandboxHandle(_FakeSdkSandbox("pooled-box"))

    async def unavailable(_sandbox_id: str) -> Any:
        provider.events.append("isolation:pooled-box")
        return SimpleNamespace(available=False, detail="isolation disappeared")

    provider.read_isolation_capability = unavailable  # type: ignore[method-assign]
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    with pytest.raises(APIError) as caught:
        await client_pool.acquire_agent_client_pool(
            _template(),
            runtime_manager=_RuntimeManager(),
            session_id="session-7",
            assignment_id="assignment-7",
        )

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert provider.events == [
        "acquire",
        "adopt:agent-runtime:agent-1:assignment-7",
        "isolation:pooled-box",
    ]
    assert provider.confirmed == ["pooled-box"]


async def test_platform_does_not_hide_a_supplier_coordination_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()

    async def unavailable(_spec: Any) -> Any:
        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
            message="redis unavailable",
            status_code=503,
        )

    provider.acquire_client_pool = unavailable  # type: ignore[method-assign]
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    with pytest.raises(APIError) as caught:
        await client_pool.acquire_agent_client_pool(
            _template(),
            runtime_manager=_RuntimeManager(),
            session_id="session-7",
            assignment_id="assignment-7",
        )

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"


# Platform placement ordering.


def _engine_request() -> provisioning.EngineSandboxRequest:
    return provisioning.EngineSandboxRequest(
        entrypoint=("/opt/astrabox/start",),
        credential=provisioning.ModelCredentialRequest(
            access=ResolvedModelAccess(
                configuration={},
                base_url="https://model.test",
                model_name="model",
                credential="secret",
                credential_kind="bearer",
                endpoint_provider="test",
            ),
            request_paths=("responses",),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message="missing model access",
        ),
        credential_env_var="MODEL_API_KEY",
    )


async def test_exact_assignment_recovery_precedes_prepared_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    result = object()
    recovered_handle = OpenSandboxHandle(_FakeSdkSandbox("recovered-box"))

    class _Manager:
        async def resolve_runtime_sandbox_backend(
            self, session_id: str, *, workspace_plan: Any
        ) -> str:
            assert session_id == "session-1"
            return "open_sandbox"

    class _Backend:
        name = "open_sandbox"
        supports_correlated_create = True

        async def find_sandbox_by_assignment(self, assignment_id: str) -> Any:
            events.append("recover-assignment")
            assert assignment_id == "assignment-1"
            return SimpleNamespace(
                sandbox_id="recovered-box",
                # Provider metadata is a wire identity. The later provider
                # create validates ownership against the platform identity.
                session_id="s-provider-projection",
            )

    async def reconcile(*_args: Any, **_kwargs: Any) -> str:
        return "a" * 64

    claim = AsyncMock(side_effect=AssertionError("prepared claim ran before recovery"))

    async def shared(*_args: Any, **kwargs: Any) -> Any:
        events.append("shared-placement")
        assert kwargs["recover_assignment"] is True
        return (
            recovered_handle,
            "recovered-box",
            {"sandbox_tenancy": "agent", "workspace_dir": "/workspace"},
            "ws://runner",
        )

    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.agent.runtime_generation.reconcile_runtime_generation",
        reconcile,
    )
    monkeypatch.setattr(
        "astrabox.seams.sandbox.sandbox_for_name",
        lambda _name: _Backend(),
    )
    monkeypatch.setattr(
        provisioning,
        "plan_conversation_identity",
        lambda **_kwargs: {
            "sandbox_tenancy": "agent",
            "workspace_dir": "/workspace",
        },
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_mcp_credential_plan",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_model_credential_delivery",
        lambda **_kwargs: ("placeholder", None, None),
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_session_environment_credentials",
        AsyncMock(return_value=(None, {})),
    )
    monkeypatch.setattr(provisioning, "_session_log_declaration", lambda _template: None)
    monkeypatch.setattr(provisioning, "claim_prepared_engine_sandbox", claim)
    monkeypatch.setattr(provisioning, "_provision_shared_conversation", shared)
    monkeypatch.setattr(
        provisioning,
        "_assemble_provisioned_sandbox",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        provisioning,
        "load_astrabox_settings",
        lambda: _settings(),
    )

    provisioned = await provisioning.provision_engine_sandbox(
        _Manager(),
        session_id="session-1",
        assignment_id="assignment-1",
        template=_template(),
        workspace_plan=RuntimeWorkspacePlan(
            subject_kind="deployment_conversation",
            session_kind="agent_chat",
            operation="runtime_start",
            runtime_key="session-1",
            conversation_session_id="session-1",
            cwd="/workspace",
            resume_engine_session_key=None,
            sandbox_id=None,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind="claude_code",
            user_id="user-1",
            agent_id="agent-1",
        ),
        user_id="user-1",
        callback_url=None,
        request=_engine_request(),
    )

    assert provisioned is result
    assert events == ["recover-assignment", "shared-placement"]
    claim.assert_not_awaited()


async def test_exact_assignment_recovery_skips_the_sdk_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecoveredCreateReached(RuntimeError):
        pass

    class _SessionRepo:
        async def get_session(self, _session_id: str) -> None:
            return None

    monkeypatch.setattr(provisioning, "SessionRepository", _SessionRepo)
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.conversation_identity."
        "conversation_identity_from_plan",
        lambda _identity, _workspace: SimpleNamespace(
            home_dir="/home/conversations/session-1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/session-1/workspace",
        ),
    )
    monkeypatch.setattr(
        "astrabox.persistence.repository.agent_repository.AgentRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.shared_sandbox_lease.SharedSandboxLease",
        lambda **_kwargs: object(),
    )
    acquire = AsyncMock(side_effect=AssertionError("SDK pool was consulted"))
    monkeypatch.setattr(client_pool, "acquire_agent_client_pool", acquire)
    monkeypatch.setattr(
        provisioning,
        "create_shared_agent_sandbox",
        AsyncMock(side_effect=_RecoveredCreateReached("reconnect exact create")),
    )

    with pytest.raises(_RecoveredCreateReached):
        await provisioning._provision_shared_conversation(
            object(),
            SimpleNamespace(name="open_sandbox"),
            session_id="session-1",
            assignment_id="assignment-1",
            template=_template(),
            workspace_plan=object(),
            user_id="user-1",
            request=_engine_request(),
            runtime_identity={"sandbox_tenancy": "agent"},
            cwd="/workspace",
            credential="placeholder",
            runtime_env={},
            network_policy=None,
            vault_write=None,
            session_log=None,
            recover_assignment=True,
        )

    acquire.assert_not_awaited()
