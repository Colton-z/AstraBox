"""Prepared boxes: the credential a box built before its Session carries, and its claim.

A prepared box exists before any Session, so its model credential is a per-slot
gateway placeholder with the substitution behind it, and a claim adopts the box
only when its placement and Environment policy still hold.
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

    def workspace_route(self) -> Any:
        from astrabox.providers.open_sandbox.workspace_route.mergerfs import workspace_router

        return workspace_router

    def fit_resources(self, limits: Any, requests: Any) -> tuple[dict[str, str], dict[str, str]]:
        return dict(limits), dict(requests)

    async def create_sandbox(self, spec: Any) -> Any:
        self.spec = spec
        return SimpleNamespace(sandbox_id="box-slot-1", id="box-slot-1")


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


# ── only an eligible start claims a prepared seat ───────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resume_key", "prepared_capacity", "agent_id", "runtime_generation", "workspace_volume"),
    [
        ("native-session", 1, "agent-1", "generation-1", ""),
        ("native-session", 1, "agent-1", "generation-1", "workspace-pvc"),
        ("", 0, "agent-1", "generation-1", ""),
        ("", 1, "", "generation-1", ""),
        ("", 1, "agent-1", "", ""),
    ],
    ids=["resume-no-volume", "resume-shared-volume", "prewarm-disabled", "missing-agent", "missing-generation"],
)
async def test_prepared_claim_distinguishes_resumable_and_ineligible_starts(
    monkeypatch: pytest.MonkeyPatch,
    resume_key: str,
    prepared_capacity: int,
    agent_id: str,
    runtime_generation: str,
    workspace_volume: str,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", workspace_volume)
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(prepared_slots, "claim_prepared_seat", claim)
    from astrabox.core.service.orchestrator.agent import prepared_capacity as capacity_module

    monkeypatch.setattr(capacity_module, "record_prepared_capacity_miss", AsyncMock())
    manager = object()
    template = SimpleNamespace(
        agent_id=agent_id,
        engine_kind="codex",
        prepared_capacity=prepared_capacity,
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
            template, session_id="session-1", runtime_manager=manager
        )
    else:
        claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_refuses_a_manifest_with_an_unknown_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots

    manifest = {
        "slot_id": "slot-1",
        "engine_kind": "codex",
        "placement": "",
        "engine_initialized": True,
        "sandbox_id": "box-1",
        "runtime_identity": {},
        "runtime_env": {},
        "environment_credential_contract": [],
        "gateway_substitution": False,
    }
    monkeypatch.setattr(
        prepared_slots,
        "claim_prepared_seat",
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
                prepared_capacity=1,
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
        "claim_prepared_seat",
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
        prepared_capacity=1,
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
        "engine_initialized": True,
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
        "claim_prepared_seat",
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
        prepared_capacity=1,
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
@pytest.mark.parametrize(("code", "probe_status", "cleanup_fails"), [
    ("SANDBOX_GONE", "NOT_FOUND", False),
    ("SANDBOX_GONE", "NOT_FOUND", True),
    ("SANDBOX_GONE", "OK", False),
    ("SANDBOX_GONE", "PROBE_FAILED", False),
    ("SANDBOX_UNAVAILABLE", "NOT_FOUND", False),
])
async def test_prepared_handoff_only_falls_back_after_confirmed_absence_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, code: str, probe_status: str, cleanup_fails: bool,
) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots
    from astrabox.persistence.repository import agent_repository
    from astrabox.seams.sandbox import SandboxLifecycleProbeResult

    manifest = {
        "slot_id": "slot", "engine_kind": "codex", "placement": "shared_slot", "engine_initialized": True,
        "sandbox_id": "box", "sandbox_backend": "fake",
        "environment_credential_contract": [], "runtime_env": {},
        "runtime_identity": {"home_dir": "/home/slot", "linux_user": "slot"},
    }
    monkeypatch.setattr(prepared_slots, "claim_prepared_seat", AsyncMock(return_value=manifest))
    manager = SimpleNamespace(
        resolve_session_egress_credentials=AsyncMock(return_value=[]),
        record_startup_allocation=AsyncMock(),
    )
    connect_error = APIError(code=code, message="cannot connect", status_code=503)
    backend = SimpleNamespace(
        name="fake", connect=AsyncMock(side_effect=connect_error),
        probe=AsyncMock(return_value=SandboxLifecycleProbeResult(probe_status=probe_status)),
    )
    repo = SimpleNamespace(clear_resident_sandbox_binding=AsyncMock(return_value=True))
    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: repo)
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup not settled") if cleanup_fails else None)
    monkeypatch.setattr(provisioning, "_discard_failed_prepared_claim", cleanup)
    template = SimpleNamespace(
        agent_id="agent", engine_kind="codex", prepared_capacity=1, runtime_generation="generation",
    )

    async def handoff() -> Any:
        return await provisioning.claim_prepared_engine_sandbox(
            manager, session_id="session", assignment_id="assignment", template=template,
            workspace_plan=SimpleNamespace(resume_engine_session_key=""), user_id="user",
            request=SimpleNamespace(credential=object()), backend_adapter=backend,
            credential="placeholder", mcp_vault_write=None, vault_enabled=True, session_log=None,
        )

    confirmed = code == "SANDBOX_GONE" and probe_status == "NOT_FOUND"
    if confirmed and not cleanup_fails:
        assert await handoff() is None
    elif cleanup_fails:
        with pytest.raises(RuntimeError, match="cleanup not settled"):
            await handoff()
    else:
        with pytest.raises(APIError) as caught:
            await handoff()
        assert caught.value is connect_error
    if confirmed:
        repo.clear_resident_sandbox_binding.assert_awaited_once_with(
            "agent", sandbox_id="box", sandbox_backend="fake",
        )
        cleanup.assert_awaited_once_with(
            manager, session_id="session", template=template,
            reason="prepared sandbox disappeared before hand-off",
        )
    else:
        repo.clear_resident_sandbox_binding.assert_not_awaited()
        cleanup.assert_not_awaited()
    if code != "SANDBOX_GONE":
        backend.probe.assert_not_awaited()
    manager.record_startup_allocation.assert_not_awaited()
