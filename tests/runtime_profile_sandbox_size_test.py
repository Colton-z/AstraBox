"""A RuntimeProfile sizes every box created from it, and saving refuses a size no box could have.

An administrator gives a RuntimeProfile its own CPU and memory. Every box of
the profile is created with them, whichever kind it is; the sandbox quota
charges them; the Agent's prepared generation changes with them, so capacity
prepared at the old size retires; and a size the deployment could never create
is refused when the profile is saved, not at the first conversation.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.providers.secret_store  # noqa: F401 - registers the local secret store
import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 - registers the claude_code engine
import astrabox.providers.open_sandbox.sandbox as open_sandbox
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import cpu_quantity
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.agent import runtime_generation
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.engine.provisioning import sandbox_create_resources
from astrabox.core.service.orchestrator.runtime import sandbox_quota
from astrabox.core.service.orchestrator.runtime.storage.mounts import create_sandbox_with_storage
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.sandbox import SandboxCreateSpec, SandboxDescriptor, SandboxPage

pytestmark = pytest.mark.usefixtures("database")

_GIB = 2**30
_SIDECAR_MEMORY = 128 * 2**20
_USER = UserContext(user_id="admin", roles=["admin"])


# ── the create recipe ────────────────────────────────────────────────────────


def test_a_profile_size_replaces_the_deployment_limits_of_every_kind_of_box() -> None:
    # A build profile on each kind of box: the profile's numbers are the limits
    # whatever the kind, its memory is also reserved, and the CPU reservation
    # stays the deployment's. Unset, nothing changes.
    build = AgentView(sandbox_cpu="8", sandbox_memory="16Gi")
    for kind in ({"shared": False}, {"shared": True}, {"shared": False, "assistant": True}):
        _, deployment_requests = sandbox_create_resources(None, **kind)
        limits, requests = sandbox_create_resources(build, **kind)
        assert limits == {"cpu": "8", "memory": "16Gi"}, kind
        assert requests == {"cpu": deployment_requests["cpu"], "memory": "16Gi"}, kind
        assert sandbox_create_resources(AgentView(), **kind) == sandbox_create_resources(None, **kind)

    # A CPU below the deployment's reservation lowers the reservation with it:
    # Kubernetes refuses a request above the limit. Memory left to the
    # deployment keeps the deployment's limit and request.
    deployment_limits, deployment_requests = sandbox_create_resources(None, shared=True)
    limits, requests = sandbox_create_resources(AgentView(sandbox_cpu="250m"), shared=True)
    assert limits == {"cpu": "250m", "memory": deployment_limits["memory"]}
    assert requests == {"cpu": "250m", "memory": deployment_requests["memory"]}


# ── what the quota charges ───────────────────────────────────────────────────


class _Backend:
    """A backend whose inventory lists the boxes it created, each with its create metadata."""

    name = "sized-backend"
    supports_inventory_listing = True
    supports_correlated_create = False

    def __init__(self) -> None:
        self.created: dict[str, SandboxCreateSpec] = {}
        self.listed: set[str] = set()

    def per_box_overhead(self) -> tuple[float, int]:
        return 1.0, _SIDECAR_MEMORY

    def paused_box_releases_resources(self) -> bool:
        return True

    async def list_sandboxes(self, *, page: int, page_size: int, metadata: dict[str, str]) -> SandboxPage:
        items = tuple(
            SandboxDescriptor(
                sandbox_id=sandbox_id, state="RUNNING", metadata=dict(self.created[sandbox_id].metadata)
            )
            for sandbox_id in sorted(self.listed)
        )
        return SandboxPage(
            items=items, page=1, page_size=page_size, total_items=len(items), total_pages=1,
            has_next_page=False,
        )

    async def create_sandbox(self, spec: SandboxCreateSpec) -> Any:
        sandbox_id = f"box-{len(self.created) + 1}"
        self.created[sandbox_id] = spec
        self.listed.add(sandbox_id)
        return SimpleNamespace(sandbox_id=sandbox_id)

    async def describe_sandbox(self, sandbox_id: str) -> SandboxDescriptor:
        return SandboxDescriptor(
            sandbox_id=sandbox_id, state="PAUSED", metadata=dict(self.created[sandbox_id].metadata)
        )


def _spec(template: AgentView, index: int) -> SandboxCreateSpec:
    limits, requests = sandbox_create_resources(template, shared=False)
    return SandboxCreateSpec(
        session_id=f"s-{index}",
        assignment_id=f"a-{index}",
        environment_name="default",
        resource_limits=limits,
        resource_requests=requests,
    )


async def test_the_quota_charges_a_box_what_its_profile_sized_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 16Gi build box is charged 16Gi and its sidecar, not the deployment's
    # 1Gi box: a second one does not fit under a 20Gi ceiling, which ten
    # default boxes would. The refusal reconciles the ledger first, so the
    # inventory's own charge for the first box decides it.
    backend = _Backend()
    monkeypatch.setitem(
        sandbox_quota._QUOTAS, backend.name, sandbox_quota.SandboxQuota(memory=20 * _GIB)
    )
    build = AgentView(sandbox_cpu="8", sandbox_memory="16Gi")
    charge = 16 * _GIB + _SIDECAR_MEMORY

    await create_sandbox_with_storage(backend, _spec(build, 1), reason="conversation")
    [entry] = await sandbox_quota.quota_report({backend.name: backend})
    assert (entry["usage"]["memory_bytes"], entry["usage"]["cpu"]) == (charge, 9.0)

    with pytest.raises(sandbox_quota.SandboxCapacityExhausted) as refused:
        await create_sandbox_with_storage(backend, _spec(build, 2), reason="conversation")
    assert refused.value.ceiling == "memory"
    [entry] = await sandbox_quota.quota_report({backend.name: backend})
    assert [(box["sandbox_id"], box["memory"], box["cpu"]) for box in entry["boxes"]] == [
        ("box-1", charge, 9.0)
    ]

    # A box paused and released before any inventory listed it takes back, on
    # resume, what its create recorded, read from the box itself.
    monkeypatch.setitem(sandbox_quota._QUOTAS, backend.name, sandbox_quota.SandboxQuota())
    await create_sandbox_with_storage(backend, _spec(build, 3), reason="conversation")
    backend.listed.discard("box-2")
    assert await sandbox_quota.reserve_resume(backend, "box-2") is not None
    [entry] = await sandbox_quota.quota_report({backend.name: backend})
    [resume] = [r for r in entry["reservations"] if r["reason"] == "resume"]
    assert (resume["memory"], resume["cpu"]) == (charge, 9.0)


# ── the prepared generation ──────────────────────────────────────────────────


async def test_the_prepared_generation_changes_with_the_profile_size() -> None:
    manager = SimpleNamespace(
        resolve_model_access=lambda _configuration: ResolvedModelAccess(
            configuration={"model": "m"},
            base_url="https://gateway.test",
            model_name="m",
            credential="sk-test",
            credential_kind="bearer",
            endpoint_provider="test",
        ),
        deployment_settings=SimpleNamespace(),
    )

    def agent(tenancy: str, **size: str) -> AgentView:
        return AgentView(
            agent_id="agent-1",
            engine_kind="claude_code",
            model_config={},
            networking=None,
            runtime_template_name="astrabox/sandbox-claude-code:latest",
            sandbox_backend="open_sandbox",
            sandbox_tenancy=tenancy,
            **size,
        )

    for tenancy in ("conversation", "agent"):
        unsized = await runtime_generation.runtime_generations(agent(tenancy), runtime_manager=manager)
        assert unsized == await runtime_generation.runtime_generations(
            agent(tenancy), runtime_manager=manager
        )
        generations = [
            unsized,
            await runtime_generation.runtime_generations(
                agent(tenancy, sandbox_memory="16Gi"), runtime_manager=manager
            ),
            await runtime_generation.runtime_generations(
                agent(tenancy, sandbox_memory="32Gi"), runtime_manager=manager
            ),
            await runtime_generation.runtime_generations(
                agent(tenancy, sandbox_cpu="8"), runtime_manager=manager
            ),
        ]
        # Both the engine generation and the base box's change with each size.
        assert len({engine for engine, _ in generations}) == 4, tenancy
        assert len({box for _, box in generations}) == 4, tenancy


# ── saving ───────────────────────────────────────────────────────────────────


@pytest.fixture
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Docker host of 12 CPUs and 64Gi under a 40Gi, 10-CPU sandbox quota."""
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"s" * 32).decode("ascii")
    )
    provider = open_sandbox.OpenSandboxSandboxProvider

    def fit(self: Any, limits: Any, requests: Any) -> tuple[dict[str, str], dict[str, str]]:
        # The bundled Docker server's narrowing, on a 12-CPU host.
        fitted = dict(limits)
        if cpu_quantity(fitted["cpu"]) > 12:
            fitted["cpu"] = "12"
        return fitted, dict(requests)

    async def host_memory(self: Any) -> int:
        return 64 * _GIB

    monkeypatch.setattr(provider, "fit_resources", fit)
    monkeypatch.setattr(provider, "host_memory_bytes", host_memory)
    monkeypatch.setattr(provider, "per_box_overhead", lambda self: (1.0, _SIDECAR_MEMORY))
    monkeypatch.setitem(
        sandbox_quota._QUOTAS,
        open_sandbox.OPEN_SANDBOX_BACKEND_NAME,
        sandbox_quota.SandboxQuota(memory=40 * _GIB, cpu=10.0, sources={"memory": "configured"}),
    )


def _svc() -> AgentConfigService:
    return AgentConfigService(
        agent_repo=object(),  # type: ignore[arg-type]  # unused on the RuntimeProfile path
        runtime_profile_repo=RuntimeProfileRepository(),
        assistant_repo=object(),  # type: ignore[arg-type]  # unused
    )


@pytest.mark.usefixtures("_deployment")
async def test_saving_refuses_a_size_the_deployment_could_never_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _svc()
    saved = await svc.upsert_runtime_profile_config(
        _USER, "build", {"engine_kind": "claude_code", "sandbox_cpu": "8", "sandbox_memory": "16Gi"}
    )
    assert (saved["sandbox_cpu"], saved["sandbox_memory"]) == ("8", "16Gi")
    stored = await svc.get_runtime_profile("build")
    assert stored is not None and (stored["sandbox_cpu"], stored["sandbox_memory"]) == ("8", "16Gi")

    refusals = {
        # A unit left off: two bytes.
        ("sandbox_memory", "16"): "below 1Gi",
        ("sandbox_cpu", "100m"): "below 250m",
        ("sandbox_cpu", "two"): "not a CPU quantity",
        # 40Gi of quota cannot hold 40Gi and its sidecar.
        ("sandbox_memory", "40Gi"): "ASTRABOX_SANDBOX_QUOTA_MEMORY",
        # 10 CPUs of quota cannot hold 10 and the sidecar's one.
        ("sandbox_cpu", "10"): "ASTRABOX_SANDBOX_QUOTA_CPU",
        # More CPU than the Docker host has would be lowered to 12.
        ("sandbox_cpu", "14"): "it would create the box with 12",
    }
    for (field, value), reason in refusals.items():
        with pytest.raises(APIError) as refused:
            await svc.upsert_runtime_profile_config(
                _USER, "build", {"engine_kind": "claude_code", field: value}
            )
        assert refused.value.code == "INVALID_REQUEST", (field, value)
        # The console marks the field a refusal starts with.
        assert refused.value.message.startswith(field), refused.value.message
        assert reason in refused.value.message, refused.value.message

    # Without a memory ceiling, the host's memory is still one.
    monkeypatch.setitem(
        sandbox_quota._QUOTAS, open_sandbox.OPEN_SANDBOX_BACKEND_NAME, sandbox_quota.SandboxQuota()
    )
    with pytest.raises(APIError) as refused:
        await svc.upsert_runtime_profile_config(
            _USER, "build", {"engine_kind": "claude_code", "sandbox_memory": "96Gi"}
        )
    assert "64.00Gi of the host" in refused.value.message

    # Every refusal left the saved size in place.
    stored = await svc.get_runtime_profile("build")
    assert stored is not None and (stored["sandbox_cpu"], stored["sandbox_memory"]) == ("8", "16Gi")
