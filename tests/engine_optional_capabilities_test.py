"""Every engine completes the core journey without inventing optional features."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineTurnReceipt,
    initialize_engine_client,
    validate_engine_client_manifest,
)
from astrabox.core.service.orchestrator.engine import registry
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeCapabilities,
)


def _minimal_capabilities(
    *, permission_modes: tuple[str, ...] = ()
) -> EngineRuntimeCapabilities:
    return EngineRuntimeCapabilities(
        engine_kind="minimal_engine",
        supported_session_kinds=frozenset({"agent_chat"}),
        permission_modes=permission_modes,
    )


@pytest.fixture(autouse=True)
def _register_minimal_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        registry._REGISTRY,
        "minimal_engine",
        SimpleNamespace(capabilities=_minimal_capabilities()),
    )


class _CompleteJourneyClient:
    """The minimum Agent contract: resume, FIFO, stream, stop and close."""

    def __init__(self) -> None:
        self._engine_session_key: str | None = None

    @property
    def is_live(self) -> bool:
        return True

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    async def bind_conversation(
        self,
        binding: EngineConversationBinding,
    ) -> None:
        self._engine_session_key = binding.engine_session_key

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
            engine_session_key="session-1",
            started_at_monotonic_ns=1,
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )

    async def _events(self) -> AsyncIterator[dict]:
        yield {"type": "text-delta", "id": "text-1", "delta": "hello"}
        yield {"type": "finish", "finishReason": "stop"}

    def iter_turn_events(self, receipt: EngineTurnReceipt) -> AsyncIterator[dict]:
        _ = receipt
        return self._events()

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        _ = receipt
        return True

    async def interrupt_active_turn(self) -> bool:
        return True

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(engine_kind="text_only")

    async def close(self) -> None:
        return None


def test_a_complete_agent_needs_no_optional_vendor_control_methods() -> None:
    client = _CompleteJourneyClient()
    validate_engine_client_manifest(
        client,
        EngineCapabilityManifest(engine_kind="text_only"),
    )


@pytest.mark.parametrize(
    ("input_content_types", "message"),
    [
        ([], "must accept text input"),
        (["text", "text"], "input_content_types must be unique"),
        (["text", "audio"], "unknown input content types"),
    ],
)
def test_an_invalid_input_content_declaration_fails_the_runtime_handshake(
    input_content_types: list[str],
    message: str,
) -> None:
    with pytest.raises(TypeError, match=message):
        validate_engine_client_manifest(
            _CompleteJourneyClient(),
            EngineCapabilityManifest(
                engine_kind="text_only",
                input_content_types=input_content_types,
            ),
        )


@pytest.mark.parametrize(
    "manifest",
    [
        EngineCapabilityManifest(engine_kind="text_only", supports_interaction=True),
        EngineCapabilityManifest(engine_kind="text_only", permission_modes=["strict"]),
        EngineCapabilityManifest(engine_kind="text_only", supports_server_info=True),
        EngineCapabilityManifest(engine_kind="text_only", supports_child_run_control=True),
    ],
)
def test_claiming_an_unimplemented_optional_capability_fails_loud(
    manifest: EngineCapabilityManifest,
) -> None:
    with pytest.raises(TypeError, match="claims unsupported client capabilities"):
        validate_engine_client_manifest(_CompleteJourneyClient(), manifest)


class _MinimalDeclaredClient(_CompleteJourneyClient):
    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(engine_kind="minimal_engine")


@pytest.mark.asyncio
async def test_runtime_handshake_binds_resume_identity_before_manifest_read() -> None:
    calls: list[str] = []

    class OrderedClient(_MinimalDeclaredClient):
        async def bind_conversation(
            self, binding: EngineConversationBinding
        ) -> None:
            assert binding == EngineConversationBinding(
                platform_session_id="session-1",
                engine_session_key="native-session-7",
            )
            await super().bind_conversation(binding)
            calls.append("bind")

        async def get_capabilities(self) -> EngineCapabilityManifest:
            calls.append("manifest")
            return await super().get_capabilities()

    await initialize_engine_client(
        OrderedClient(),
        expected_engine_kind="minimal_engine",
        conversation_binding=EngineConversationBinding(
            platform_session_id="session-1",
            engine_session_key="native-session-7",
        ),
    )

    assert calls == ["bind", "manifest"]


@pytest.mark.asyncio
async def test_runtime_handshake_accepts_additive_permission_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExtendedClient(_MinimalDeclaredClient):
        async def get_capabilities(self) -> EngineCapabilityManifest:
            return EngineCapabilityManifest(
                engine_kind="minimal_engine",
                permission_modes=["declared", "runtime-only"],
            )

        async def set_permission_mode(self, mode: str) -> None:
            _ = mode

    monkeypatch.setitem(
        registry._REGISTRY,
        "minimal_engine",
        SimpleNamespace(
            capabilities=_minimal_capabilities(permission_modes=("declared",))
        ),
    )

    manifest = await initialize_engine_client(
        ExtendedClient(),
        expected_engine_kind="minimal_engine",
        conversation_binding=EngineConversationBinding(
            platform_session_id="session-1"
        ),
    )

    assert manifest.permission_modes == ["declared", "runtime-only"]


@pytest.mark.asyncio
async def test_runtime_handshake_rejects_a_missing_required_permission_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRequiredClient(_MinimalDeclaredClient):
        async def get_capabilities(self) -> EngineCapabilityManifest:
            return EngineCapabilityManifest(
                engine_kind="minimal_engine",
                permission_modes=["runtime-only"],
            )

        async def set_permission_mode(self, mode: str) -> None:
            _ = mode

    monkeypatch.setitem(
        registry._REGISTRY,
        "minimal_engine",
        SimpleNamespace(
            capabilities=_minimal_capabilities(permission_modes=("declared",))
        ),
    )

    with pytest.raises(TypeError, match="missing required permission modes"):
        await initialize_engine_client(
            MissingRequiredClient(),
            expected_engine_kind="minimal_engine",
            conversation_binding=EngineConversationBinding(
                platform_session_id="session-1"
            ),
        )


@pytest.mark.asyncio
async def test_runtime_publish_rejects_a_client_that_silently_starts_fresh() -> None:
    class WrongConversationClient(_MinimalDeclaredClient):
        async def bind_conversation(
            self, binding: EngineConversationBinding
        ) -> None:
            _ = binding
            self._engine_session_key = "different-native-session"

    with pytest.raises(
        RuntimeError, match="did not resume the requested native conversation"
    ):
        await initialize_engine_client(
            WrongConversationClient(),
            expected_engine_kind="minimal_engine",
            conversation_binding=EngineConversationBinding(
                platform_session_id="session-1",
                engine_session_key="native-session-7",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_member",
    [
        "is_live",
        "engine_session_key",
        "bind_conversation",
        "deliver",
        "begin_delivery",
        "iter_turn_events",
        "cancel_turn",
        "interrupt_active_turn",
        "get_capabilities",
        "close",
    ],
)
async def test_runtime_publish_rejects_every_incomplete_core_journey(
    missing_member: str,
) -> None:
    complete = _MinimalDeclaredClient()
    surface = {
        "is_live": True,
        "engine_session_key": complete.engine_session_key,
        "bind_conversation": complete.bind_conversation,
        "deliver": complete.deliver,
        "begin_delivery": complete.begin_delivery,
        "iter_turn_events": complete.iter_turn_events,
        "cancel_turn": complete.cancel_turn,
        "interrupt_active_turn": complete.interrupt_active_turn,
        "get_capabilities": complete.get_capabilities,
        "close": complete.close,
    }
    surface.pop(missing_member)

    with pytest.raises(TypeError, match="mandatory conversation and turn surface"):
        await initialize_engine_client(
            SimpleNamespace(**surface),
            expected_engine_kind="minimal_engine",
            conversation_binding=EngineConversationBinding(
                platform_session_id="session-1"
            ),
        )


@pytest.mark.asyncio
async def test_runtime_publish_does_not_require_impossible_process_death_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry,
        "_REGISTRY",
        {
            "minimal_engine": SimpleNamespace(capabilities=_minimal_capabilities())
        },
    )

    manifest = await initialize_engine_client(
        _MinimalDeclaredClient(),
        expected_engine_kind="minimal_engine",
        conversation_binding=EngineConversationBinding(
            platform_session_id="session-1"
        ),
    )
    assert manifest.engine_kind == "minimal_engine"


@pytest.mark.asyncio
async def test_runtime_publish_rejects_a_dead_client_after_resume_binding() -> None:
    class DeadClient(_MinimalDeclaredClient):
        @property
        def is_live(self) -> bool:
            return False

    with pytest.raises(RuntimeError, match="unavailable during conversation binding"):
        await initialize_engine_client(
            DeadClient(),
            expected_engine_kind="minimal_engine",
            conversation_binding=EngineConversationBinding(
                platform_session_id="session-1"
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        EngineConversationBinding(platform_session_id=""),
        EngineConversationBinding(platform_session_id=" session-1"),
        EngineConversationBinding(
            platform_session_id="session-1",
            engine_session_key="",
        ),
        EngineConversationBinding(
            platform_session_id="session-1",
            engine_session_key=" native-session ",
        ),
    ],
)
async def test_runtime_publish_rejects_ambiguous_conversation_identity(
    binding: EngineConversationBinding,
) -> None:
    with pytest.raises(ValueError):
        await initialize_engine_client(
            _MinimalDeclaredClient(),
            expected_engine_kind="minimal_engine",
            conversation_binding=binding,
        )
