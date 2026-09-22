"""Startup refusal and failure-evidence contracts at the engine seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import claude_code_runtime
from astrabox.core.service.orchestrator.engine import codex
from astrabox.core.service.orchestrator.engine import deepseek_harness as dsh
from astrabox.core.service.orchestrator.engine import pi
from astrabox.core.service.orchestrator.engine import pi_client
from astrabox.core.service.orchestrator.engine import startup
from astrabox.core.service.orchestrator.engine.claude_code import (
    ClaudeCodeEngineAdapter,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineStartupContext,
    EngineStartupMaterialRequest,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    ProvisionedEngineSandbox,
)


def _activation_context(
    *,
    engine_kind: str,
    resume_session_key: str | None = None,
    attach_mode: str | None = None,
    prepared_manifest: dict[str, Any] | None = None,
) -> EngineStartupContext:
    return EngineStartupContext(
        session_id="session-1",
        template=SimpleNamespace(
            engine_kind=engine_kind,
            engine_options={},
            runtime_template_name=f"astrabox/{engine_kind}:latest",
            system="",
        ),
        workspace_plan=SimpleNamespace(resume_engine_session_key=resume_session_key),
        sandbox=SimpleNamespace(sandbox_id="box-1"),
        sandbox_id="box-1",
        cwd="/workspace",
        runtime_identity={"workspace_dir": "/workspace"},
        model_access=SimpleNamespace(
            base_url="https://gateway.test",
            model_name="model-1",
            credential="sk-model",
            credential_kind="bearer",
            endpoint_provider="test",
        ),
        model_credential="placeholder",
        resume_session_key=resume_session_key,
        prepared_manifest=prepared_manifest,
        runner_uri="ws://runner.test/ws",
        attach_mode=attach_mode,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter",
    [
        codex.CodexEngineAdapter(),
        dsh.DeepSeekHarnessEngineAdapter(),
        pi.PiEngineAdapter(),
    ],
    ids=["codex", "deepseek_harness", "pi"],
)
async def test_reattach_without_a_native_session_key_refuses_before_engine_io(
    monkeypatch: pytest.MonkeyPatch,
    adapter: Any,
) -> None:
    codex_connect = AsyncMock()
    dsh_connect = AsyncMock()
    pi_connect = AsyncMock()
    install_dsh = AsyncMock()
    install_pi = AsyncMock()
    monkeypatch.setattr(codex.CodexAppServerLink, "connect", codex_connect)
    monkeypatch.setattr(dsh.DshApiLink, "connect", dsh_connect)
    monkeypatch.setattr(pi_client, "connect_pi_client", pi_connect)
    monkeypatch.setattr(dsh, "_install_agent_instructions", install_dsh)
    monkeypatch.setattr(pi, "_install_agent_instructions", install_pi)

    with pytest.raises(APIError) as caught:
        await adapter.activate_runtime(
            _activation_context(
                engine_kind=adapter.engine_kind,
                resume_session_key="",
                attach_mode="full",
            )
        )

    assert caught.value.code == "ENGINE_RUNTIME_UNAVAILABLE"
    assert caught.value.status_code == 409
    codex_connect.assert_not_awaited()
    dsh_connect.assert_not_awaited()
    pi_connect.assert_not_awaited()
    install_dsh.assert_not_awaited()
    install_pi.assert_not_awaited()


class _StartupPlatform:
    deployment_settings = SimpleNamespace()

    def resolve_model_access(self, _config: Any) -> Any:
        return SimpleNamespace(
            base_url="https://gateway.test",
            model_name="model-1",
            credential="sk-model",
            credential_kind="bearer",
            endpoint_provider="test",
        )


def _startup_template() -> Any:
    return SimpleNamespace(
        agent_id="",
        engine_kind="claude_code",
        model_config={},
        mcp_servers=None,
        system="",
    )


def _startup_workspace_plan() -> Any:
    return SimpleNamespace(
        subject_kind="deployment_conversation",
        agent_id=None,
        assistant_id=None,
        engine_kind="claude_code",
    )


def _provisioned(*, prepared: bool = False) -> ProvisionedEngineSandbox:
    return ProvisionedEngineSandbox(
        sandbox=SimpleNamespace(sandbox_id="box-1"),
        sandbox_id="box-1",
        runtime_identity={"workspace_dir": "/workspace"},
        cwd="/workspace",
        model_credential="placeholder",
        prepared_manifest={"slot_id": "slot-1", "placement": "shared_slot"} if prepared else None,
        runner_uri="ws://runner.test/ws",
    )


@pytest.mark.asyncio
async def test_claude_cold_activation_failure_carries_diagnostics_before_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator import runtime_manager

    provisioned = _provisioned()
    provider = object()
    events: list[str] = []

    async def collect_diagnostics(*_args: Any, **_kwargs: Any) -> str:
        events.append("diagnostics")
        return "runtime start failure diagnostics: runner traceback"

    class Manager(_StartupPlatform):
        _quiesced_reason = None

        async def _rollback_runtime_start(self, _session_id: str) -> tuple[None, None]:
            events.append("rollback")
            return None, None

        def _runtime_start_error(self, **kwargs: Any) -> APIError:
            original = kwargs["original"]
            assert isinstance(original, APIError)
            return original

    adapter = ClaudeCodeEngineAdapter()
    monkeypatch.setattr(
        adapter,
        "startup_material_request",
        Mock(return_value=EngineStartupMaterialRequest()),
    )
    monkeypatch.setattr(
        startup,
        "provision_engine_sandbox",
        AsyncMock(return_value=provisioned),
    )
    monkeypatch.setattr(
        claude_code_runtime,
        "_build_claude_options",
        lambda *_args, **_kwargs: SimpleNamespace(settings=None, cwd="/workspace"),
    )
    monkeypatch.setattr(claude_code_runtime, "_runner_configure_options", lambda _value: {})

    async def fail_connect(*_args: Any, **_kwargs: Any) -> Any:
        events.append("activate")
        raise RuntimeError("runner link closed")

    monkeypatch.setattr(claude_code_runtime, "_connect_runner_engine_client", fail_connect)
    monkeypatch.setattr(claude_code_runtime, "sandbox_for_sandbox", lambda value: provider)
    collect = AsyncMock(side_effect=collect_diagnostics)
    monkeypatch.setattr(claude_code_runtime, "collect_runtime_start_diagnostics", collect)
    monkeypatch.setattr(startup, "_workspace_capability_scope", lambda *_args: object())
    monkeypatch.setattr(runtime_manager, "get_engine_adapter", lambda _kind: adapter)

    with pytest.raises(APIError) as caught:
        await runtime_manager.RemoteAgentRuntimeManager._start_runtime(
            Manager(),  # type: ignore[arg-type]
            session_id="session-1",
            template=_startup_template(),
            assignment_id="assignment-1",
            user_id=None,
            permission_mode=None,
            progress_callback=None,
            callback_url=None,
            workspace_plan=_startup_workspace_plan(),
        )

    assert caught.value.code == "AGENT_RUNTIME_ERROR"
    assert caught.value.status_code == 502
    assert "runner traceback" in caught.value.message
    assert events == ["activate", "diagnostics", "rollback"]
    collect.assert_awaited_once()
    assert collect.await_args.args == (provisioned.sandbox,)
    assert collect.await_args.kwargs["sandbox_provider"] is provider
