"""Engine adapters declare behavior; no engine inherits Claude by omission."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.providers as providers
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineSettledProjection,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    composed_runtime_profile,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    EngineRuntimeProfileDeclaration,
    EngineWorkloadDeclaration,
    EngineRuntimeCapabilities,
    capabilities_for_engine_kind,
    engine_allowed_for_session_kind,
    engine_kinds_for_session_kind,
    resolve_session_capabilities,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    plan_capabilities,
    resolve_runtime_profile,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    plan_assistant_profile_identity,
    plan_conversation_identity,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    SessionWorkspacePlanner,
)
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import registry
from astrabox.core.service.orchestrator.engine.registry import (
    EngineKindNotRegistered,
    register_engine_adapter,
)

providers.register_builtin_providers()


class _TestEngineClient:
    @property
    def is_live(self) -> bool:
        return True

    @property
    def engine_session_key(self) -> str | None:
        return None

    async def bind_conversation(self, binding: EngineConversationBinding) -> None:
        _ = binding

    async def deliver(self, command: EngineInputCommand) -> None:
        _ = command

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        _ = (command, consumption_confirmed)
        raise NotImplementedError

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

    async def get_capabilities(self) -> EngineCapabilityManifest:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class _TextOnlyEngine(EngineAdapter):
    @property
    def engine_kind(self) -> str:
        return "text_only"

    @property
    def engine_client_type(self) -> type[_TestEngineClient]:
        return _TestEngineClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=self.engine_kind,
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=_text_workload(),
        )

    def sandbox_request(self, *, template, model_access):  # pragma: no cover - stub
        _ = (template, model_access)
        return object()

    async def activate_runtime(self, context):  # pragma: no cover - stub
        _ = context
        raise NotImplementedError

    def slice_recovery_turn(self, raw_items, *, prompt_text):
        _ = prompt_text
        return list(raw_items)

    def has_transcript_terminal_evidence(self, raw_items) -> bool:
        return bool(raw_items)

    def project_settled_transcript(self, raw_items, *, done=False):
        _ = raw_items
        return EngineSettledProjection(
            blocks=[], assistant_text=None, completed=done, has_result=False
        )


def _text_workload() -> EngineWorkloadDeclaration:
    return EngineWorkloadDeclaration(required_commands=("bash",))


class _DeclaredTextEngine(_TextOnlyEngine):
    @property
    def engine_kind(self) -> str:
        return "declared_text"

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=self.engine_kind,
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=_text_workload(),
        )


def _assistant_workload() -> EngineWorkloadDeclaration:
    return EngineWorkloadDeclaration(
        config_dir_name=".example",
        config_env_var="EXAMPLE_HOME",
        required_commands=("bash",),
    )


class _DeclaredAssistantEngine(_TextOnlyEngine):
    @property
    def engine_kind(self) -> str:
        return "declared_assistant"

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind=self.engine_kind,
            supported_session_kinds=frozenset({"assistant_chat"}),
            workload=_assistant_workload(),
            conversation_placement=CONVERSATION_PLACEMENT_PER_ACCOUNT,
        )


def test_claude_code_adapter_declares_its_profile() -> None:
    caps = capabilities_for_engine_kind("claude_code")
    assert caps.supported_session_kinds == frozenset({"agent_chat"})
    assert caps.workload.config_dir_name == ".claude"
    assert caps.workload.config_env_var == "CLAUDE_CONFIG_DIR"
    # Tenancy is composed by the platform, not declared: both compositions
    # exist for this engine and carry its facts unchanged.
    for tenancy in ("agent", "conversation"):
        composed = composed_runtime_profile("claude_code", tenancy)
        assert composed.sandbox_tenancy == tenancy
        assert composed.config_dir_name == ".claude"
    assert dict(caps.permission_mode_defaults) == {
        "agent_chat": "bypassPermissions"
    }
    # `tracing` is an Environment field rather than an Agent one, but the question
    # this set answers is the same: does this adapter actually read it. Claude Code
    # does — it writes the vendor's `OTEL_*` switches into the box — so an
    # Environment may name a collector against it, and one naming an engine that
    # declares nothing is refused at write time.
    assert caps.configuration_inputs == frozenset(
        {"mcp_servers", "skills", "plugin_repos", "tracing"}
    )


def test_assistant_adapter_declares_its_profile() -> None:
    caps = capabilities_for_engine_kind("assistant")
    assert caps.supported_session_kinds == frozenset({"assistant_chat"})
    assert caps.workload.config_dir_name == ".hermes"
    assert caps.workload.config_env_var == "HERMES_HOME"
    assert caps.conversation_placement == "per_conversation_account"
    assert caps.configuration_inputs == frozenset({"mcp_servers"})


def test_deepseek_harness_declares_no_unimplemented_configuration_inputs() -> None:
    caps = capabilities_for_engine_kind("deepseek_harness")
    assert caps.configuration_inputs == frozenset()


def test_unregistered_kind_does_not_inherit_claude() -> None:
    with pytest.raises(EngineKindNotRegistered, match="not registered"):
        capabilities_for_engine_kind("missing_engine")


def test_capabilities_do_not_claim_an_unused_durability_taxonomy() -> None:
    capabilities = EngineRuntimeCapabilities(
        engine_kind="probe",
        supported_session_kinds=frozenset({"agent_chat"}),
        workload=_text_workload(),
    )

    assert not hasattr(capabilities, "durability_source")


def test_permission_mode_default_must_belong_to_the_adapter_declaration() -> None:
    from astrabox.core.service.orchestrator.engine.capabilities import (
        validate_engine_capabilities,
    )

    def declaration(
        *,
        modes: tuple[str, ...],
        defaults: tuple[tuple[str, str], ...],
    ) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind="probe",
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=_text_workload(),
            permission_modes=modes,
            permission_mode_defaults=defaults,
        )

    validate_engine_capabilities(
        "probe",
        declaration(modes=("open",), defaults=(("agent_chat", "open"),)),
    )
    with pytest.raises(ValueError, match="is not in permission_modes"):
        validate_engine_capabilities(
            "probe",
            declaration(modes=("open",), defaults=(("agent_chat", "closed"),)),
        )
    with pytest.raises(ValueError, match="unsupported session_kind"):
        validate_engine_capabilities(
            "probe",
            declaration(modes=("open",), defaults=(("assistant_chat", "open"),)),
        )


def test_adapter_must_explicitly_declare_capabilities() -> None:
    class MissingCapabilities(EngineAdapter):
        @property
        def engine_kind(self) -> str:
            return "missing"

    with pytest.raises(TypeError, match="abstract"):
        MissingCapabilities()


def test_adapter_registration_does_not_restate_resume_as_a_strategy_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTranscriptRecovery(EngineAdapter):
        @property
        def engine_kind(self) -> str:
            return "missing_transcript"

        @property
        def engine_client_type(self) -> type[_TestEngineClient]:
            return _TestEngineClient

        @property
        def capabilities(self) -> EngineRuntimeCapabilities:
            return EngineRuntimeCapabilities(
                engine_kind=self.engine_kind,
                supported_session_kinds=frozenset({"agent_chat"}),
                workload=_text_workload(),
            )

        def sandbox_request(self, *, template, model_access):  # pragma: no cover
            _ = (template, model_access)
            return object()

        async def activate_runtime(self, context):  # pragma: no cover - stub
            _ = context
            raise NotImplementedError

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("missing_transcript", MissingTranscriptRecovery())
    assert registry.get_engine_adapter("missing_transcript").engine_kind == (
        "missing_transcript"
    )


def test_adapter_registration_needs_no_engine_owned_reattach_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PlatformAttachedEngine(_TextOnlyEngine):
        @property
        def engine_kind(self) -> str:
            return "platform_attached"

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("platform_attached", PlatformAttachedEngine())
    assert registry.get_engine_adapter("platform_attached").process_disposal() is None


def test_adapter_registration_rejects_an_incomplete_agent_journey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteClient:
        @property
        def is_live(self) -> bool:
            return True

    class IncompleteEngine(_TextOnlyEngine):
        @property
        def engine_kind(self) -> str:
            return "incomplete_agent"

        @property
        def engine_client_type(self) -> type[IncompleteClient]:
            return IncompleteClient

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    with pytest.raises(TypeError, match="mandatory EngineClient surface") as exc_info:
        register_engine_adapter("incomplete_agent", IncompleteEngine())
    assert "bind_conversation: missing" in str(exc_info.value)
    assert "deliver: missing" in str(exc_info.value)
    assert "interrupt_active_turn: missing" in str(exc_info.value)


def test_text_only_engine_with_no_config_directory_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("text_only", _TextOnlyEngine())

    caps = capabilities_for_engine_kind("text_only")
    assert caps.workload == _text_workload()
    assert caps.workload.config_dir_name is None
    assert engine_allowed_for_session_kind("text_only", "agent_chat") is True
    assert "text_only" in engine_kinds_for_session_kind("agent_chat")


def test_plugin_runtime_profile_is_selected_from_its_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("declared_text", _DeclaredTextEngine())

    profile = resolve_runtime_profile(
        SimpleNamespace(engine_kind="declared_text", sandbox_tenancy="conversation"),
        session_kind="agent_chat",
    )

    # The platform composes the identity; the plugin contributes its facts.
    assert profile.sandbox_tenancy == "conversation"
    assert profile.username_template == "agent"
    assert profile.home_template == "/home/agent"
    assert profile.config_dir_name is None


def test_plugin_assistant_identity_uses_only_its_declared_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("declared_assistant", _DeclaredAssistantEngine())

    identity = plan_assistant_profile_identity(
        engine_kind="declared_assistant",
        user_id="owner-1",
        assistant_id="assistant-1",
        sandbox_id="sandbox-1",
    )

    # The platform derives account and filesystem placement consistently for
    # plugin engines. The plugin supplies only its config directory and env var.
    account = identity["linux_user"]
    assert account.startswith("asst_")
    assert identity["home_dir"] == f"/home/conversations/{account}"
    assert identity["workspace_dir"] == "/workspace"
    assert identity["config_dir"] == f"/home/conversations/{account}/.example"
    assert identity["config_env_var"] == "EXAMPLE_HOME"


def test_hermes_exposes_one_root_workspace_over_its_persistent_profile() -> None:
    identity = plan_assistant_profile_identity(
        engine_kind="assistant",
        user_id="owner-1",
        assistant_id="assistant-1",
        sandbox_id="sandbox-1",
    )

    # One segment under the root, the same shape an Agent conversation's home
    # has. It was `{user_id}/{assistant_id}` — the deepest path in the product,
    # and the user segment had no reader: the user↔Assistant association lives
    # in the database. The account name is derived rather than spelled here so
    # this follows the profile's template rather than duplicating it.
    account = identity["linux_user"]
    assert identity["home_dir"] == f"/home/conversations/{account}"
    assert identity["workspace_dir"] == "/workspace"
    assert identity["workspace_source_dir"] == f"/home/conversations/{account}/workspace"
    assert identity["file_root_dir"] == "/workspace"
    assert identity["file_root_source_dir"] == identity["workspace_source_dir"]


def test_assistant_attach_maps_a_physical_cwd_back_to_the_visible_workspace() -> None:
    planner = SessionWorkspacePlanner("/unused")
    # The physical path this attach is handed must be the one the profile
    # renders, so it is derived here rather than spelled.
    account = plan_assistant_profile_identity(
        engine_kind="assistant",
        user_id="owner-1",
        assistant_id="assistant-1",
        sandbox_id="sandbox-1",
    )["linux_user"]

    fresh = planner.plan_assistant_runtime_start(
        user_id="owner-1",
        assistant_id="assistant-1",
        runtime_key="assistant-runtime",
        template=SimpleNamespace(),
        engine_kind="assistant",
    )
    attached = planner.plan_assistant_runtime_attach(
        user_id="owner-1",
        assistant_id="assistant-1",
        runtime_key="assistant-runtime",
        sandbox_id="sandbox-1",
        existing_terminal_cwd=(
            f"/home/conversations/{account}/workspace/project"
        ),
        engine_kind="assistant",
    )

    assert fresh.cwd == "/workspace"
    assert attached.cwd == "/workspace/project"


def test_configless_engine_runs_plain_sessions_and_refuses_config_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("declared_text", _DeclaredTextEngine())
    template = SimpleNamespace(
        engine_kind="declared_text",
        sandbox_tenancy="conversation",
        skills=[],
        plugin_repos=[],
        mcp_servers={},
        default_repo=None,
    )
    profile = resolve_runtime_profile(template, session_kind="agent_chat")
    identity = plan_conversation_identity(
        session_id="session-1",
        sandbox_id=None,
        agent_id="agent-1",
        runtime_profile=profile,
    )

    assert identity["config_dir"] == ""
    assert identity["config_env_var"] is None
    plan = plan_capabilities(template, identity)
    assert plan["skill_install_plan"] == []
    assert plan["plugin_repo_plan"] == []
    assert plan["mcp_plan"] == []

    template.skills = ["repo/example"]
    with pytest.raises(APIError) as raised:
        plan_capabilities(template, identity)
    assert raised.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"


def test_a_config_directory_does_not_imply_support_for_vendor_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("declared_assistant", _DeclaredAssistantEngine())
    template = SimpleNamespace(
        engine_kind="declared_assistant",
        skills=[],
        plugin_repos=[
            {"url": "https://example.invalid/plugin.git", "protocol": "https"}
        ],
        mcp_servers={},
        default_repo=None,
    )
    identity = plan_assistant_profile_identity(
        engine_kind="declared_assistant",
        user_id="owner-1",
        assistant_id="assistant-1",
        sandbox_id="sandbox-1",
    )

    with pytest.raises(APIError) as raised:
        plan_capabilities(template, identity)

    assert raised.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"
    assert "plugin_repos" in raised.value.message


def test_session_resolution_requires_all_declared_engine_identities_to_agree() -> None:
    runtime = SimpleNamespace(engine_kind="assistant")
    caps = resolve_session_capabilities(
        {"session_kind": "assistant_chat", "engine_kind": "assistant"},
        runtime=runtime,
    )
    assert caps.engine_kind == "assistant"

    with pytest.raises(ValueError, match="declarations disagree"):
        resolve_session_capabilities(
            {"session_kind": "assistant_chat", "engine_kind": "claude_code"},
            runtime=runtime,
        )

    caps = resolve_session_capabilities(
        {"session_kind": "assistant_chat", "engine_kind": "assistant"}
    )
    assert caps.engine_kind == "assistant"

    caps = resolve_session_capabilities(
        {
            "session_kind": "assistant_chat",
            "workspace_ref": {"engine_kind": "assistant"},
        }
    )
    assert caps.engine_kind == "assistant"

    with pytest.raises(ValueError, match="engine_kind is required"):
        resolve_session_capabilities({"session_kind": "agent_chat"})


@pytest.mark.parametrize("config_env_var", ["1INVALID", "HAS-DASH", "HAS SPACE"])
def test_runtime_profile_rejects_invalid_config_environment_names(
    config_env_var: str,
) -> None:
    declaration = EngineRuntimeProfileDeclaration(
        sandbox_tenancy="conversation",
        username_template="agent",
        home_template="/home/agent",
        workspace_template="/home/agent/workspace",
        config_dir_name=".example",
        config_env_var=config_env_var,
    )
    from astrabox.core.service.orchestrator.engine.capabilities import (
        validate_runtime_profile_declaration,
    )

    with pytest.raises(ValueError, match="config_env_var"):
        validate_runtime_profile_declaration("example", declaration)


def test_runtime_profile_rejects_config_environment_without_directory() -> None:
    declaration = EngineRuntimeProfileDeclaration(
        sandbox_tenancy="conversation",
        username_template="agent",
        home_template="/home/agent",
        workspace_template="/home/agent/workspace",
        config_env_var="EXAMPLE_HOME",
    )
    from astrabox.core.service.orchestrator.engine.capabilities import (
        validate_runtime_profile_declaration,
    )

    with pytest.raises(ValueError, match="requires config_dir_name"):
        validate_runtime_profile_declaration("example", declaration)

    with pytest.raises(ValueError, match="supported AstraBox product"):
        resolve_session_capabilities({})
