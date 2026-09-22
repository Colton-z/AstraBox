"""The platform's shared-conversation placement recipe, pinned by order.

Placement opens two provider resources before returning. Their exact ids must
be named durably before connect, Vault, workspace, or engine startup can fail.
An unpublished candidate that lost the Agent-row race is a separate resource;
its cleanup must not depend on connecting to the winning box.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.seams.sandbox import SandboxAllocation
from astrabox.seams.sandbox_disposal import SandboxDestruction


class _Files:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.written: dict[str, dict[str, Any]] = {}

    async def write_file(
        self,
        path: str,
        body: bytes,
        *,
        mode: int,
        owner: str,
        group: str,
    ) -> None:
        self.log.append(f"write:{path}")
        self.written[path] = {
            "body": body,
            "mode": mode,
            "owner": owner,
            "group": group,
        }


class _Handle:
    def __init__(self, sandbox_id: str, log: list[str]) -> None:
        self.files = _Files(log)
        self.sandbox_id = sandbox_id


class _Backend:
    name = "open_sandbox"

    def __init__(
        self,
        log: list[str],
        *,
        connect_failure: BaseException | None = None,
    ) -> None:
        self.log = log
        self.connect_failure = connect_failure
        self.handles: dict[str, _Handle] = {}

    def handle(self, sandbox_id: str) -> _Handle:
        return self.handles.setdefault(sandbox_id, _Handle(sandbox_id, self.log))

    async def connect(self, sandbox_id: str) -> _Handle:
        self.log.append(f"connect:{sandbox_id}")
        if self.connect_failure is not None:
            raise self.connect_failure
        return self.handle(sandbox_id)

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        self.log.append(f"destroy:{sandbox_id}")
        return SandboxDestruction.confirmed_gone(
            sandbox_id,
            detail="provider confirmed candidate deletion",
        )

    async def apply_credential_vault(
        self,
        sandbox: Any,
        *,
        vault_write: Any,
        create_if_missing: bool,
    ) -> None:
        assert sandbox.sandbox_id
        assert vault_write is not None
        assert create_if_missing is False
        self.log.append("vault")


class _Lease:
    def __init__(self, log: list[str], placements: tuple[Any, ...]) -> None:
        self.log = log
        self.placements = list(placements)

    async def place_in_agent_box(self, **kwargs: Any) -> Any:
        candidate = str(kwargs.get("candidate") or "resident")
        self.log.append(f"place:{candidate}")
        return self.placements.pop(0)

    async def start_runner(
        self,
        binding: Any,
        *,
        launch: str,
        engine_label: str,
    ) -> str:
        assert binding is _PLACED
        assert launch == "launch shared engine"
        assert engine_label == "deepseek_harness"
        self.log.append("launch")
        return "9001"


class _Workspace:
    provisioned_runtime_identity = None

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def mount_and_provision(self, *args: Any, **kwargs: Any) -> list[str]:
        self.log.append("mount_and_provision")
        return []


class _Manager(RemoteAgentRuntimeManager):
    def __init__(
        self,
        log: list[str],
        *,
        record_failure: BaseException | None = None,
    ) -> None:
        self.log = log
        self.record_failure = record_failure
        self.allocations: list[Any] = []
        super().__init__(
            sessions_repo=SimpleNamespace(
                record_startup_allocation=AsyncMock(side_effect=self._persist_allocation)
            )
        )

    async def _persist_allocation(
        self,
        _session_id: str,
        allocation: dict[str, Any],
        *,
        replaces: dict[str, Any] | None = None,
    ) -> None:
        if allocation["scope"] == "isolated_sessions" and self.record_failure is not None:
            raise self.record_failure

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        replaces: SandboxAllocation | None = None,
    ) -> None:
        assert session_id == "session-1"
        self.log.append(
            "record_candidate" if allocation.scope == "sandbox" else "record_allocation"
        )
        self.allocations.append(allocation)
        await super().record_startup_allocation(session_id, allocation, replaces=replaces)


class _SessionRepo:
    async def get_session(self, _session_id: str) -> None:
        return None


class _Engine:
    def shared_conversation_service_launch(
        self,
        *,
        home: str,
        workspace: str,
        port: int,
    ) -> str:
        assert home == _PLACED.home_dir
        assert workspace == _PLACED.workspace_dir
        assert port > 0
        return "launch shared engine"


_PLACED = SimpleNamespace(
    sandbox_id="box-1",
    uid=2001,
    gid=2001,
    isolated_session_id="iso-1",
    terminal_isolated_session_id="iso-term-1",
    home_dir="/home/conversations/conv_abc",
    workspace_dir="/workspace",
)

_IDENTITY = {
    "sandbox_tenancy": "agent",
    "agent_id": "agent-1",
    "linux_user": "conv_abc",
    "home_dir": "/home/conversations/conv_abc",
    "workspace_dir": "/workspace",
    "workspace_source_dir": "/home/conversations/conv_abc/workspace",
}

_PLAN = SimpleNamespace(
    subject_kind="deployment_conversation",
    agent_id="agent-1",
    assistant_id=None,
    engine_kind="deepseek_harness",
)


def _request() -> EngineSandboxRequest:
    return EngineSandboxRequest(
        entrypoint=("/opt/gem/run.sh",),
        credential=ModelCredentialRequest(
            access=None,
            request_paths=("chat/completions",),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message="no model access",
        ),
        credential_env_var="DEEPSEEK_API_KEY",
        cwd_env_var="ASTRABOX_WORKSPACE",
        env={"DEEPSEEK_BASE_URL": "https://gw.test"},
    )


def _build_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    placements: tuple[Any, ...] = (_PLACED,),
    candidate_id: str | None = None,
    record_failure: BaseException | None = None,
    connect_failure: BaseException | None = None,
    vault_write: Any = None,
) -> Any:
    log: list[str] = []
    manager = _Manager(log, record_failure=record_failure)
    backend = _Backend(log, connect_failure=connect_failure)
    lease = _Lease(log, placements)
    candidate = backend.handle(candidate_id) if candidate_id is not None else None

    async def acquire_candidate(*_args: Any, **_kwargs: Any) -> Any:
        if candidate is None:
            return None
        await manager.record_startup_allocation(
            "session-1",
            SandboxAllocation(
                sandbox_id=str(candidate_id),
                sandbox_backend=backend.name,
                scope="sandbox",
            ),
        )
        return SimpleNamespace(sandbox=candidate, sandbox_id=candidate_id)

    acquire = AsyncMock(side_effect=acquire_candidate)

    monkeypatch.setattr(provisioning, "SessionRepository", _SessionRepo)
    monkeypatch.setattr(
        "astrabox.persistence.repository.agent_repository.AgentRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.shared_sandbox_lease."
        "SharedSandboxLease",
        lambda **_kwargs: lease,
    )
    monkeypatch.setattr(client_pool, "acquire_agent_client_pool", acquire)
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime_manager.sandbox_for_name",
        lambda _name: backend,
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.workspace.workspace_from_subject_kind",
        lambda *args, **kwargs: _Workspace(log),
    )

    async def bind_mirror(*args: Any, **kwargs: Any) -> None:
        log.append(f"bind_mirror:{kwargs.get('target_file')}:{kwargs.get('owner')}")

    async def resolve_endpoint(_sandbox: Any, port: int) -> str:
        assert port == 9001
        log.append("endpoint")
        return "ws://box:9001/"

    monkeypatch.setattr(provisioning.transcript_mirror, "bind_mirror_target", bind_mirror)
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.engine.registry.get_engine_adapter",
        lambda _kind: _Engine(),
    )
    monkeypatch.setattr(provisioning, "resolve_sandbox_websocket_endpoint", resolve_endpoint)

    async def run(
        *,
        session_log: Any = object(),
        progress_callback: Any = None,
        recover_assignment: bool = False,
    ) -> Any:
        return await provisioning._provision_shared_conversation(
            manager,
            backend,
            session_id="session-1",
            assignment_id="assignment-1",
            template=SimpleNamespace(
                agent_id="agent-1",
                engine_kind="deepseek_harness",
                runtime_generation="generation-1",
            ),
            workspace_plan=_PLAN,
            user_id="user-1",
            request=_request(),
            runtime_identity=dict(_IDENTITY),
            cwd="/workspace",
            credential="placeholder-cred",
            runtime_env={},
            network_policy=None,
            vault_write=vault_write,
            session_log=session_log,
            progress_callback=progress_callback,
            recover_assignment=recover_assignment,
        )

    return SimpleNamespace(
        log=log,
        manager=manager,
        backend=backend,
        acquire=acquire,
        run=run,
    )


async def test_placement_is_recorded_before_every_fallible_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(monkeypatch, vault_write=object())

    sandbox, sandbox_id, identity, runner_uri = await harness.run()

    assert sandbox_id == "box-1"
    assert sandbox.sandbox_id == sandbox_id
    assert runner_uri == "ws://box:9001/"
    assert harness.log.index("place:resident") < harness.log.index("record_allocation")
    for later in ("connect:box-1", "vault", "mount_and_provision", "launch"):
        assert harness.log.index("record_allocation") < harness.log.index(later)
    assert harness.log.count("vault") == 1
    assert identity["uid"] == 2001
    assert identity["isolated_session_id"] == "iso-1"
    allocation = harness.manager.allocations[0]
    assert allocation.scope == "isolated_sessions"
    assert allocation.isolated_session_ids == ("iso-1", "iso-term-1")
    harness.acquire.assert_not_awaited()


async def test_shared_cold_placement_reports_mounting_immediately_before_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(monkeypatch)

    async def progress(stage: str) -> None:
        harness.log.append(f"progress:{stage}")

    await harness.run(progress_callback=progress)

    mount = harness.log.index("mount_and_provision")
    assert harness.log[mount - 1 : mount + 1] == [
        "progress:mounting_nas",
        "mount_and_provision",
    ]
    assert harness.log.count("progress:mounting_nas") == 1


async def test_a_new_cold_winner_does_not_receive_the_same_vault_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_write = object()
    harness = _build_harness(
        monkeypatch,
        placements=(None, _PLACED),
        vault_write=vault_write,
    )

    async def create(*_args: Any, **kwargs: Any) -> Any:
        assert kwargs["vault_write"] is vault_write
        harness.log.append("create_with_vault")
        return harness.backend.handle("box-1"), "box-1"

    monkeypatch.setattr(provisioning, "create_shared_agent_sandbox", create)

    await harness.run()

    assert "create_with_vault" in harness.log
    assert "vault" not in harness.log


@pytest.mark.parametrize("recover_assignment", [False, True])
async def test_pool_and_recovered_candidates_receive_the_vault_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    recover_assignment: bool,
) -> None:
    vault_write = object()
    deliveries: list[str] = []
    harness = _build_harness(
        monkeypatch,
        placements=(_PLACED,) if recover_assignment else (None, _PLACED),
        candidate_id=None if recover_assignment else "box-1",
        vault_write=vault_write,
    )

    original_patch = harness.backend.apply_credential_vault

    async def patch(*args: Any, **kwargs: Any) -> None:
        deliveries.append("placement-patch")
        await original_patch(*args, **kwargs)

    monkeypatch.setattr(harness.backend, "apply_credential_vault", patch)

    if recover_assignment:

        async def recover(*_args: Any, **kwargs: Any) -> Any:
            assert kwargs["vault_write"] is vault_write
            harness.log.append("recover_assignment")
            deliveries.append("create-seam")
            return harness.backend.handle("box-1"), "box-1"

        monkeypatch.setattr(provisioning, "create_shared_agent_sandbox", recover)

    await harness.run(recover_assignment=recover_assignment)

    assert deliveries == (
        ["create-seam"] if recover_assignment else ["placement-patch"]
    )


async def test_a_cold_candidate_that_loses_the_race_refreshes_the_winner_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_write = object()
    harness = _build_harness(
        monkeypatch,
        placements=(None, _PLACED),
        vault_write=vault_write,
    )

    async def create(*_args: Any, **kwargs: Any) -> Any:
        assert kwargs["vault_write"] is vault_write
        harness.log.append("create_with_vault")
        return harness.backend.handle("box-loser"), "box-loser"

    monkeypatch.setattr(provisioning, "create_shared_agent_sandbox", create)

    await harness.run()

    assert "destroy:box-loser" in harness.log
    assert "connect:box-1" in harness.log
    assert harness.log.count("vault") == 1


async def test_unused_candidate_cleanup_does_not_depend_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(
        monkeypatch,
        placements=(None, _PLACED),
        candidate_id="box-loser",
        connect_failure=RuntimeError("winning box unavailable"),
    )

    with pytest.raises(RuntimeError, match="winning box unavailable"):
        await harness.run()

    assert harness.log == [
        "place:resident",
        "record_candidate",
        "place:box-loser",
        "record_allocation",
        "destroy:box-loser",
        "connect:box-1",
    ]


async def test_record_failure_still_cleans_the_unused_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(
        monkeypatch,
        placements=(None, _PLACED),
        candidate_id="box-loser",
        record_failure=RuntimeError("startup allocation write failed"),
    )

    with pytest.raises(RuntimeError, match="startup allocation write failed"):
        await harness.run()

    assert harness.log == [
        "place:resident",
        "record_candidate",
        "place:box-loser",
        "record_allocation",
        "destroy:box-loser",
    ]


async def test_engine_env_and_mirror_are_delivered_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(monkeypatch)

    sandbox, _sandbox_id, _identity, _runner_uri = await harness.run()

    path = "/home/conversations/conv_abc/.astrabox-engine-env"
    record = sandbox.files.written[path]
    assert record["owner"] == "conv_abc"
    assert record["group"] == "conv_abc"
    assert record["mode"] == 600
    body = record["body"].decode("utf-8")
    assert "export DEEPSEEK_API_KEY=placeholder-cred" in body
    assert "export DEEPSEEK_BASE_URL=https://gw.test" in body
    assert "export ASTRABOX_WORKSPACE=/workspace" in body
    env_write = next(
        index for index, item in enumerate(harness.log) if item.startswith("write:")
    )
    mirror = next(
        index
        for index, item in enumerate(harness.log)
        if item.startswith("bind_mirror:")
    )
    assert env_write < harness.log.index("launch")
    assert mirror < harness.log.index("launch")


async def test_engine_without_session_log_binds_no_mirror_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(monkeypatch)

    await harness.run(session_log=None)

    assert not any(item.startswith("bind_mirror:") for item in harness.log)
