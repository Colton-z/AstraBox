"""An adapter declares what a sandbox needs; it does not build one itself.

A setup step an adapter has to know about produces, when missed, a sandbox
that reaches READY and fails somewhere else. These tests hold the property
rather than the individual steps: an adapter that builds its own create spec,
or a change that drops the identity plan or the credential delivery, fails
here rather than against a deployment.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    deepseek_harness as _dsh_engine,  # module import registers the adapter
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
    connect_engine_sandbox,
    provision_engine_sandbox,
    resolve_model_credential_delivery,
    sandbox_create_resources,
)
from astrabox.seams.egress_credentials import (
    EgressCredential,
    SandboxEgressCredentialPlan,
)
from astrabox.seams.model import ResolvedModelAccess

_ENGINE_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrabox"
    / "core"
    / "service"
    / "orchestrator"
    / "engine"
)


def _credential(**overrides: Any) -> ModelCredentialRequest:
    return ModelCredentialRequest(
        **{
            "access": ResolvedModelAccess(
                configuration={"model": "m"},
                base_url="https://gateway.test",
                model_name="m",
                credential="sk-real-key",
                credential_kind="bearer",
                endpoint_provider="test",
            ),
            "request_paths": ("chat/completions",),
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
        return SimpleNamespace(sandbox_id="box-1", id="box-1")


class _Manager:
    def __init__(self) -> None:
        self.tracked: list[tuple[str, Any]] = []

    async def resolve_runtime_sandbox_backend(self, session_id: str, **_: Any) -> str:
        return "fake"

    async def resolve_session_egress_credentials(
        self,
        _session_id: str,
        *,
        placeholder_context: str | None = None,
    ) -> list[Any]:
        _ = placeholder_context
        return []

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: Any,
    ) -> None:
        self.tracked.append((session_id, allocation.sandbox_id))

    @property
    def deployment_settings(self) -> Any:
        # Read for the box-reachable backend address, because an engine that
        # keeps its transcript in a file it never hands over is mirrored by
        # this contract rather than by its adapter.
        return SimpleNamespace(mcp_proxy_base_url="http://backend.contract.test:8000")


def _workspace_plan(cwd: str = "/home/agent/workspace") -> Any:
    return SimpleNamespace(
        subject_kind="deployment_conversation",
        agent_id="agent-1",
        assistant_id=None,
        engine_kind="deepseek_harness",
        cwd=cwd,
    )


@pytest.fixture(autouse=True)
def workspace_preparation_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep recipe tests outside in-box Plugin and Skill installation."""
    async def prepare(*_args: Any, **kwargs: Any) -> tuple[Any, str]:
        return kwargs["runtime_identity"], kwargs["cwd"]

    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.engine.provisioning.prepare_platform_workspace",
        prepare,
    )


async def _provision(
    monkeypatch: pytest.MonkeyPatch,
    request: EngineSandboxRequest,
    *,
    manager: _Manager | None = None,
    backend: _Backend | None = None,
    workspace_plan: Any | None = None,
    progress_callback: Any = None,
    sandbox_tenancy: str = "conversation",
) -> Any:
    backend = backend or _Backend()
    manager = manager or _Manager()
    import astrabox.seams.sandbox as sandbox_seam

    monkeypatch.setattr(sandbox_seam, "sandbox_for_name", lambda name: backend)
    return await provision_engine_sandbox(
        manager,
        session_id="6f5d2c1b-0a99-4d3e-8b77-1c2d3e4f5a6b",
        assignment_id="assignment-contract-1",
        template=AgentView(
            engine_kind="deepseek_harness",
            model_config={},
            networking={"type": "limited", "allow_mcp_servers": True},
            runtime_template_name="astrabox/sandbox-test:latest",
            sandbox_permission_level="advanced",
            sandbox_tenancy=sandbox_tenancy,
        ),
        workspace_plan=workspace_plan if workspace_plan is not None else _workspace_plan(),
        user_id=None,
        callback_url=None,
        request=request,
        progress_callback=progress_callback,
    )


@pytest.mark.asyncio
async def test_cold_create_reports_mounting_immediately_before_workspace_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.engine import provisioning

    events: list[str] = []

    async def progress(stage: str) -> None:
        events.append(f"progress:{stage}")

    async def prepare(*_args: Any, **kwargs: Any) -> tuple[Any, str]:
        events.append("prepare_workspace")
        return kwargs["runtime_identity"], kwargs["cwd"]

    monkeypatch.setattr(provisioning, "prepare_platform_workspace", prepare)
    await _provision(
        monkeypatch,
        EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
        progress_callback=progress,
    )

    assert events == ["progress:mounting_nas", "prepare_workspace"]


@pytest.mark.asyncio
async def test_prepared_claim_does_not_report_workspace_mounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.engine import provisioning

    prepared = provisioning.ProvisionedEngineSandbox(
        sandbox=object(),
        sandbox_id="prepared-box",
        runtime_identity={"workspace_dir": "/workspace"},
        cwd="/workspace",
        model_credential="placeholder",
        prepared_manifest={"slot_id": "slot-1"},
    )
    monkeypatch.setattr(
        provisioning,
        "claim_prepared_engine_sandbox",
        AsyncMock(return_value=prepared),
    )
    progress = AsyncMock()

    result = await _provision(
        monkeypatch,
        EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
        progress_callback=progress,
        sandbox_tenancy="agent",
    )

    assert result is prepared
    progress.assert_not_awaited()


class TestTheContractComposesWhatAdaptersUsedToForget:
    def test_environment_policy_stays_separate_until_the_provider_maps_vault_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from astrabox.core.service.orchestrator.engine import provisioning

        monkeypatch.setattr(
            "astrabox.core.service.orchestrator.runtime.config_resolver."
            "platform_callback_egress_targets",
            lambda: [],
        )
        template = AgentView(
            engine_kind="deepseek_harness",
            networking={
                "type": "limited",
                "allowed_hosts": ["operator.example"],
                "allow_mcp_servers": False,
            },
            tracing=None,
        )
        credential_plan = SandboxEgressCredentialPlan(
            environment=(
                EgressCredential(
                    credential_id="unused",
                    secret_name="TOKEN",
                    secret_value="secret",
                    placeholder="ASTRABOX-VAULT-CRED::unused::nonce",
                    networking={
                        "type": "limited",
                        "allowed_hosts": ["credential-only.example"],
                    },
                    injection_location={"header": True},
                ),
            )
        )

        monkeypatch.setattr(
            provisioning,
            "load_astrabox_settings",
            lambda: SimpleNamespace(sandbox_credential_vault_enabled=False),
        )
        unprotected = resolve_model_credential_delivery(
            template=template,
            backend_adapter=_Backend(),
            credential=_credential(),
            additional_vault_write=credential_plan,
        )
        monkeypatch.setattr(
            provisioning,
            "load_astrabox_settings",
            lambda: SimpleNamespace(sandbox_credential_vault_enabled=True),
        )
        protected = resolve_model_credential_delivery(
            template=template,
            backend_adapter=_Backend(),
            credential=_credential(),
            additional_vault_write=credential_plan,
        )

        assert unprotected[1] == protected[1]
        assert protected[1].allowed_hosts == (
            "operator.example",
            "gateway.test",
        )
        assert "credential-only.example" not in protected[1].allowed_hosts
        assert unprotected[2] is None
        assert protected[2] is not None

    @pytest.mark.asyncio
    async def test_the_box_is_tracked_before_the_caller_can_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _Manager()
        result = await _provision(
            monkeypatch,
            EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
            manager=manager,
        )
        # An untracked box that fails during engine start is a leak nothing
        # later can name.
        assert manager.tracked == [
            (
                "6f5d2c1b-0a99-4d3e-8b77-1c2d3e4f5a6b",
                result.sandbox_id,
            )
        ]

    @pytest.mark.asyncio
    async def test_the_entrypoint_is_the_engines_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _Backend()
        await _provision(
            monkeypatch,
            EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
            backend=backend,
        )
        # The provider's None default is the Claude image's boot script, which
        # another image exits 127 on. The contract has no default at all.
        assert backend.spec.entrypoint == ("/opt/gem/run.sh",)
        assert backend.spec.permission_level == "advanced"
        assert backend.spec.resource_limits == sandbox_create_resources()[0]
        assert backend.spec.resource_requests == {
            "cpu": "200m",
            "memory": "768Mi",
        }

    @pytest.mark.asyncio
    async def test_reattach_reproves_the_running_box_permission_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Reattached:
            sandbox_id = "box-1"

            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1

        class ReattachManager:
            async def connect_sandbox_only(self, sandbox_id: str) -> Any:
                assert sandbox_id == "box-1"
                return sandbox

        class Provider:
            name = "capable"
            supported_permission_levels = ("default", "advanced")

            def __init__(self) -> None:
                self.probes: list[str] = []

            async def read_isolation_capability(self, sandbox_id: str) -> Any:
                self.probes.append(sandbox_id)
                return SimpleNamespace(available=True, detail=None)

        sandbox = Reattached()
        provider = Provider()
        import astrabox.seams.sandbox as sandbox_seam

        monkeypatch.setattr(sandbox_seam, "sandbox_for_sandbox", lambda _value: provider)
        result = await connect_engine_sandbox(
            ReattachManager(),
            sandbox_id="box-1",
            template=SimpleNamespace(sandbox_permission_level="advanced"),
        )

        assert result is sandbox
        assert provider.probes == ["box-1"]
        assert sandbox.close_calls == 0

    @pytest.mark.asyncio
    async def test_reattach_closes_a_box_that_cannot_prove_the_requested_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Reattached:
            sandbox_id = "box-1"

            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1

        class ReattachManager:
            async def connect_sandbox_only(self, sandbox_id: str) -> Any:
                assert sandbox_id == "box-1"
                return sandbox

        async def unavailable(_sandbox_id: str) -> Any:
            return SimpleNamespace(available=False, detail="namespace creation denied")

        provider = SimpleNamespace(
            name="incapable",
            supported_permission_levels=("default", "advanced"),
            read_isolation_capability=unavailable,
        )
        sandbox = Reattached()
        import astrabox.seams.sandbox as sandbox_seam

        monkeypatch.setattr(sandbox_seam, "sandbox_for_sandbox", lambda _value: provider)
        with pytest.raises(APIError, match="namespace creation denied"):
            await connect_engine_sandbox(
                ReattachManager(),
                sandbox_id="box-1",
                template=SimpleNamespace(sandbox_permission_level="advanced"),
            )

        assert sandbox.close_calls == 1

    @pytest.mark.asyncio
    async def test_an_engine_that_keeps_its_transcript_gets_told_where_to_send_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declaring the log is the whole of an adapter's part in mirroring it.

        The engine under this contract writes its conversation to a file and
        offers none of it over its protocol, so the box is the only place that
        conversation exists. Composing the relay's address here rather than in
        each adapter is what makes every such engine mirrored the same way —
        and an adapter that forgot to would be a box whose session cannot be
        resumed anywhere else, with nothing to show for it until someone tries.
        """

        from astrabox.core.service.orchestrator.engine import transcript_mirror

        monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "false")
        backend = _Backend()
        await _provision(
            monkeypatch,
            EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
            backend=backend,
        )
        env = dict(backend.spec.env)
        assert env[transcript_mirror.TRANSCRIPT_BASE_URL_ENV] == (
            "http://backend.contract.test:8000"
        )
        assert env[transcript_mirror.PLATFORM_SESSION_ID_ENV]
        assert env[transcript_mirror.TRANSCRIPT_PROJECT_KEY_ENV]

    @pytest.mark.asyncio
    async def test_an_engine_env_may_not_shadow_what_the_platform_composes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A credential declared here would disagree with the sidecar's, and a
        # cwd declared here would name a directory the box is not in.
        with pytest.raises(RuntimeError, match="platform composes"):
            await _provision(
                monkeypatch,
                EngineSandboxRequest(
                    entrypoint=("/x",),
                    credential=_credential(),
                    credential_env_var="KEY",
                    env={"KEY": "sk-mine"},
                ),
            )

    @pytest.mark.asyncio
    async def test_a_missing_credential_speaks_the_engines_own_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(APIError) as excinfo:
            await _provision(
                monkeypatch,
                EngineSandboxRequest(
                    entrypoint=("/x",),
                    credential=_credential(
                        access=ResolvedModelAccess(
                            configuration={},
                            base_url=None,
                            model_name="m",
                            credential="sk-real-key",
                            credential_kind="bearer",
                            endpoint_provider="test",
                        ),
                        missing_code="HERMES_MODEL_API_KEY_NOT_CONFIGURED",
                        missing_status=500,
                    ),
                ),
            )
        assert excinfo.value.code == "HERMES_MODEL_API_KEY_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_an_engine_that_owns_its_identity_gets_no_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _provision(
            monkeypatch,
            EngineSandboxRequest(
                entrypoint=("/x",), credential=_credential(), plan_identity=False
            ),
        )
        # Hermes provisions its own inside the box; an unused plan beside it
        # would be a second answer to one question.
        assert result.runtime_identity is None


class TestNoAdapterStepsAroundTheContract:
    """The structural half: the property, not one adapter's current code."""

    @staticmethod
    def _adapter_modules() -> list[Path]:
        return [
            path
            for path in sorted(_ENGINE_DIR.glob("*.py"))
            if path.name not in {"provisioning.py", "__init__.py"}
        ]

    def test_only_the_contract_builds_a_create_spec(self) -> None:
        offenders = []
        for path in self._adapter_modules():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "SandboxCreateSpec"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            "these engine modules build their own sandbox create spec: "
            f"{offenders}. Provisioning is a declaration — add what you need "
            "to EngineSandboxRequest so no adapter can omit a step."
        )

    def test_the_check_would_see_a_hand_rolled_spec(self, tmp_path: Path) -> None:
        # The gate above passes on a clean tree either way, so prove it reads
        # the construction rather than the tree's current tidiness.
        offending = tmp_path / "rogue.py"
        offending.write_text("spec = SandboxCreateSpec(session_id='s')\n")
        found = [
            node
            for node in ast.walk(ast.parse(offending.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SandboxCreateSpec"
        ]
        assert len(found) == 1


class TestARejoinTheStoreCannotServe:
    """What a box does when the conversation it was told to rejoin is not there.

    An engine whose transcript the platform moves cannot rejoin a conversation
    whose log never reached the store: the file the engine looks for does not
    exist, and naming the key anyway makes the engine refuse the box in its own
    words (`no rollout found for thread id`, `No session found matching`), which
    ends the session rather than answering. The platform decides that here, once,
    and hands adapters the key it could honour.
    """

    @staticmethod
    def _plan_with_resume_key() -> Any:
        plan = _workspace_plan()
        plan.resume_engine_session_key = "01a015ed-ac8a-7c44-9a07-aba00a3ca195"
        return plan

    @pytest.mark.asyncio
    async def test_nothing_mirrored_means_a_new_conversation_in_this_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from astrabox.core.service.orchestrator.engine import transcript_mirror

        async def _restored_none(*_args: Any, **_kwargs: Any) -> int:
            return 0

        monkeypatch.setattr(transcript_mirror, "restore_mirrored_logs", _restored_none)
        result = await _provision(
            monkeypatch,
            EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
            workspace_plan=self._plan_with_resume_key(),
        )
        assert result.resume_session_key is None

    @pytest.mark.asyncio
    async def test_a_restored_log_keeps_the_key_the_plan_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from astrabox.core.service.orchestrator.engine import transcript_mirror

        async def _restored_one(*_args: Any, **_kwargs: Any) -> int:
            return 1

        monkeypatch.setattr(transcript_mirror, "restore_mirrored_logs", _restored_one)
        plan = self._plan_with_resume_key()
        result = await _provision(
            monkeypatch,
            EngineSandboxRequest(entrypoint=("/opt/gem/run.sh",), credential=_credential()),
            workspace_plan=plan,
        )
        assert result.resume_session_key == plan.resume_engine_session_key
