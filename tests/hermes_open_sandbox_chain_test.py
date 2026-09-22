"""Assistant startup through the OpenSandbox adapter with a simulated HTTP wire.

``start_platform_runtime`` owns placement, workspace, credentials and cleanup
tracking, then hands the resulting box to Hermes for vendor activation. This
binds that whole product path to the real ``OpenSandboxSandboxProvider``. It
catches both halves drifting: a backend without box creation, and an engine
adapter that tries to take platform workflow back.

The provider uses ``httpx.MockTransport``; no sandbox or Hermes process runs.
Storage provisioning, in-box readiness, identity bootstrap, profile preparation
and the engine client are test doubles. These checks cannot prove a real
supplier call, storage mount or credential substitution succeeds.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import hermes, provisioning, startup
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    assistant_workspace_dir,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineOutputCheckpoint,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    RuntimeWorkspacePlan,
)
from astrabox.core.service.orchestrator.workspace.base import WorkspaceRef
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import EXECD_PORT
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.storage import StorageMountPlan
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    MCPOutboundCredential,
    MCPOutboundCredentialResolution,
)

_BASE_URL = "http://opensandbox.test:8080"
_EXECD_HOST = "box.execd.test"
_MODEL_BASE_URL = "https://models.astrabox.test/v1"
_MODEL_API_KEY = "hermes-real-model-key"


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _BASE_URL)
    monkeypatch.setenv("OPENSANDBOX_DISABLE_METRICS", "1")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "hermes-chain-test-signing-key-32-bytes")


class _FakeLifecycleWire:
    """The lifecycle routes one create + teardown touches."""

    def __init__(self) -> None:
        self.create_bodies: list[dict[str, Any]] = []
        self.created_directories: list[dict[str, Any]] = []
        self.vault_bodies: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.live: set[str] = set()
        self._created = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == _EXECD_HOST:
            if path == "/ping":
                return httpx.Response(200, json={"status": "ok"})
            if path == "/command" and request.method == "POST":
                return httpx.Response(
                    200,
                    content=(
                        'data: {"type":"execution_complete","timestamp":0,'
                        '"execution_time":1}\n\n'
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            if path == "/directories":
                self.created_directories.append(json.loads(request.content))
                return httpx.Response(200, json={})
            if path == "/credential-vault" and request.method == "POST":
                self.vault_bodies.append(json.loads(request.content))
                return httpx.Response(
                    201,
                    json={"revision": 1, "credentials": [], "bindings": []},
                )
            return httpx.Response(404, json={"code": "NOT_FOUND", "message": "x"})
        if path == "/v1/sandboxes" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "pagination": {
                        "page": 1,
                        "pageSize": 2,
                        "totalItems": 0,
                        "totalPages": 1,
                        "hasNextPage": False,
                    },
                },
            )
        if path == "/v1/sandboxes" and request.method == "POST":
            self.create_bodies.append(json.loads(request.content))
            self._created += 1
            sandbox_id = f"sb-hermes-{self._created}"
            self.live.add(sandbox_id)
            return httpx.Response(202, json=self._sandbox_json(sandbox_id))
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "sandboxes":
            sandbox_id = parts[2]
            if sandbox_id not in self.live:
                return httpx.Response(404, json={"code": "NOT_FOUND", "message": "x"})
            if len(parts) == 3 and request.method == "GET":
                return httpx.Response(200, json=self._sandbox_json(sandbox_id))
            if len(parts) == 3 and request.method == "DELETE":
                self.killed.append(sandbox_id)
                self.live.discard(sandbox_id)
                return httpx.Response(204)
            if len(parts) == 5 and parts[3] == "endpoints":
                return httpx.Response(
                    200, json={"endpoint": f"{_EXECD_HOST}:{parts[4]}"}
                )
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "x"})

    @staticmethod
    def _sandbox_json(sandbox_id: str) -> dict[str, Any]:
        return {
            "id": sandbox_id,
            "status": {"state": "Running"},
            "entrypoint": ["/opt/astrabox/boot.sh"],
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }


class _Workspace:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ref = WorkspaceRef(
            kind="assistant", user_id="principal-1", assistant_id="assistant-1"
        )
        self.provisioned_runtime_identity: dict[str, Any] | None = None

    def plan_runtime_identity(
        self,
        *,
        template: Any,
        session_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        _ = (template, session_id)
        return hermes._build_hermes_workspace_identity(
            user_id=str(user_id or "principal-1"),
            assistant_id="assistant-1",
            sandbox_id=None,
        )

    def capability_scope(self) -> str:
        return "agent_runtime"

    async def mount_and_provision(
        self, _manager: Any, sandbox: Any, **kwargs: Any
    ) -> None:
        self.events.append("identity_bootstrap")
        identity = dict(kwargs["runtime_identity"])
        identity["sandbox_id"] = sandbox.sandbox_id
        self.provisioned_runtime_identity = identity


class _Manager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.deployment_settings = SimpleNamespace(mcp_proxy_base_url="https://astrabox.test")
        self.tracked: list[Any] = []
        self.mcp_credentials: list[MCPOutboundCredential] = []

    async def resolve_runtime_sandbox_backend(
        self, _session_id: str, *, workspace_plan: Any
    ) -> str:
        return "open_sandbox"

    def resolve_sandbox_backend_secret(
        self, _template: Any, *, backend: str | None = None
    ) -> str:
        return "sandbox-secret-long-enough"

    def resolve_model_access(self, _config: dict[str, Any]) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration={},
            base_url=_MODEL_BASE_URL,
            model_name="hermes-model",
            credential=_MODEL_API_KEY,
            credential_kind="bearer",
            endpoint_provider="litellm",
        )

    async def record_startup_allocation(
        self,
        _session_id: str,
        allocation: Any,
    ) -> None:
        self.events.append("record_allocation")
        self.tracked.append(allocation)

    async def clone_default_repo(self, *_args: Any, **_kwargs: Any) -> None:
        self.events.append("clone_default_repo")

    async def resolve_session_mcp_credentials(
        self, _session_id: str, _server_urls: list[str]
    ) -> MCPOutboundCredentialResolution:
        return MCPOutboundCredentialResolution(
            scope_id="hermes-test-scope",
            credentials=tuple(self.mcp_credentials),
        )

    async def resolve_session_egress_credentials(
        self, _session_id: str, **_kwargs: Any
    ) -> list[Any]:
        return []


class _FakeEngineClient:
    """Smallest real EngineClient construction contract used by these tests."""

    def __init__(self) -> None:
        self._engine_session_key: str | None = None

    @property
    def is_live(self) -> bool:
        return True

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    async def bind_conversation(
        self, binding: EngineConversationBinding
    ) -> None:
        self._engine_session_key = binding.engine_session_key

    async def deliver(self, command: EngineInputCommand) -> None:
        del command

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        return EngineTurnReceipt(
            command.command_id,
            "session-1",
            1,
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )

    async def _events(self) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    def iter_turn_events(
        self, receipt: EngineTurnReceipt
    ) -> AsyncIterator[dict[str, Any]]:
        del receipt
        return self._events()

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        del receipt
        return True

    async def interrupt_active_turn(self) -> bool:
        return True

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(engine_kind="assistant")

    def iter_reconnected_turn_events(
        self,
        *,
        engine_turn_id: str,
        output_checkpoint: EngineOutputCheckpoint,
    ) -> AsyncIterator[dict[str, Any]]:
        del engine_turn_id, output_checkpoint
        return self._events()

    async def close(self) -> None:
        return None


def _plan(
    *, resume_engine_session_key: str | None = None
) -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="assistant_runtime",
        session_kind="assistant_chat",
        operation="runtime_start",  # type: ignore[arg-type]
        runtime_key="assistant-runtime",
        conversation_session_id=None,
        cwd="/workspace",
        resume_engine_session_key=resume_engine_session_key,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="assistant",
        user_id="principal-1",
        assistant_id="assistant-1",
    )


def _template(
    *,
    mcp_servers: dict[str, Any] | None = None,
    networking: dict[str, Any] | None = None,
) -> AgentView:
    return AgentView(
        model_config={},
        networking=networking,
        sandbox_backend="open_sandbox",
        name="astrabox/sandbox-hermes:latest",
        runtime_template_name="astrabox/sandbox-hermes:latest",
        mcp_servers=mcp_servers,
        engine_kind="assistant",
        agent_id=None,
    )


async def _start(
    manager: _Manager,
    adapter: hermes.HermesEngineAdapter,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    callback_url: str | None = None,
    resume_engine_session_key: str | None = None,
) -> Any:
    return await startup.start_platform_runtime(
        manager,
        adapter,
        session_id=session_id,
        assignment_id=assignment_id,
        template=template,
        workspace_plan=_plan(
            resume_engine_session_key=resume_engine_session_key
        ),
        user_id="principal-1",
        permission_mode=None,
        progress_callback=None,
        callback_url=callback_url,
    )


@pytest.mark.asyncio
async def test_hermes_engine_start_rejects_undeclared_startup_inputs() -> None:
    adapter = hermes.HermesEngineAdapter()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await adapter._start_engine(
            object(),
            session_id="session-1",
            identity={},
            obsolete_startup_option=True,
        )


def _bind_real_backend(
    monkeypatch: pytest.MonkeyPatch,
    wire: _FakeLifecycleWire,
    events: list[str],
    gateway_calls: list[dict[str, Any]] | None = None,
) -> _Workspace:
    """Stub everything AROUND provisioning; leave the backend real."""
    workspace = _Workspace(events)
    provider = OpenSandboxSandboxProvider(transport=wire.transport())
    # Patched on the seam itself: the backend is resolved by the platform's
    # provisioning contract now, not inside the adapter, and the seam registry
    # is the one place both read.
    import astrabox.seams.sandbox as sandbox_seam
    from astrabox.core.service.orchestrator.runtime import mcp_credentials
    from astrabox.core.service.orchestrator.runtime.storage import mounts as storage_mounts
    from astrabox.core.service.orchestrator import workspace as workspace_module

    def extension_provider(name: str) -> Any:
        assert name == "builtin"
        return SimpleNamespace(mcp_gateway_credential=lambda: None)

    monkeypatch.setattr(sandbox_seam, "sandbox_for_name", lambda _name: provider)
    monkeypatch.setattr(
        mcp_credentials, "extension_provider_for_name", extension_provider
    )
    monkeypatch.setattr(
        workspace_module, "workspace_from_subject_kind", lambda *_a, **_k: workspace
    )

    async def planned_mounts(**_kwargs: Any) -> tuple[tuple[str, str], ...]:
        return ((assistant_workspace_dir("principal-1", "assistant-1"), "/workspaces/a1"),)

    async def mounted(*_args: Any, **_kwargs: Any) -> None:
        events.append("workspace_mount_ready")

    async def no_platform_mcp(**_kwargs: Any) -> None:
        return None

    async def backing_mounts(_assignment: str, mounts: Any) -> StorageMountPlan:
        return StorageMountPlan("backing-volume", mounts)

    async def routed_mounts(_assignment: str, plan: StorageMountPlan) -> StorageMountPlan:
        return StorageMountPlan("astrabox-workspaces", plan.mounts)

    monkeypatch.setattr(
        storage_mounts, "storage_provider",
        lambda: SimpleNamespace(provision_mounts=backing_mounts),
    )
    monkeypatch.setattr(storage_mounts.workspace_router, "provision_mounts", routed_mounts)
    monkeypatch.setattr(storage_mounts.workspace_router, "attach_sandbox", AsyncMock())
    monkeypatch.setattr(provisioning, "plan_workspace_mounts", planned_mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", mounted)
    monkeypatch.setattr(startup, "_publish_platform_mcp_binding", no_platform_mcp)

    async def inbox(_sandbox: Any) -> None:
        events.append("inbox_ready")

    async def prepare_profile(*_args: Any, **kwargs: Any) -> None:
        events.append("profile_ready")
        if gateway_calls is not None:
            gateway_calls.append(dict(kwargs))

    monkeypatch.setattr(hermes, "_await_hermes_inbox_ready", inbox)
    monkeypatch.setattr(hermes, "_prepare_hermes_profile", prepare_profile)
    return workspace


async def test_assistant_default_protection_keeps_the_model_key_outside_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermes must use the same default-on protection promised for Agent turns."""
    monkeypatch.setenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", "1")
    events: list[str] = []
    gateway_calls: list[dict[str, Any]] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    _bind_real_backend(monkeypatch, wire, events, gateway_calls)
    adapter = hermes.HermesEngineAdapter()

    async def start_engine(*_args: Any, **_kwargs: Any) -> Any:
        return _FakeEngineClient()

    monkeypatch.setattr(adapter, "_start_engine", start_engine)
    runtime = await _start(
        manager,
        adapter,
        session_id="session-vault-on",
        assignment_id="assignment-vault-on",
        template=_template(),
    )

    (create_body,) = wire.create_bodies
    assert create_body["networkPolicy"]["defaultAction"] == "deny"
    assert create_body["credentialProxy"] == {"enabled": True}
    assert _MODEL_API_KEY not in json.dumps(create_body)
    (vault_body,) = wire.vault_bodies
    assert vault_body["credentials"][0]["source"]["value"] == _MODEL_API_KEY
    assert gateway_calls[0]["model_api_key"] == EGRESS_HELD_PLACEHOLDER
    assert all(
        _MODEL_API_KEY not in value
        for value in gateway_calls[0].values()
        if isinstance(value, str)
    )
    await runtime.sandbox.close()


async def test_assistant_direct_mcp_credential_joins_the_create_vault_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", "1")
    events: list[str] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    manager.mcp_credentials = [
        MCPOutboundCredential(
            credential_id="mcp-cred-1",
            target_url="https://tools.example.test/mcp",
            headers={"apikey": "mcp-secret"},
        )
    ]
    _bind_real_backend(monkeypatch, wire, events)
    adapter = hermes.HermesEngineAdapter()

    async def start_engine(*_args: Any, **_kwargs: Any) -> Any:
        return _FakeEngineClient()

    monkeypatch.setattr(adapter, "_start_engine", start_engine)
    runtime = await _start(
        manager,
        adapter,
        session_id="session-mcp-vault",
        assignment_id="assignment-mcp-vault",
        template=_template(
            mcp_servers={
                "tools": {
                    "provider": "builtin",
                    "type": "http",
                    "url": "https://tools.example.test/mcp",
                    "credential_target_url": "https://tools.example.test/mcp",
                }
            },
            networking={"type": "limited", "allow_mcp_servers": True},
        ),
    )

    allowed = {
        rule["target"] for rule in wire.create_bodies[0]["networkPolicy"]["egress"]
    }
    assert "tools.example.test" in allowed
    vault_body = wire.vault_bodies[0]
    assert {item["source"]["value"] for item in vault_body["credentials"]} == {
        _MODEL_API_KEY,
        "mcp-secret",
    }
    assert "mcp-secret" not in json.dumps(wire.create_bodies[0])
    assert runtime.prepare_engine_input is not None
    await runtime.sandbox.close()


async def test_assistant_explicit_opt_out_delivers_the_real_model_key_to_hermes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", "0")
    events: list[str] = []
    gateway_calls: list[dict[str, Any]] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    _bind_real_backend(monkeypatch, wire, events, gateway_calls)
    adapter = hermes.HermesEngineAdapter()

    async def start_engine(*_args: Any, **_kwargs: Any) -> Any:
        return _FakeEngineClient()

    monkeypatch.setattr(adapter, "_start_engine", start_engine)
    runtime = await _start(
        manager,
        adapter,
        session_id="session-vault-off",
        assignment_id="assignment-vault-off",
        template=_template(),
    )

    (create_body,) = wire.create_bodies
    assert create_body["networkPolicy"]["defaultAction"] == "deny"
    assert "credentialProxy" not in create_body
    assert wire.vault_bodies == []
    assert gateway_calls[0]["model_api_key"] == _MODEL_API_KEY
    await runtime.sandbox.close()


async def test_assistant_restore_provisions_a_real_box_and_keeps_resume_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    _bind_real_backend(monkeypatch, wire, events)
    adapter = hermes.HermesEngineAdapter()

    async def start_engine(*_args: Any, **_kwargs: Any) -> Any:
        events.append("engine_started")
        return _FakeEngineClient()

    monkeypatch.setattr(adapter, "_start_engine", start_engine)

    runtime = await _start(
        manager,
        adapter,
        session_id="session-1",
        assignment_id="assignment-1",
        template=_template(),
        callback_url="https://astrabox.test/api/v1/sandbox-callback/a/b/c/d",
        resume_engine_session_key="hermes-session-1",
    )

    # A real box was created on the real backend's create wire…
    (body,) = wire.create_bodies
    assert body["image"]["uri"] == "astrabox/sandbox-hermes:latest"
    assert body["entrypoint"] == ["/opt/gem/run.sh"]
    assert body["env"] == {
        "IS_SANDBOX": "1",
        "DISABLE_BROWSER": "true",
        "BROWSER_DOWNLOAD_DIR": "/tmp/astrabox-browser-downloads",
        "ASTRABOX_HERMES_AUTOSTART": "false",
    }
    assert body["metadata"]["astrabox.session-id"] == "session-1"
    assert body["metadata"]["astrabox.assignment-id"] == "assignment-1"
    (volume,) = body["volumes"]
    assert volume["pvc"]["claimName"] == "astrabox-workspaces"
    assert volume["pvc"]["deleteOnSandboxTermination"] is False
    assert volume["mountPath"] == assistant_workspace_dir(
        "principal-1", "assistant-1"
    )
    assert volume["subPath"] == "workspaces/a1"
    # The runtime profile is created later by the identity bootstrap. Creating
    # its future workspace here makes it root-owned, so useradd cannot give the
    # Hermes workload account a writable home.
    (directories,) = wire.created_directories
    assert list(directories) == ["/home/conversations"]
    # …the engine got a usable handle, tracked before anything can fail…
    assert isinstance(runtime.sandbox, OpenSandboxHandle)
    assert runtime.sandbox_id == "sb-hermes-1"
    assert runtime.engine_session_key == "hermes-session-1"
    assert [allocation.sandbox_id for allocation in manager.tracked] == [
        runtime.sandbox_id
    ]
    assert events[:4] == [
        "record_allocation",
        "workspace_mount_ready",
        "identity_bootstrap",
        "inbox_ready",
    ]
    assert events[-1] == "engine_started"
    assert runtime.terminal_cwd == "/workspace"
    assert runtime.runtime_identity["workspace_dir"] == "/workspace"
    assert runtime.runtime_identity["workspace_source_dir"] == (
        assistant_workspace_dir("principal-1", "assistant-1")
    )
    assert events.index("workspace_mount_ready") < events.index(
        "identity_bootstrap"
    ) < events.index("engine_started")
    # …and the box survives startup.
    assert wire.killed == []
    await runtime.sandbox.close()


async def test_hermes_uses_execd_instead_of_publishing_an_application_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The custom host talks to Hermes through OpenSandbox's injected execd PTY.
    # No Hermes HTTP or dashboard port belongs in the sandbox create request.
    events: list[str] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    _bind_real_backend(monkeypatch, wire, events)
    adapter = hermes.HermesEngineAdapter()
    monkeypatch.setattr(
        adapter, "_start_engine", lambda *_a, **_k: _noop_engine(events)
    )

    runtime = await _start(
        manager,
        adapter,
        session_id="session-1",
        assignment_id="assignment-1",
        template=_template(),
    )

    (body,) = wire.create_bodies
    assert "8642" not in json.dumps(body)
    assert "9119" not in json.dumps(body)
    endpoint = await runtime.sandbox.get_endpoint(EXECD_PORT)
    assert endpoint.endpoint == f"http://{_EXECD_HOST}:{EXECD_PORT}"
    await runtime.sandbox.close()


async def _noop_engine(events: list[str]) -> Any:
    events.append("engine_started")
    return _FakeEngineClient()


async def test_a_post_create_failure_keeps_the_allocation_named_for_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adapter records the allocation before the readiness gate. Its caller
    # owns rollback; the adapter neither destroys nor forgets the resource.
    events: list[str] = []
    wire = _FakeLifecycleWire()
    manager = _Manager(events)
    _bind_real_backend(monkeypatch, wire, events)

    async def inbox_fails(_sandbox: Any) -> None:
        raise RuntimeError("in-box server never bound")

    monkeypatch.setattr(hermes, "_await_hermes_inbox_ready", inbox_fails)
    adapter = hermes.HermesEngineAdapter()

    with pytest.raises(Exception) as raised:
        await _start(
            manager,
            adapter,
            session_id="session-1",
            assignment_id="assignment-1",
            template=_template(),
        )

    assert "in-box server never bound" in str(raised.value)
    assert wire.create_bodies, "the box was created before the failure"
    assert [allocation.sandbox_id for allocation in manager.tracked] == [
        "sb-hermes-1"
    ]
    assert wire.live == {"sb-hermes-1"}
