"""Prepared boxes: the credential a box built before its Session carries, and its claim.

A prepared box exists before any Session, so its model credential is a per-slot
gateway placeholder with the substitution behind it, and a claim adopts the box
only when its placement and Environment policy still hold.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    codex as _codex_engine,  # module import registers the adapter
)
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.seams.egress_credentials import (
    EgressCredential,
    SandboxEgressCredentialPlan,
    mint_placeholder,
    workload_placeholder_context,
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
