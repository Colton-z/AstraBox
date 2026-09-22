"""The single engine↔session-kind matrix (domain-model.md §2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.providers as providers
from astrabox.core.service.orchestrator.engine.capabilities import (
    default_engine_for_session_kind,
    engine_allowed_for_session_kind,
    require_engine_for_session_kind,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineSettledProjection,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeCapabilities,
    EngineWorkloadDeclaration,
)
from astrabox.core.service.orchestrator.engine import registry
from astrabox.core.service.orchestrator.engine.registry import register_engine_adapter
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
    SessionReadRenderingMixin,
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


@pytest.mark.parametrize(
    ("session_kind", "default"),
    [
        ("agent_chat", "claude_code"),
        ("assistant_chat", "assistant"),
    ],
)
def test_per_product_default_engine(session_kind: str | None, default: str) -> None:
    assert default_engine_for_session_kind(session_kind) == default


def test_assistant_sessions_reject_claude_code() -> None:
    assert engine_allowed_for_session_kind("claude_code", "assistant_chat") is False
    with pytest.raises(ValueError, match="not installed with support"):
        require_engine_for_session_kind("claude_code", "assistant_chat")


def test_agent_sessions_reject_the_resident_engine() -> None:
    assert engine_allowed_for_session_kind("assistant", "agent_chat") is False


def test_removed_standalone_chat_product_has_no_default() -> None:
    for invalid in (None, "", "chat"):
        with pytest.raises(ValueError, match="supported AstraBox product"):
            default_engine_for_session_kind(invalid)


def test_require_fills_the_product_default() -> None:
    assert require_engine_for_session_kind(None, "assistant_chat") == "assistant"
    assert require_engine_for_session_kind("", "agent_chat") == "claude_code"


def test_plugin_declares_agent_chat_without_core_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PluginEngine(EngineAdapter):
        @property
        def engine_kind(self) -> str:
            return "plugin_engine"

        @property
        def engine_client_type(self) -> type[_TestEngineClient]:
            return _TestEngineClient

        @property
        def capabilities(self) -> EngineRuntimeCapabilities:
            return EngineRuntimeCapabilities(
                engine_kind=self.engine_kind,
                supported_session_kinds=frozenset({"agent_chat"}),
                workload=EngineWorkloadDeclaration(required_commands=("bash",)),
            )

        def sandbox_request(self, *, template, model_access):  # pragma: no cover
            _ = (template, model_access)
            return object()

        async def activate_runtime(self, context):  # pragma: no cover
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

    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    register_engine_adapter("plugin_engine", PluginEngine())

    assert engine_allowed_for_session_kind("plugin_engine", "agent_chat") is True
    assert require_engine_for_session_kind("plugin_engine", "agent_chat") == "plugin_engine"


def test_unstamped_session_row_is_rejected_instead_of_guessing_an_engine() -> None:
    session = {"session_id": "s-1", "session_kind": "assistant_chat"}

    with pytest.raises(ValueError, match="engine_kind is required"):
        resolve_session_engine_kind(session)


def test_missing_session_kind_is_not_treated_as_an_agent() -> None:
    with pytest.raises(ValueError, match="supported AstraBox product"):
        resolve_session_engine_kind(None)


def test_session_projection_rejects_an_unstamped_engine() -> None:
    renderer = SessionReadRenderingMixin()
    renderer._session_service = SimpleNamespace(
        _sanitize_session=lambda session: dict(session)
    )
    with pytest.raises(ValueError, match="engine_kind is required"):
        renderer._render_projection_backed_session(
            {
                "session_id": "session-1",
                "session_kind": "agent_chat",
                "terminal_cwd": "/workspace",
            },
            snapshot=None,
            pending_interaction=None,
        )


@pytest.mark.parametrize(
    ("session_kind", "engine_kind"),
    [
        ("agent_chat", "claude_code"),
        ("assistant_chat", "assistant"),
    ],
)
def test_session_projection_does_not_feature_flag_the_core_fifo(
    session_kind: str,
    engine_kind: str,
) -> None:
    renderer = SessionReadRenderingMixin()
    renderer._session_service = SimpleNamespace(
        _sanitize_session=lambda session: dict(session)
    )
    renderer._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *args, **kwargs: None
    )

    rendered = renderer._render_projection_backed_session(
        {
            "session_id": "session-1",
            "session_kind": session_kind,
            "engine_kind": engine_kind,
        },
        snapshot=None,
        pending_interaction=None,
    )

    assert "accepts_active_turn_input" not in rendered
    assert rendered["engine_available"] is True


def test_session_projection_keeps_an_uninstalled_plugin_session_readable() -> None:
    renderer = SessionReadRenderingMixin()
    renderer._session_service = SimpleNamespace(
        _sanitize_session=lambda session: dict(session)
    )
    renderer._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *args, **kwargs: None
    )

    rendered = renderer._render_projection_backed_session(
        {
            "session_id": "session-1",
            "session_kind": "agent_chat",
            "engine_kind": "plugin-that-is-no-longer-installed",
        },
        snapshot=None,
        pending_interaction=None,
    )

    assert rendered["engine_kind"] == "plugin-that-is-no-longer-installed"
    assert rendered["engine_available"] is False
    assert "accepts_active_turn_input" not in rendered


def test_explicit_values_win_and_removed_product_values_fail() -> None:
    session = {"session_kind": "assistant_chat", "engine_kind": "assistant"}
    assert resolve_session_engine_kind(session) == "assistant"
    with pytest.raises(ValueError, match="supported AstraBox product"):
        resolve_session_engine_kind({"session_kind": "chat", "engine_kind": "x"})


def test_conflicting_runtime_and_durable_engine_identity_fails_loud() -> None:
    session = {
        "session_kind": "agent_chat",
        "engine_kind": "plugin_engine",
        "workspace_ref": {"engine_kind": "plugin_engine"},
    }

    with pytest.raises(ValueError, match="declarations disagree"):
        resolve_session_engine_kind(
            session,
            runtime=SimpleNamespace(engine_kind="claude_code"),
        )
