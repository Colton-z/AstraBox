"""Slot-identity provisioning: what a box prepared before its Session omits.

The Session-bound residues named in ``provision_engine_slot_sandbox``'s
docstring are each held here: slot id as create identity, no allocation
record, deferred mirror target, and the per-slot gateway placeholder with its
substitution behind it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    codex as _codex_engine,  # module import registers the adapter
)
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.engine import transcript_mirror
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    EgressCredential,
    SandboxEgressCredentialPlan,
    mint_placeholder,
    workload_placeholder_context,
    workload_credential_name,
    workload_model_placeholder,
)
from astrabox.seams.model import ResolvedModelAccess


def _credential(**overrides: Any) -> provisioning.ModelCredentialRequest:
    return provisioning.ModelCredentialRequest(
        **{
            "access": ResolvedModelAccess(
                configuration={"model": "m"},
                base_url="https://gateway.test",
                model_name="m",
                credential="sk-shared-key",
                credential_kind="bearer",
                endpoint_provider="test",
            ),
            "request_paths": ("responses",),
            "missing_code": "ENGINE_CAPABILITY_UNAVAILABLE",
            "missing_message": "no model access",
            **overrides,
        }
    )


class _Backend:
    name = "fake"
    supports_correlated_create = True
    supports_create_network_policy = True
    supports_egress_credential_injection = True

    def __init__(self) -> None:
        self.spec: Any = None

    async def find_sandbox_by_assignment(self, _assignment_id: str) -> None:
        return None

    async def create_sandbox(self, spec: Any) -> Any:
        self.spec = spec
        return SimpleNamespace(sandbox_id="box-slot-1", id="box-slot-1")


class _Manager:
    def __init__(self) -> None:
        self.tracked: list[Any] = []

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.tracked.append((session_id, allocation))

    @property
    def deployment_settings(self) -> Any:
        return SimpleNamespace(mcp_proxy_base_url="http://backend.slot.test:8000")


def _template() -> AgentView:
    return AgentView(
        engine_kind="codex",
        model_config={},
        networking=None,
        runtime_template_name="astrabox/sandbox-codex:latest",
        sandbox_backend="open_sandbox",
    )


def _vault_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provisioning,
        "load_astrabox_settings",
        lambda: SimpleNamespace(sandbox_credential_vault_enabled=True),
    )


def _vault_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provisioning,
        "load_astrabox_settings",
        lambda: SimpleNamespace(sandbox_credential_vault_enabled=False),
    )


# ── an empty eligible pool seeds its replacement ─────────────────────────


@pytest.mark.asyncio
async def test_an_eligible_claim_miss_schedules_the_next_prepared_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    claim = AsyncMock(return_value=None)
    refill = Mock()
    monkeypatch.setattr(prepared_slots, "claim_prepared_slot", claim)
    monkeypatch.setattr(prepared_slots, "schedule_prepared_runtime_refill", refill)
    manager = object()
    template = SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        prewarm_enabled=True,
        runtime_generation="generation-1",
    )

    result = await provisioning.claim_prepared_engine_sandbox(
        manager,
        session_id="session-1",
        assignment_id="assignment-1",
        template=template,
        workspace_plan=SimpleNamespace(resume_engine_session_key=""),
        user_id="user-1",
        request=SimpleNamespace(credential=object()),
        backend_adapter=object(),
        credential="model-placeholder",
        mcp_vault_write=None,
        vault_enabled=True,
        session_log=None,
    )

    assert result is None
    claim.assert_awaited_once_with(
        agent_id="agent-1",
        session_id="session-1",
        expected_runtime_generation="generation-1",
    )
    refill.assert_called_once_with(template, manager)

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resume_key", "prewarm_enabled", "agent_id", "runtime_generation", "workspace_volume"),
    [
        ("native-session", True, "agent-1", "generation-1", ""),
        ("native-session", True, "agent-1", "generation-1", "workspace-pvc"),
        ("", False, "agent-1", "generation-1", ""),
        ("", True, "", "generation-1", ""),
        ("", True, "agent-1", "", ""),
    ],
    ids=["resume-no-volume", "resume-shared-volume", "prewarm-disabled", "missing-agent", "missing-generation"],
)
async def test_prepared_refill_distinguishes_resumable_and_ineligible_starts(
    monkeypatch: pytest.MonkeyPatch,
    resume_key: str,
    prewarm_enabled: bool,
    agent_id: str,
    runtime_generation: str,
    workspace_volume: str,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", workspace_volume)
    claim = AsyncMock(return_value=None)
    refill = Mock()
    monkeypatch.setattr(prepared_slots, "claim_prepared_slot", claim)
    monkeypatch.setattr(prepared_slots, "schedule_prepared_runtime_refill", refill)
    manager = object()
    template = SimpleNamespace(
        agent_id=agent_id,
        engine_kind="codex",
        prewarm_enabled=prewarm_enabled,
        runtime_generation=runtime_generation,
    )

    result = await provisioning.claim_prepared_engine_sandbox(
        manager,
        session_id="session-1",
        assignment_id="assignment-1",
        template=template,
        workspace_plan=SimpleNamespace(resume_engine_session_key=resume_key),
        user_id="user-1",
        request=SimpleNamespace(credential=object()),
        backend_adapter=object(),
        credential="model-placeholder",
        mcp_vault_write=None,
        vault_enabled=True,
        session_log=None,
    )

    assert result is None
    if resume_key and not workspace_volume:
        claim.assert_awaited_once_with(
            agent_id=agent_id,
            session_id="session-1",
            expected_runtime_generation=runtime_generation,
        )
        refill.assert_called_once_with(template, manager)
    else:
        claim.assert_not_awaited()
        refill.assert_not_called()


@pytest.mark.asyncio
async def test_claim_refuses_a_manifest_with_an_unknown_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    manifest = {
        "slot_id": "slot-1",
        "engine_kind": "codex",
        "placement": "",
        "sandbox_id": "box-1",
        "runtime_identity": {},
        "runtime_env": {},
        "environment_credential_contract": [],
        "gateway_substitution": False,
    }
    monkeypatch.setattr(
        prepared_slots,
        "claim_prepared_slot",
        AsyncMock(return_value=manifest),
    )
    monkeypatch.setattr(
        provisioning,
        "resolve_session_environment_credentials",
        AsyncMock(return_value=(None, {})),
    )
    backend = SimpleNamespace(name="fake", connect=AsyncMock())

    with pytest.raises(APIError, match="unknown placement") as caught:
        await provisioning.claim_prepared_engine_sandbox(
            object(),
            session_id="session-1",
            assignment_id="assignment-1",
            template=SimpleNamespace(
                agent_id="agent-1",
                engine_kind="codex",
                prewarm_enabled=True,
                runtime_generation="generation-1",
            ),
            workspace_plan=SimpleNamespace(resume_engine_session_key=""),
            user_id="user-1",
            request=SimpleNamespace(credential=object()),
            backend_adapter=backend,
            credential="model-placeholder",
            mcp_vault_write=None,
            vault_enabled=True,
            session_log=None,
        )

    assert caught.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    backend.connect.assert_not_awaited()


# ── the credential decision under slot identity ───────────────────────────


def test_slot_preparation_refuses_an_unprotected_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_off(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        provisioning.resolve_model_credential_delivery(
            template=_template(),
            backend_adapter=_Backend(),
            credential=_credential(),
            slot_id="slot-1",
        )
    # The real key inside an unclaimed box would be readable by whichever
    # Session claims it later, under whatever identity — refused outright.
    assert excinfo.value.code == "AGENT_PREWARM_UNSUPPORTED"


def test_slot_preparation_refuses_a_non_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_on(monkeypatch)
    with pytest.raises(APIError) as excinfo:
        provisioning.resolve_model_credential_delivery(
            template=_template(),
            backend_adapter=_Backend(),
            credential=_credential(header="x-api-key"),
            slot_id="slot-1",
        )
    assert excinfo.value.code == "AGENT_PREWARM_UNSUPPORTED"


def test_slot_delivery_composes_the_per_slot_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_on(monkeypatch)
    credential, _policy, vault_write = provisioning.resolve_model_credential_delivery(
        template=_template(),
        backend_adapter=_Backend(),
        credential=_credential(),
        slot_id="slot-1",
    )
    assert credential == workload_model_placeholder("slot-1")
    assert vault_write is not None
    model = vault_write.model[0]
    # The slot credential starts as the SHARED key: a first model call racing
    # the claim's replace authenticates under the box identity, never fails.
    substitutions = {
        (item.placeholder, item.name, item.secret_value)
        for item in model.substitutions
    }
    assert (
        workload_model_placeholder("slot-1"),
        workload_credential_name("slot-1"),
        "sk-shared-key",
    ) in substitutions
    # The engine's declared wire, not a v1/* assumption: codex admits only the
    # Responses path, and a binding scoped to another path 401s every turn.
    assert model.request_paths == ("/responses",)


def test_the_cold_delivery_shape_is_unchanged_without_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_on(monkeypatch)
    credential, _policy, vault_write = provisioning.resolve_model_credential_delivery(
        template=_template(),
        backend_adapter=_Backend(),
        credential=_credential(),
    )
    assert credential == provisioning.EGRESS_HELD_PLACEHOLDER
    assert vault_write is not None
    # No per-slot credential rides a cold create; the box-wide entry stands alone.
    assert len(vault_write.model) == 1
    assert vault_write.model[0].substitutions == ()


# ── the slot create itself ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_slot_create_carries_slot_identity_and_defers_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_on(monkeypatch)
    backend = _Backend()
    agent_mcp = AsyncMock(return_value=None)
    import astrabox.seams.sandbox as sandbox_seam

    monkeypatch.setattr(sandbox_seam, "sandbox_for_template", lambda template: backend)
    monkeypatch.setattr(
        provisioning,
        "resolve_agent_mcp_credential_plan",
        agent_mcp,
    )
    manager = _Manager()
    template = _template()
    template.sandbox_permission_level = "advanced"
    provisioned = await provisioning.provision_engine_slot_sandbox(
        manager,
        slot_id="slot-9",
        template=template,
        runtime_identity={"workspace_dir": "/workspace"},
        request=provisioning.EngineSandboxRequest(
            entrypoint=("/opt/gem/run.sh",),
            credential=_credential(),
            credential_env_var="OPENAI_API_KEY",
            cwd_env_var="ASTRABOX_WORKSPACE",
            wait_for_inbox_service_port=44790,
        ),
    )
    spec = backend.spec
    # The create's reverse-lookup metadata names the SLOT, never a Session id
    # that does not exist; the claim re-points it.
    assert spec.session_id == "slot-9"
    assert spec.wait_for_inbox_service_port == 44790
    assert spec.resource_limits == provisioning.sandbox_create_resources()[0]
    assert spec.resource_requests == {"cpu": "200m", "memory": "768Mi"}
    assert spec.permission_level == "advanced"
    env = dict(spec.env)
    assert env["OPENAI_API_KEY"] == workload_model_placeholder("slot-9")
    assert env["ASTRABOX_WORKSPACE"] == "/workspace"
    # Codex declares a session log, so the box gets the deferred target file
    # rather than per-session mirror values it could not know yet.
    assert (
        env[transcript_mirror.TRANSCRIPT_MIRROR_TARGET_FILE_ENV]
        == transcript_mirror.DEFERRED_MIRROR_TARGET_FILE
    )
    assert transcript_mirror.TRANSCRIPT_BASE_URL_ENV not in env
    assert transcript_mirror.PLATFORM_SESSION_ID_ENV not in env
    # No Session row exists to own the box: the claim records the allocation.
    assert manager.tracked == []
    assert provisioned.sandbox_id == "box-slot-1"
    assert provisioned.cwd == "/workspace"
    assert provisioned.gateway_substitution is True
    agent_mcp.assert_awaited_once_with(template=template, vault_enabled=True)


@pytest.mark.asyncio
async def test_the_slot_create_requires_a_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_on(monkeypatch)
    import astrabox.seams.sandbox as sandbox_seam

    monkeypatch.setattr(
        sandbox_seam, "sandbox_for_template", lambda template: _Backend()
    )
    with pytest.raises(RuntimeError, match="working directory"):
        await provisioning.provision_engine_slot_sandbox(
            _Manager(),
            slot_id="slot-9",
            template=_template(),
            runtime_identity={},
            request=provisioning.EngineSandboxRequest(
                entrypoint=("/x",), credential=_credential()
            ),
        )


# ── the readiness gate, and the window it sits in ─────────────────────────
#
# A slot records NO startup allocation: there is no Session row to own the box
# until a claim records one. So between the create and the return the box is
# named by nothing, and the caller's `try` — which begins after the return —
# cannot reach it. Anything this function does in that window owns its own
# cleanup, or it leaks a box per refusal with no record of what leaked.


class _RefusingMedium:
    """A workspace medium that reports the mount did not arrive."""

    name = "slot_refusing_medium"

    async def prepare(self, ref: Any, *, box: Any, box_path: str) -> None:
        raise RuntimeError("volume is not mounted in this sandbox")


class _PresentMedium:
    """A workspace medium that confirms the mount, so the slot is kept."""

    name = "slot_present_medium"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def prepare(self, ref: Any, *, box: Any, box_path: str) -> None:
        self.asked.append(box_path)


def _deployment_with_a_workspace_volume(
    monkeypatch: pytest.MonkeyPatch, medium: Any
) -> tuple[list[tuple[str, str | None]], AsyncMock]:
    """A deployment that mounts a volume, and a record of what gets destroyed."""

    import astrabox.core.service.orchestrator.agent.prepared_boxes as prepared_boxes
    from astrabox.core.service.orchestrator.runtime.storage.mergerfs import workspace_router
    import astrabox.seams.storage as storage_seam

    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", "astrabox-workspaces")
    monkeypatch.setenv("ASTRABOX_NAS_BASE_PATH", "/nas")
    monkeypatch.setattr(storage_seam, "_PROVIDERS", dict(storage_seam._PROVIDERS))
    storage_seam.register_storage(medium.name, medium)
    monkeypatch.setattr(storage_seam, "_CONFIGURED", medium.name)

    async def backing(_assignment: str, mounts: Any) -> storage_seam.StorageMountPlan:
        return storage_seam.StorageMountPlan("backing-volume", mounts)

    async def routed(_assignment: str, plan: Any) -> storage_seam.StorageMountPlan:
        return storage_seam.StorageMountPlan("routed-volume", plan.mounts)

    monkeypatch.setattr(medium, "provision_mounts", backing, raising=False)
    monkeypatch.setattr(workspace_router, "provision_mounts", routed)
    monkeypatch.setattr(workspace_router, "attach_sandbox", AsyncMock())
    route_ready = AsyncMock()
    monkeypatch.setattr(workspace_router, "prepare", route_ready)

    destroyed: list[tuple[str, str | None]] = []

    async def _record_destroy(
        sandbox_id: str, *, sandbox_backend: str, slot_id: str | None
    ) -> None:
        assert sandbox_backend == "fake"
        destroyed.append((sandbox_id, slot_id))

    monkeypatch.setattr(prepared_boxes, "destroy_slot_box", _record_destroy)
    return destroyed, route_ready


async def _prepare_one_slot(monkeypatch: pytest.MonkeyPatch) -> Any:
    _vault_on(monkeypatch)
    import astrabox.seams.sandbox as sandbox_seam

    monkeypatch.setattr(
        sandbox_seam, "sandbox_for_template", lambda template: _Backend()
    )
    return await provisioning.provision_engine_slot_sandbox(
        _Manager(),
        slot_id="slot-9",
        template=AgentView(
            engine_kind="codex",
            model_config={},
            networking=None,
            runtime_template_name="astrabox/sandbox-codex:latest",
            sandbox_backend="open_sandbox",
            agent_id="agent-7",
        ),
        runtime_identity={
            "linux_user": "agent",
            "home_dir": "/home/agent",
            "sandbox_tenancy": "conversation",
            "workspace_dir": "/workspace",
            "workspace_source_dir": "/workspace",
        },
        request=provisioning.EngineSandboxRequest(
            entrypoint=("/opt/gem/run.sh",),
            credential=_credential(),
            credential_env_var="OPENAI_API_KEY",
            cwd_env_var="ASTRABOX_WORKSPACE",
        ),
    )


@pytest.mark.asyncio
async def test_a_slot_whose_mount_arrived_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: this input reaches the pool when the medium confirms.

    Without it the refusal test below proves only that the box is destroyed,
    not that it is destroyed BECAUSE the mount was refused.
    """

    medium = _PresentMedium()
    destroyed, route_ready = _deployment_with_a_workspace_volume(monkeypatch, medium)

    provisioned = await _prepare_one_slot(monkeypatch)

    assert provisioned.sandbox_id == "box-slot-1"
    assert destroyed == []
    assert medium.asked == ["/workspace"]
    route_ready.assert_awaited_once()
    assert route_ready.await_args.kwargs == {"box": provisioned.sandbox, "box_path": "/workspace"}


@pytest.mark.asyncio
async def test_a_slot_whose_mount_was_refused_destroys_its_own_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked slot box costs capacity nothing can name back.

    Lending it instead would be worse: a prepared box is handed over as ready,
    so every conversation that borrows it writes to the container's own disk.
    """

    destroyed, route_ready = _deployment_with_a_workspace_volume(monkeypatch, _RefusingMedium())

    with pytest.raises(RuntimeError, match="volume is not mounted in this sandbox"):
        await _prepare_one_slot(monkeypatch)

    assert destroyed == [("box-slot-1", "slot-9")]
    route_ready.assert_not_awaited()


def _environment_credential(
    *,
    secret_value: str,
    hosts: tuple[str, ...] = ("api.example.test",),
) -> EgressCredential:
    slot_context = workload_placeholder_context("slot-env")
    return EgressCredential(
        credential_id="credential-env",
        secret_name="SERVICE_TOKEN",
        secret_value=secret_value,
        placeholder=mint_placeholder("credential-env", context=slot_context),
        networking={"type": "limited", "allowed_hosts": list(hosts)},
        injection_location={"header": True, "body": False},
        allowed_requests={"methods": ["POST"], "paths": ["/v1/*"]},
    )


def test_prepared_environment_contract_pins_policy_without_persisting_secret() -> None:
    original = SandboxEgressCredentialPlan(
        environment=(_environment_credential(secret_value="old-secret"),)
    )
    rotated = SandboxEgressCredentialPlan(
        environment=(_environment_credential(secret_value="new-secret"),)
    )
    widened = SandboxEgressCredentialPlan(
        environment=(
            _environment_credential(
                secret_value="new-secret",
                hosts=("api.example.test", "other.example.test"),
            ),
        )
    )

    contract = provisioning.environment_credential_contract(original)

    assert contract == provisioning.environment_credential_contract(rotated)
    assert contract != provisioning.environment_credential_contract(widened)
    assert "old-secret" not in str(contract)


@pytest.mark.asyncio
async def test_session_credentials_can_reproduce_a_prepared_workload_context() -> None:
    context = workload_placeholder_context("slot-env")
    expected = _environment_credential(secret_value="rotated-secret")

    class _CredentialManager:
        async def resolve_session_egress_credentials(
            self,
            session_id: str,
            *,
            placeholder_context: str | None = None,
        ) -> list[EgressCredential]:
            assert session_id == "session-1"
            assert placeholder_context == context
            return [expected]

    plan, runtime_env = await provisioning.resolve_session_environment_credentials(
        _CredentialManager(),
        session_id="session-1",
        vault_enabled=True,
        vault_write=None,
        placeholder_context=context,
    )

    assert plan is not None
    assert plan.environment == (expected,)
    assert runtime_env == {"SERVICE_TOKEN": expected.placeholder}


@pytest.mark.asyncio
async def test_claim_refuses_environment_policy_drift_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    prepared_plan = SandboxEgressCredentialPlan(
        environment=(_environment_credential(secret_value="prepared-secret"),)
    )
    changed = _environment_credential(
        secret_value="current-secret",
        hosts=("other.example.test",),
    )
    manifest = {
        "slot_id": "slot-env",
        "engine_kind": "codex",
        "runtime_env": {"SERVICE_TOKEN": changed.placeholder},
        "environment_credential_contract": (
            provisioning.environment_credential_contract(prepared_plan)
        ),
    }
    monkeypatch.setattr(
        prepared_slots,
        "claim_prepared_slot",
        AsyncMock(return_value=manifest),
    )

    class _CredentialManager:
        async def resolve_session_egress_credentials(
            self,
            _session_id: str,
            *,
            placeholder_context: str | None = None,
        ) -> list[EgressCredential]:
            assert placeholder_context == workload_placeholder_context("slot-env")
            return [changed]

    backend = SimpleNamespace(connect=AsyncMock())
    template = SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        prewarm_enabled=True,
        runtime_generation="generation-1",
    )

    with pytest.raises(APIError) as caught:
        await provisioning.claim_prepared_engine_sandbox(
            _CredentialManager(),
            session_id="session-1",
            assignment_id="assignment-1",
            template=template,
            workspace_plan=SimpleNamespace(resume_engine_session_key=""),
            user_id="user-1",
            request=SimpleNamespace(credential=object()),
            backend_adapter=backend,
            credential="model-placeholder",
            mcp_vault_write=None,
            vault_enabled=True,
            session_log=None,
        )

    assert caught.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    backend.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_rotates_a_secret_behind_the_prepared_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    current = _environment_credential(secret_value="rotated-secret")
    environment_plan = SandboxEgressCredentialPlan(environment=(current,))
    manifest = {
        "slot_id": "slot-env",
        "engine_kind": "codex",
        "placement": "shared_slot",
        "sandbox_id": "box-1",
        "sandbox_backend": "fake",
        "runtime_env": {"SERVICE_TOKEN": current.placeholder},
        "isolated_session_id": "engine-isolation-1",
        "terminal_isolated_session_id": "terminal-isolation-1",
        "environment_credential_contract": (
            provisioning.environment_credential_contract(environment_plan)
        ),
        "runtime_identity": {
            "linux_user": "slot_env",
            "home_dir": "/home/conversations/slot_env",
            "sandbox_tenancy": "agent",
            "workspace_dir": "/workspace",
            "workspace_source_dir": "/home/conversations/slot_env/workspace",
        },
        "gateway_substitution": False,
        "runner_port": 0,
    }
    monkeypatch.setattr(
        prepared_slots,
        "claim_prepared_slot",
        AsyncMock(return_value=manifest),
    )
    repoint = AsyncMock()
    monkeypatch.setattr(prepared_slots, "repoint_slot_gateway_credential", repoint)
    monkeypatch.setattr(provisioning, "plan_workspace_mounts", AsyncMock(return_value=()))
    monkeypatch.setattr(provisioning, "workspace_is_ready", AsyncMock())
    monkeypatch.setattr(
        provisioning,
        "workspace_ref_for_subject",
        lambda **values: SimpleNamespace(**values),
    )
    assembled = object()
    assemble = AsyncMock(return_value=assembled)
    monkeypatch.setattr(provisioning, "_assemble_provisioned_sandbox", assemble)

    class _CredentialManager:
        def __init__(self) -> None:
            self.allocations: list[tuple[str, Any]] = []

        async def resolve_session_egress_credentials(
            self,
            _session_id: str,
            *,
            placeholder_context: str | None = None,
        ) -> list[EgressCredential]:
            assert placeholder_context == workload_placeholder_context("slot-env")
            return [current]

        async def record_startup_allocation(
            self, session_id: str, allocation: Any
        ) -> None:
            self.allocations.append((session_id, allocation))

    class _ClaimBackend:
        name = "fake"

        def __init__(self) -> None:
            self.sandbox = SimpleNamespace()
            self.applied: SandboxEgressCredentialPlan | None = None

        async def connect(self, sandbox_id: str) -> Any:
            assert sandbox_id == "box-1"
            return self.sandbox

        async def apply_credential_vault(
            self,
            sandbox: Any,
            *,
            vault_write: SandboxEgressCredentialPlan,
            create_if_missing: bool,
        ) -> None:
            assert sandbox is self.sandbox
            assert create_if_missing is False
            self.applied = vault_write

    manager = _CredentialManager()
    backend = _ClaimBackend()
    template = SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        prewarm_enabled=True,
        runtime_generation="generation-1",
        sandbox_permission_level="default",
    )

    claimed = await provisioning.claim_prepared_engine_sandbox(
        manager,
        session_id="session-1",
        assignment_id="assignment-1",
        template=template,
        workspace_plan=SimpleNamespace(
            resume_engine_session_key="",
            cwd="/workspace",
            assistant_id=None,
            user_id="user-1",
        ),
        user_id="user-1",
        request=SimpleNamespace(credential=object()),
        backend_adapter=backend,
        credential="model-placeholder",
        mcp_vault_write=None,
        vault_enabled=True,
        session_log=None,
    )

    assert claimed is assembled
    assert backend.applied is not None
    assert backend.applied.environment[0].secret_value == "rotated-secret"
    assert backend.applied.environment[0].placeholder == current.placeholder
    assert assemble.await_args.kwargs["runtime_env"] == {
        "SERVICE_TOKEN": current.placeholder
    }
    assert manager.allocations[0][0] == "session-1"
    assert manager.allocations[0][1].scope == "isolated_sessions"
    assert manager.allocations[0][1].isolated_session_ids == (
        "engine-isolation-1", "terminal-isolation-1",
    )
    repoint.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_pre_activation_handoff_discards_the_exact_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_boxes, prepared_slots
    from astrabox.persistence.repository import agent_repository

    manifest = {
        "state": "claimed",
        "claimed_session_id": "session-1",
        "placement": "shared_slot",
        "slot_id": "slot-1",
        "sandbox_id": "box-1",
    }
    repo = SimpleNamespace(get_agent=AsyncMock(return_value={"_prepared_slot": manifest}))
    discard_slot = AsyncMock()
    release = AsyncMock()
    refill = Mock()
    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: repo)
    monkeypatch.setattr(prepared_slots, "discard_prepared_slot", discard_slot)
    monkeypatch.setattr(prepared_slots, "release_claimed_slot_allocation", release)
    monkeypatch.setattr(prepared_slots, "schedule_prepared_runtime_refill", refill)
    monkeypatch.setattr(prepared_boxes, "discard_prepared_box", AsyncMock())
    manager = object()
    template = SimpleNamespace(agent_id="agent-1")

    await provisioning._discard_failed_prepared_claim(
        manager,
        session_id="session-1",
        template=template,
        reason="credential contract changed",
    )

    discard_slot.assert_awaited_once_with(
        "agent-1",
        manifest,
        reason="credential contract changed",
    )
    release.assert_awaited_once_with("session-1", manifest)
    refill.assert_called_once_with(template, manager)
