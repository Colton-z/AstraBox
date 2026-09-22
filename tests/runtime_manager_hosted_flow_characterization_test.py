"""Platform runtime publication, rollback, cancellation, and disposal contracts.

Engine launch details are covered at each adapter and by live E2E. This module
holds only platform-observable ownership transitions: when an allocated box is
released or retained, when a runtime becomes visible, and what eviction versus
termination does to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

from claude_agent_sdk import ClaudeAgentOptions, PermissionMode

from astrabox.common.utils.errors import APIError

# Import for its import-time side effect: registers the claude_code adapter so
# the create_runtime → _start_runtime dispatcher can route to the hosted flow.
from astrabox.core.service.orchestrator.engine import claude_code as _claude_code  # noqa: F401
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineTurnReceipt,
)

# Patch the Claude-only launch/config builders and seam lookups at their owning
# module boundary.
from astrabox.core.service.orchestrator.engine import (
    claude_code_runtime,
    provisioning,
    startup,
)
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_workspace_plan import RuntimeWorkspacePlan
from astrabox.seams.egress_credentials import MCPOutboundCredentialResolution
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.sandbox_disposal import UnjudgedSandbox
from astrabox.seams.sandbox_disposal import SandboxClaim
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxLifecycleProbeResult,
    SandboxProvider,
)

_OPTIONS = ClaudeAgentOptions()  # real SDK shape; option contents are irrelevant here


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeSandbox:
    """A provisioned sandbox handle exposing its image-owned runner."""

    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.runner_controller: _FakeAgent | None = None

    async def get_endpoint(self, port: int) -> Any:
        assert self.runner_controller is not None
        runner_uri = await self.runner_controller.resolve_runner_uri()
        return SimpleNamespace(endpoint=runner_uri.replace("ws://", "http://"), headers={})


class _FakeAgent:
    """Test controller for image-owned runner endpoint failures.

    It is not a provider capability. The fake handle calls this controller
    when the platform resolves the runner endpoint exposed by the image.
    """

    def __init__(
        self,
        *,
        events: list[str],
        sandbox: _FakeSandbox,
        launch_runner_error: BaseException | None = None,
        on_launch_runner: Any = None,
    ) -> None:
        self.events = events
        self._sandbox = sandbox
        self._launch_runner_error = launch_runner_error
        self._on_launch_runner = on_launch_runner

    async def resolve_runner_uri(self) -> str:
        self.events.append("launch_runner")
        if self._on_launch_runner is not None:
            self._on_launch_runner()
        if self._launch_runner_error is not None:
            raise self._launch_runner_error
        return "ws://127.0.0.1:1/fake-runner"


class _FakeEngineClient:
    """What the patched runner-connect seam hands the flow."""

    def __init__(self) -> None:
        self.closed = 0
        self.binding: EngineConversationBinding | None = None

    @property
    def is_live(self) -> bool:
        return self.binding is not None and self.closed == 0

    @property
    def engine_session_key(self) -> str | None:
        return self.binding.engine_session_key if self.binding is not None else None

    async def bind_conversation(self, binding: EngineConversationBinding) -> None:
        self.binding = binding

    async def deliver(self, command: EngineInputCommand) -> None:
        _ = command

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        return EngineTurnReceipt(
            engine_turn_id=command.command_id,
            engine_session_key=(
                (self.binding.engine_session_key if self.binding else None)
                or "fake-engine-session"
            ),
            started_at_monotonic_ns=1,
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )

    async def iter_turn_events(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[dict[str, Any]]:
        _ = receipt
        if False:
            yield {}

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        _ = receipt
        return True

    async def interrupt_active_turn(self) -> bool:
        return True

    async def set_permission_mode(self, mode: str) -> None:
        _ = mode

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="claude_code",
            permission_modes=list(get_args(PermissionMode)),
        )

    def start_resident_observation(self) -> None:
        pass

    async def close(self) -> None:
        self.closed += 1


class _FakeBackendProvider:
    """The provider seam, including the BY-ID lifecycle a destroy goes through.

    Reaping does not use the live handle's ``kill()``: that reports only that
    an SDK call returned, and a destruction nothing observed twice may not
    license forgetting the box. It goes through
    ``SandboxProvider.confirm_destroyed``, which is reused here verbatim so
    these tests characterize the real judgement rather than a re-implementation
    of it.
    """

    name = "fake_backend"
    supports_correlated_create = True
    supports_create_network_policy = True

    # The single minting point for a destruction verdict, taken from the seam
    # itself: only ``kill`` and ``probe`` below are this fake's own.
    confirm_destroyed = SandboxProvider.confirm_destroyed

    def __init__(
        self, agent: _FakeAgent, events: list[str], *, create_error: BaseException | None = None
    ) -> None:
        self._agent = agent
        self._events = events
        self._create_error = create_error
        self.create_calls: list[SimpleNamespace] = []
        #: every id this backend was asked to delete, in order.
        self.killed: list[str] = []
        #: what the delete reports, and whether the box survives it.
        self.kill_result = True
        self.survives_the_kill = False
        self.probe_answerable = True

    async def kill(self, sandbox_id: str) -> bool:
        self.killed.append(sandbox_id)
        return self.kill_result

    async def probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        if not self.probe_answerable:
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text="the control plane could not be reached",
            )
        if sandbox_id in self.killed and not self.survives_the_kill:
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
            )
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state="running"
        )

    async def claim_of(
        self,
        sandbox_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> SandboxClaim:
        return SandboxClaim.mine(
            sandbox_id,
            detail="the fake create named this Session",
            session_id=expected_session_id,
        )

    async def find_sandbox_by_assignment(self, assignment_id: str) -> None:
        return None

    async def create_sandbox(self, create_spec: Any) -> _FakeSandbox:
        self.create_calls.append(
            SimpleNamespace(
                create_spec=create_spec,
                session_id=create_spec.session_id,
                assignment_id=create_spec.assignment_id,
                cwd=create_spec.cwd,
                image=create_spec.image,
                sandbox_permission_level=create_spec.permission_level,
                network_policy=create_spec.network_policy,
            )
        )
        self._events.append("create_sandbox")
        if self._create_error is not None:
            raise self._create_error
        return self._agent._sandbox

    async def connect(self, sandbox_id: str) -> _FakeSandbox:
        return self._agent._sandbox

    async def apply_credential_vault(self, sandbox: Any, **kwargs: Any) -> None:
        return None


class _FakeWorkspace:
    def __init__(
        self,
        events: list[str],
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        self._events = events
        self._runtime_identity = runtime_identity
        self.provisioned_runtime_identity: dict[str, Any] | None = None

    def capability_scope(self) -> str:
        return "conversation"

    def plan_runtime_identity(self, **_kwargs: Any) -> dict[str, Any] | None:
        return (
            dict(self._runtime_identity)
            if isinstance(self._runtime_identity, dict)
            else None
        )

    async def mount_and_provision(self, *_args: Any, **_kwargs: Any) -> None:
        self._events.append("mount_and_provision")
        self.provisioned_runtime_identity = (
            dict(self._runtime_identity)
            if isinstance(self._runtime_identity, dict)
            else None
        )


class _FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {
            "s-1": {
                "session_id": "s-1",
                "state": "CREATING",
                "sandbox_backend": "fake_backend",
            }
        }

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.rows.get(session_id)
        return dict(row) if row is not None else None

    async def get_session_including_deleted(
        self, session_id: str
    ) -> dict[str, Any] | None:
        return await self.get_session(session_id)

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.rows.values()
            if row.get("sandbox_id") == sandbox_id and row.get("deleted") is not True
        ]

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: dict[str, Any],
    ) -> bool:
        self.rows[session_id]["startup_allocation"] = dict(allocation)
        return True

    async def clear_startup_allocation(
        self,
        session_id: str,
        *,
        allocation: dict[str, Any],
    ) -> bool:
        row = self.rows.get(session_id)
        if row is None:
            return True
        current = row.get("startup_allocation")
        if isinstance(current, dict) and current != allocation:
            return True
        row["startup_allocation"] = None
        return True

    async def list_startup_allocation_candidates(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.rows.values()
            if isinstance(row.get("startup_allocation"), dict)
        ][:limit]


# ── helpers ──────────────────────────────────────────────────────────────────


def _template() -> SimpleNamespace:
    # `prewarm=None` / `environment_name=None`: an environment that never opted
    # into a sandbox pool, which is what every case in this file characterizes —
    # the COLD create path. The prewarm borrow has its own suite
    # (tests/open_sandbox_prewarm_test.py).
    return SimpleNamespace(
        model_config={},
        mcp_servers={},
        sandbox_backend=None,
        name="tpl",
        runtime_template_name="img-test",
        engine_kind="claude_code",
        agent_id="",
        prewarm_enabled=False,
        environment_name=None,
        prewarm=None,
    )


def _plan(session_id: str, *, cwd: str = "") -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="deployment_conversation",
        session_kind="agent_chat",
        operation="runtime_start",
        runtime_key=session_id,
        conversation_session_id=session_id,
        cwd=cwd or f"/workspace/{session_id}",
        resume_engine_session_key=None,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="claude_code",
        agent_id="agent-1",
    )


def _attach_plan(session_id: str, *, cwd: str = "") -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="deployment_conversation",
        session_kind="agent_chat",
        operation="runtime_attach",
        runtime_key=session_id,
        conversation_session_id=session_id,
        cwd=cwd or f"/workspace/{session_id}",
        resume_engine_session_key=None,
        sandbox_id="sbx-1",
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="claude_code",
        agent_id="agent-1",
    )


def _scenario(
    *,
    launch_runner_error: BaseException | None = None,
    on_launch_runner: Any = None,
    create_error: BaseException | None = None,
) -> SimpleNamespace:
    events: list[str] = []
    sandbox = _FakeSandbox("sbx-1")
    agent = _FakeAgent(
        events=events,
        sandbox=sandbox,
        launch_runner_error=launch_runner_error,
        on_launch_runner=on_launch_runner,
    )
    sandbox.runner_controller = agent
    backend = _FakeBackendProvider(agent, events, create_error=create_error)
    sessions_repo = _FakeSessionRepository()
    manager = RemoteAgentRuntimeManager(sessions_repo=sessions_repo)
    return SimpleNamespace(
        manager=manager,
        sessions_repo=sessions_repo,
        agent=agent,
        sandbox=sandbox,
        engine_client=_FakeEngineClient(),
        backend=backend,
        events=events,
    )


@contextlib.contextmanager
def _hosted_flow_seams(
    manager: RemoteAgentRuntimeManager,
    backend: _FakeBackendProvider,
    *,
    api_key: str = "sk-test-key",
    base_url: str = "https://model.test",
    engine_client: Any = None,
    workspace_identity: dict[str, Any] | None = None,
) -> Any:
    """Stub the config/SDK delegation + module seams so the flow reaches the
    provider create/engine activation sequence with fakes and no network/db.

    The Claude-only launch/config builders and seam lookups are patched in
    ``engine.claude_code_runtime``. Model access remains a manager method
    reached through the single ``resolve_model_access`` platform operation.
    """
    with contextlib.ExitStack() as stack:
        e = stack.enter_context
        e(
            patch.object(
                manager,
                "_resolve_model_access",
                lambda mc: ResolvedModelAccess(
                    configuration=dict(mc or {}),
                    base_url=base_url,
                    model_name=str((mc or {}).get("model_name") or "spec-model"),
                    credential=api_key,
                    credential_kind="bearer",
                    endpoint_provider="litellm",
                ),
            )
        )
        # This suite owns only the hosted runtime lifecycle. Session-bound
        # credentials are repository-backed and have dedicated vault +
        # PostgreSQL coverage, so keep this fake-backend seam database-free.
        e(
            patch.object(
                manager,
                "resolve_session_egress_credentials",
                new=AsyncMock(return_value=[]),
            )
        )
        e(
            patch.object(
                manager,
                "resolve_session_mcp_credentials",
                new=AsyncMock(
                    return_value=MCPOutboundCredentialResolution(
                        scope_id="hosted-flow-scope"
                    )
                ),
            )
        )
        e(
            patch.object(
                manager,
                "resolve_runtime_sandbox_backend",
                new=AsyncMock(return_value=backend.name),
            )
        )
        e(patch.object(claude_code_runtime, "_build_claude_options", lambda *a, **k: _OPTIONS))
        e(patch.object(
            claude_code_runtime,
            "_connect_runner_engine_client",
            new=AsyncMock(return_value=engine_client if engine_client is not None else _FakeEngineClient()),
        ))
        e(patch.object(
            claude_code_runtime,
            "_attach_runner_engine_client",
            new=AsyncMock(return_value=(
                engine_client if engine_client is not None else _FakeEngineClient(),
                "attached",
            )),
        ))
        import astrabox.seams.sandbox as sandbox_seam
        from astrabox.core.service.orchestrator import workspace as workspace_module

        e(patch.object(sandbox_seam, "sandbox_for_name", lambda name: backend))
        # The by-id destroy path: which backend owns a sandbox id, and the
        # provider it resolves to. Both are patched because destroy goes
        # through this by-id resolution rather than a live handle.
        e(patch.object(runtime_manager_module, "sandbox_for_name", lambda name: backend))
        e(patch.object(
            runtime_manager_module.AssistantWorkspaceRepository,
            "list_workspaces_by_sandbox_id",
            new=AsyncMock(return_value=[]),
        ))
        e(patch.object(
            runtime_manager_module.AgentRepository,
            "find_agent_by_sandbox_id",
            new=AsyncMock(return_value=None),
        ))
        e(patch.object(
            manager,
            "_resolve_sandbox_backend",
            new=AsyncMock(return_value=backend.name),
        ))
        e(
            patch.object(
                provisioning,
                "resolve_network_policy",
                lambda template, **_kwargs: None,
            )
        )
        # This suite characterizes the generic executor lifecycle with a fake
        # backend that intentionally implements no credential sidecar. Vault
        # delivery has its own real-backend suites, so opt out explicitly here.
        # The real settings with ONE field overridden, rather than a stand-in
        # carrying only that field: a namespace holding what this suite happens
        # to have needed goes dark the moment the start path reads anything
        # else, and reads as a failure of the code under test.
        from astrabox.common.utils.settings import load_astrabox_settings

        e(patch.object(
            provisioning,
            "load_astrabox_settings",
            lambda: load_astrabox_settings().model_copy(
                update={"sandbox_credential_vault_enabled": False}
            ),
        ))
        e(patch.object(
            claude_code_runtime,
            "load_astrabox_settings",
            lambda: load_astrabox_settings().model_copy(
                update={"sandbox_credential_vault_enabled": False}
            ),
        ))
        e(patch.object(
            workspace_module,
            "workspace_from_subject_kind",
            lambda *a, **k: _FakeWorkspace(
                backend._events,
                workspace_identity,
            ),
        ))
        e(patch.object(
            provisioning, "resolve_runtime_template_name", lambda template: "img-test"
        ))
        e(patch.object(
            provisioning,
            "plan_workspace_mounts",
            new=AsyncMock(return_value=()),
        ))
        e(patch.object(
            provisioning,
            "workspace_is_ready",
            new=AsyncMock(return_value=None),
        ))
        e(patch.object(
            startup,
            "_platform_access_targets",
            lambda *a, **k: (None, None),
        ))
        e(patch.object(sandbox_seam, "sandbox_for_sandbox", lambda sandbox: backend))
        yield


async def _start(
    manager: RemoteAgentRuntimeManager,
    session_id: str,
    template: Any,
    *,
    assignment_id: str = "assignment-1",
    workspace_plan: RuntimeWorkspacePlan,
    progress_callback: Any = None,
) -> Any:
    return await startup.start_platform_runtime(
        manager,
        _claude_code.ClaudeCodeEngineAdapter(),
        session_id=session_id,
        assignment_id=assignment_id,
        template=template,
        workspace_plan=workspace_plan,
        user_id=None,
        permission_mode=None,
        progress_callback=progress_callback,
        callback_url=None,
    )


# ── fail-loud model gate ─────────────────────────────────────────────────────


class ModelConfigGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_api_key_fails_before_any_provisioning(self) -> None:
        sc = _scenario()
        with _hosted_flow_seams(sc.manager, sc.backend, api_key=""):
            with self.assertRaises(APIError) as ctx:
                await _start(
                    sc.manager,
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        self.assertEqual(ctx.exception.status_code, 500)
        # No provider create was attempted — the gate is before provisioning.
        self.assertEqual(sc.backend.create_calls, [])
        self.assertEqual(sc.events, [])


# ── failure paths: cleanup at each step ──────────────────────────────────────


class ProviderCreateFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_failure_is_normalized_without_cleanup(self) -> None:
        # No resource exists yet, so platform rollback has nothing to release.
        sc = _scenario(create_error=RuntimeError("provider create failed"))
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with self.assertRaises(APIError) as ctx:
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(sc.events, ["create_sandbox"])
        self.assertEqual(sc.manager._pending_startup_sandbox_ids("s-1"), [])
        self.assertEqual(sc.backend.killed, [])


class PostCreateFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_endpoint_failure_reaps_the_started_sandbox(self) -> None:
        # Endpoint resolution fails after create returned a box, so rollback
        # must release the allocation and a confirmed destroy leaks no id.
        sc = _scenario(launch_runner_error=RuntimeError("runner endpoint failed"))
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with self.assertRaises(APIError) as ctx:
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(sc.backend.killed, ["sbx-1"])  # reaped, not orphaned
        self.assertIsNone(ctx.exception.data)  # confirmed gone -> no leaked id
        # The name is dropped because — and only because — the destruction was
        # CONFIRMED by a second observation.
        self.assertEqual(sc.manager._pending_startup_sandbox_ids("s-1"), [])

    async def test_failed_reap_surfaces_leaked_sandbox_id(self) -> None:
        # If provider destruction cannot be confirmed, the sandbox is a leak
        # and its id is surfaced in the error data for the caller to chase.
        sc = _scenario(launch_runner_error=RuntimeError("runner endpoint failed"))

        async def _unconfirmed_destroy(_sid: str) -> Any:
            from astrabox.seams.sandbox_disposal import SandboxDestruction

            return SandboxDestruction.unconfirmed(_sid, detail="kill failed")

        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with patch.object(sc.manager, "destroy_sandbox_by_id", _unconfirmed_destroy):
                with self.assertRaises(APIError) as ctx:
                    await sc.manager.create_runtime(
                        "s-1",
                        _template(),
                        assignment_id="assignment-1",
                        workspace_plan=_plan("s-1"),
                    )
        self.assertEqual(ctx.exception.status_code, 502)
        assert ctx.exception.data is not None
        self.assertEqual(ctx.exception.data["leaked_sandbox_id"], "sbx-1")
        self.assertEqual(sc.manager._pending_startup_sandbox_ids("s-1"), ["sbx-1"])

    async def test_an_unanswerable_probe_is_not_evidence_the_box_is_gone(self) -> None:
        # An unreachable control plane is not an absent sandbox: the delete may
        # well have worked, but nothing here established it, so the name stays.
        sc = _scenario(launch_runner_error=RuntimeError("runner endpoint failed"))
        sc.backend.probe_answerable = False
        with _hosted_flow_seams(sc.manager, sc.backend):
            with self.assertRaises(APIError) as ctx:
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        assert ctx.exception.data is not None
        self.assertEqual(ctx.exception.data["leaked_sandbox_id"], "sbx-1")
        self.assertEqual(sc.manager._pending_startup_sandbox_ids("s-1"), ["sbx-1"])

class CancellationFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_not_quiescing_runs_full_cleanup(self) -> None:
        # A cancel that is NOT a shutdown reaps the box (full cleanup) and
        # re-raises CancelledError (never an APIError).
        sc = _scenario(launch_runner_error=asyncio.CancelledError())
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with self.assertRaises(asyncio.CancelledError):
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        self.assertEqual(sc.backend.killed, ["sbx-1"])

    async def test_cancel_while_quiescing_preserves_the_sandbox(self) -> None:
        # Under shutdown the fast-path disconnects the client only and re-raises;
        # the box is deliberately NOT killed (a shutdown must not reap live work).
        sc = _scenario(launch_runner_error=asyncio.CancelledError())
        sc.agent._on_launch_runner = lambda: setattr(
            sc.manager, "_quiesced_reason", "shutdown"
        )
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with self.assertRaises(asyncio.CancelledError):
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                )
        self.assertEqual(sc.backend.killed, [])



# ── caller seam: registration (create_runtime) ───────────────────────────────


class CreateRuntimeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_runtime_stays_named_until_the_ready_write(self) -> None:
        sc = _scenario()
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            runtime = await sc.manager.create_runtime(
                "s-1",
                _template(),
                assignment_id="assignment-1",
                workspace_plan=_plan("s-1"),
                user_id="u-1",
            )
        self.assertIs(sc.manager._runtimes["s-1"], runtime)
        self.assertEqual(runtime.user_id, "u-1")
        self.assertTrue(getattr(runtime, "permission_mode_verified", False))
        # Process-local registration is not the durable publication boundary.
        self.assertEqual(sc.manager._pending_startup_sandbox_ids("s-1"), ["sbx-1"])
        allocation = sc.sessions_repo.rows["s-1"]["startup_allocation"]
        self.assertEqual(allocation["sandbox_id"], "sbx-1")

    async def test_quiesce_during_start_aborts_registration_and_disconnects(self) -> None:
        # A manager that begins quiescing mid-start returns from _start_runtime,
        # then the post-start quiesce gate fires: the started runtime is NOT
        # registered and its client is disconnected.
        sc = _scenario()

        def _quiesce_now() -> None:
            sc.manager._quiesced_reason = "shutdown"

        sc.agent._on_launch_runner = _quiesce_now
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            with self.assertRaises(APIError) as ctx:
                await sc.manager.create_runtime(
                    "s-1",
                    _template(),
                    assignment_id="assignment-1",
                    workspace_plan=_plan("s-1"),
                    user_id="u-1",
                )
        self.assertEqual(ctx.exception.status_code, 503, ctx.exception.message)
        self.assertNotIn("s-1", sc.manager._runtimes)
        self.assertEqual(sc.engine_client.closed, 1)  # engine client torn down


# ── caller seam: registration (lightweight attach) ──────────────────────


class LightweightRuntimeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_publishes_every_adapter_lightweight_attach(self) -> None:
        manager = RemoteAgentRuntimeManager()
        engine_client = _FakeEngineClient()
        await engine_client.bind_conversation(
            EngineConversationBinding(platform_session_id="s-1")
        )
        runtime = runtime_manager_module.SessionRuntime(
            session_id="s-1",
            agent=None,
            engine_kind="claude_code",
            engine_client=engine_client,
            engine_manifest=await engine_client.get_capabilities(),
            conversation_bound=True,
            sandbox=object(),
            sandbox_id="sbx-1",
        )

        attach = AsyncMock(return_value=runtime)
        with (
            patch.object(
                runtime_manager_module,
                "get_engine_adapter",
                return_value=object(),
            ),
            patch.object(startup, "attach_platform_runtime", attach),
        ):
            attached = await manager.ensure_runtime_lightweight(
                "s-1",
                _template(),
                sandbox_id="sbx-1",
                session_kind="agent_chat",
                workspace_plan=_attach_plan("s-1"),
            )

        self.assertIs(attached, runtime)
        self.assertIs(manager.get_runtime("s-1", sandbox_id="sbx-1"), runtime)
        self.assertEqual(attach.await_args.kwargs["attach_mode"], "lightweight")

    async def test_quiesce_during_attach_aborts_publication_and_disconnects(self) -> None:
        manager = RemoteAgentRuntimeManager()
        engine_client = _FakeEngineClient()
        await engine_client.bind_conversation(
            EngineConversationBinding(platform_session_id="s-1")
        )
        runtime = runtime_manager_module.SessionRuntime(
            session_id="s-1",
            agent=None,
            engine_kind="claude_code",
            engine_client=engine_client,
            engine_manifest=await engine_client.get_capabilities(),
            conversation_bound=True,
            sandbox=object(),
            sandbox_id="sbx-1",
        )

        async def _attach_platform_runtime(*args: Any, **kwargs: Any) -> Any:
            _ = (args, kwargs)
            manager._quiesced_reason = "shutdown"
            return runtime

        with (
            patch.object(
                runtime_manager_module,
                "get_engine_adapter",
                return_value=object(),
            ),
            patch.object(
                startup,
                "attach_platform_runtime",
                _attach_platform_runtime,
            ),
        ):
            with self.assertRaises(APIError) as ctx:
                await manager.ensure_runtime_lightweight(
                    "s-1",
                    _template(),
                    sandbox_id="sbx-1",
                    session_kind="agent_chat",
                    workspace_plan=_attach_plan("s-1"),
                )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotIn("s-1", manager._runtimes)
        self.assertEqual(engine_client.closed, 1)


# ── stop / kill interaction with a started runtime ───────────────────────────


class StopKillInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def _register(self) -> SimpleNamespace:
        sc = _scenario()
        with _hosted_flow_seams(sc.manager, sc.backend, engine_client=sc.engine_client):
            await sc.manager.create_runtime(
                "s-1",
                _template(),
                assignment_id="assignment-1",
                workspace_plan=_plan("s-1"),
                user_id="u-1",
            )
        return sc

    async def test_terminate_drops_runtime_and_reaps_sandbox(self) -> None:
        sc = await self._register()
        with _hosted_flow_seams(sc.manager, sc.backend):
            destruction = await sc.manager.terminate_runtime("s-1")
        self.assertTrue(destruction.confirmed)
        with self.assertRaises(UnjudgedSandbox):
            # The verdict has no truth value on purpose: `if killed:` would have
            # to fold "I could not tell" into "there is nothing there", which is
            # the collapse this type exists to make impossible.
            bool(destruction)
        self.assertNotIn("s-1", sc.manager._runtimes)
        self.assertEqual(sc.engine_client.closed, 1)  # engine client torn down
        self.assertEqual(sc.backend.killed, ["sbx-1"])  # box reaped

    async def test_terminate_reports_an_unconfirmed_destruction_as_unconfirmed(
        self,
    ) -> None:
        sc = await self._register()
        sc.backend.survives_the_kill = True
        with _hosted_flow_seams(sc.manager, sc.backend):
            destruction = await sc.manager.terminate_runtime("s-1")
        self.assertFalse(destruction.confirmed)
        self.assertEqual(destruction.leaked_sandbox_id, "sbx-1")

    async def test_terminate_destroys_the_runtimes_own_box_on_a_mismatch(self) -> None:
        """A stale pointer must not get a live box discarded instead of killed.

        The caller's id comes from a row that may have moved, while the runtime
        handle identifies the sandbox it created. Termination destroys both the
        runtime-owned sandbox and the fallback id supplied by the caller.
        """
        sc = await self._register()
        with _hosted_flow_seams(sc.manager, sc.backend):
            destruction = await sc.manager.terminate_runtime(
                "s-1", fallback_sandbox_id="sbx-from-a-stale-row"
            )
        self.assertEqual(sc.backend.killed, ["sbx-1", "sbx-from-a-stale-row"])
        # The verdict is about the box the CALLER asked about, so no pointer is
        # released on the strength of a different box's death.
        self.assertEqual(destruction.sandbox_id, "sbx-from-a-stale-row")

    async def test_evict_drops_runtime_but_preserves_sandbox(self) -> None:
        sc = await self._register()
        with _hosted_flow_seams(sc.manager, sc.backend):
            await sc.manager.evict_runtime("s-1")
        self.assertNotIn("s-1", sc.manager._runtimes)
        self.assertEqual(sc.engine_client.closed, 1)  # engine client torn down
        self.assertEqual(sc.backend.killed, [])  # evict never kills the box


if __name__ == "__main__":
    unittest.main()


class DeadLinkEvictionTests(unittest.TestCase):
    """A dead engine link's runtime is EVICTED at lookup, not reused.

    Without this, a crashed runner leaves its runtime in the map and every
    later turn re-fails on the same closed websocket in milliseconds — the
    ensure path reads get_runtime, sees a runtime, and never re-attaches:
    ensure answered ATTACHED in 0.075ms against a link that 1001'd on the
    previous turn.
    """

    def _manager_with(self, runtime: Any) -> RemoteAgentRuntimeManager:
        manager = RemoteAgentRuntimeManager()
        manager._runtimes["s-1"] = runtime
        return manager

    def test_a_dead_link_runtime_is_popped_and_not_returned(self) -> None:
        runtime = SimpleNamespace(
            engine_client=SimpleNamespace(is_live=False),
            sandbox_id="sbx-1",
        )
        manager = self._manager_with(runtime)
        assert manager.get_runtime("s-1", sandbox_id="sbx-1") is None
        assert "s-1" not in manager._runtimes, (
            "a dead-link runtime left in the map would shadow the fresh one "
            "the caller is about to attach"
        )

    def test_a_live_link_runtime_is_returned(self) -> None:
        runtime = SimpleNamespace(
            engine_client=SimpleNamespace(is_live=True),
            sandbox_id="sbx-1",
        )
        manager = self._manager_with(runtime)
        assert manager.get_runtime("s-1", sandbox_id="sbx-1") is runtime

    def test_a_client_without_the_liveness_contract_is_evicted(self) -> None:
        runtime = SimpleNamespace(
            engine_client=SimpleNamespace(),
            sandbox_id="sbx-1",
        )
        manager = self._manager_with(runtime)
        assert manager.get_runtime("s-1", sandbox_id="sbx-1") is None
        assert "s-1" not in manager._runtimes
    def test_a_client_with_an_indeterminate_liveness_signal_is_evicted(self) -> None:
        runtime = SimpleNamespace(
            engine_client=SimpleNamespace(is_live=None),
            sandbox_id="sbx-1",
        )
        manager = self._manager_with(runtime)
        assert manager.get_runtime("s-1", sandbox_id="sbx-1") is None
        assert "s-1" not in manager._runtimes
