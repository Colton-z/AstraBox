"""Snapshot wake recreates protected credentials before engine activation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.core.service.orchestrator.engine import provisioning, startup
from astrabox.core.service.orchestrator.engine.base import EngineStartupMaterialRequest
from astrabox.providers.open_sandbox.sandbox import OpenSandboxHandle, OpenSandboxSandboxProvider
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    EgressCredential,
    MCPOutboundCredential,
    MCPOutboundCredentialResolution,
    mint_placeholder,
)
from astrabox.seams.model import ResolvedModelAccess


class _Vault:
    """Process-local storage with sanitized reads and revision-guarded writes."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.revision = 1 if state == "existing" else 0
        self.credentials: dict[str, Any] = (
            {"sibling": SimpleNamespace(name="sibling", source=SimpleNamespace(value="sibling-secret"))}
            if self.revision else {}
        )
        self.bindings: dict[str, Any] = {}

    async def create(self, *, credentials: list[Any], bindings: list[Any]) -> None:
        if self.state == "denied":
            raise RuntimeError("HTTP 403 credential vault access denied")
        if self.revision:
            raise RuntimeError("HTTP 409 credential vault already exists")
        self.credentials = {item.name: item for item in credentials}
        self.bindings = {item.name: item for item in bindings}
        self.revision = 1

    async def get(self) -> Any:
        if not self.revision:
            raise RuntimeError("HTTP 404 credential vault not found")
        return SimpleNamespace(
            revision=self.revision,
            credentials=[SimpleNamespace(name=name) for name in self.credentials],
            bindings=[SimpleNamespace(name=name) for name in self.bindings],
        )

    async def patch(
        self, *, expected_revision: int,
        credentials: dict[str, Any] | None, bindings: dict[str, Any] | None,
    ) -> None:
        assert expected_revision == self.revision
        for current, mutations in ((self.credentials, credentials), (self.bindings, bindings)):
            for name in (mutations or {}).get("delete", []):
                del current[name]
            for action in ("add", "replace"):
                for item in (mutations or {}).get(action, []):
                    assert (item.name in current) == (action == "replace")
                    current[item.name] = item
        self.revision += 1


@pytest.mark.parametrize("attach_mode", ["full", "lightweight", "observe"])
@pytest.mark.parametrize("vault_state", ["missing", "existing", "denied"])
@pytest.mark.parametrize("shared", [False, True], ids=["dedicated", "recreated-shared"])
async def test_attach_restores_complete_credentials_before_activation(
    monkeypatch: pytest.MonkeyPatch, attach_mode: str, vault_state: str, shared: bool,
) -> None:
    from astrabox.seams import sandbox as sandbox_seam

    settings = SimpleNamespace(sandbox_credential_vault_enabled=True)
    monkeypatch.setattr(startup, "load_astrabox_settings", lambda: settings)
    monkeypatch.setattr(provisioning, "load_astrabox_settings", lambda: settings)
    access = ResolvedModelAccess(
        configuration={}, base_url="https://gateway.test", model_name="test-model",
        credential="model-secret", credential_kind="bearer", endpoint_provider="test",
    )
    mcp_url = "https://mcp.test/mcp"
    env_placeholder = mint_placeholder("env-1")
    platform = SimpleNamespace(
        deployment_settings=SimpleNamespace(),
        resolve_model_access=Mock(return_value=access),
        resolve_runtime_sandbox_backend=AsyncMock(return_value="open_sandbox"),
        record_attached_runtime_identity=AsyncMock(),
        resolve_session_mcp_credentials=AsyncMock(return_value=MCPOutboundCredentialResolution(
            scope_id="scope-1", credentials=(MCPOutboundCredential(
                credential_id="mcp-1", target_url=mcp_url, headers={"Authorization": "mcp-secret"},
            ),),
        )),
        resolve_session_egress_credentials=AsyncMock(return_value=[EgressCredential(
            credential_id="env-1", secret_name="EXTERNAL_TOKEN", secret_value="env-secret",
            placeholder=env_placeholder,
            networking={"type": "limited", "allowed_hosts": ["api.test"]},
            injection_location={"header": True, "body": False},
        )]),
    )
    vault = _Vault(vault_state)
    sdk = SimpleNamespace(id="same-box", credential_vault=vault, close=AsyncMock())
    sandbox = OpenSandboxHandle(sdk)  # type: ignore[arg-type]
    backend = OpenSandboxSandboxProvider()
    admit = AsyncMock()
    monkeypatch.setattr(backend, "admit_runtime_egress", admit)
    monkeypatch.setattr(sandbox_seam, "sandbox_for_name", lambda name: backend)
    monkeypatch.setattr(startup, "connect_engine_sandbox", AsyncMock(return_value=sandbox))
    async def prepare_workspace(*args: Any, **kwargs: Any) -> Any:
        if shared:
            saved = platform.record_attached_runtime_identity.call_args.kwargs["runtime_identity"]
            assert saved["isolated_session_id"] == "new-agent"
            assert saved["terminal_isolated_session_id"] == "new-terminal"
        return (kwargs["runtime_identity"], "/workspace")

    workspace = AsyncMock(side_effect=prepare_workspace)
    monkeypatch.setattr(startup, "prepare_platform_workspace", workspace)
    monkeypatch.setattr(startup, "_workspace_capability_scope", lambda *_args: None)
    template = SimpleNamespace(
        engine_kind="test", model_config={}, networking={"type": "unrestricted"},
        mcp_servers={"external": {"type": "http", "url": mcp_url}},
    )
    plan = SimpleNamespace(user_id="user-1", subject_kind="deployment_conversation")
    identity: dict[str, Any] = {"workspace_dir": "/workspace"}
    if shared:
        from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import SharedSandboxLease

        identity.update({
            "sandbox_tenancy": "agent", "isolated_session_id": "old-agent",
            "terminal_isolated_session_id": "old-terminal", "uid": 2042, "gid": 2042,
            "home_dir": "/home/agent", "workspace_source_dir": "/home/agent/workspace",
        })

        async def restore_placement(_lease: Any, **kwargs: Any) -> Any:
            placement = SimpleNamespace(
                isolated_session_id="new-agent", terminal_isolated_session_id="new-terminal",
                uid=2042, gid=2042, home_dir="/home/agent", workspace_dir="/workspace",
            )
            await kwargs["on_recreated"](placement)
            return placement

        monkeypatch.setattr(SharedSandboxLease, "restore_existing", restore_placement)
        monkeypatch.setattr(SharedSandboxLease, "start_runner", AsyncMock(return_value="8000"))
        monkeypatch.setattr(startup, "write_engine_env_file", AsyncMock())
        monkeypatch.setattr(startup, "resolve_sandbox_websocket_endpoint", AsyncMock(return_value="ws://runner"))

    async def activate(context: Any) -> Any:
        admit.assert_awaited_once()
        values = {entry.source.value for entry in vault.credentials.values()}
        assert {"model-secret", "mcp-secret", "env-secret"} <= values
        if vault_state == "existing":
            assert "sibling-secret" in values
        assert context.sandbox_id == "same-box"
        assert context.resume_session_key == "native-session"
        assert context.model_credential == EGRESS_HELD_PLACEHOLDER
        assert context.runtime_env == {"EXTERNAL_TOKEN": env_placeholder}
        if shared:
            assert context.runtime_identity["isolated_session_id"] == "new-agent"
            assert context.runtime_identity["terminal_isolated_session_id"] == "new-terminal"
            assert platform.record_attached_runtime_identity.await_count == 2
        for secret in ("model-secret", "mcp-secret", "env-secret"):
            assert secret not in repr(context.runtime_env)
            assert secret != context.model_credential
        return "attached"

    adapter = SimpleNamespace(
        engine_kind="test", supports_unowned_output_attach=lambda: True,
        shared_conversation_service_launch=lambda **kwargs: "runner",
        startup_material_request=lambda **kwargs: EngineStartupMaterialRequest(),
        sandbox_request=lambda **kwargs: provisioning.EngineSandboxRequest(
            entrypoint=("runner",), plan_identity=False, cwd="/workspace",
            credential=provisioning.ModelCredentialRequest(
                access=access, request_paths=("/v1/*",), missing_code="NO_MODEL", missing_message="no model",
            ),
        ),
        activate_runtime=AsyncMock(side_effect=activate),
    )

    async def attach() -> Any:
        return await startup.attach_platform_runtime(
            platform, adapter, session_id="session-1", sandbox_id="same-box", template=template,
            workspace_plan=plan, user_id="user-1", engine_session_key="native-session",
            permission_mode=None, runtime_identity=identity,
            attach_mode=attach_mode,
        )

    if vault_state == "denied":
        with pytest.raises(RuntimeError, match="403"):
            await attach()
        adapter.activate_runtime.assert_not_awaited()
        sdk.close.assert_awaited_once()
        workspace.assert_not_awaited()
    else:
        assert await attach() == "attached"
        adapter.activate_runtime.assert_awaited_once()
        sdk.close.assert_not_awaited()
        assert workspace.await_count == (1 if attach_mode == "full" else 0)
