"""Provider vocabulary must not leak across the core seam boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

import astrabox.providers as providers
import astrabox.seams.extensions as extension_seam
import astrabox.seams.model as model_seam
from astrabox.core.service.orchestrator.engine import registry as engine_registry
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineWorkloadDeclaration,
    EngineRuntimeCapabilities,
)
from astrabox.seams.extensions import ExtensionCatalog, ExtensionRuntimeSelection
from astrabox.seams.model import ModelEndpoint


class _EntryPoint:
    name = "external"
    value = "external.package:Provider"

    def __init__(self, target: type[Any]) -> None:
        self._target = target

    def load(self) -> type[Any]:
        return self._target


class _ExternalExtensionProvider:
    name = "external"

    async def list_catalog(self) -> ExtensionCatalog:
        return ExtensionCatalog()

    def materialize(self, **_kwargs: Any) -> ExtensionRuntimeSelection:
        return ExtensionRuntimeSelection()


class _ExternalModelProvider:
    name = "external"

    def resolve(
        self,
        *,
        requested: ModelEndpoint,
        settings: Any = None,
    ) -> ModelEndpoint:
        _ = settings
        return requested


class _ExternalEngineClient:
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


class _ExternalEngine(EngineAdapter):
    @property
    def engine_kind(self) -> str:
        return "external"

    @property
    def engine_client_type(self) -> type[_ExternalEngineClient]:
        return _ExternalEngineClient

    @property
    def capabilities(self) -> EngineRuntimeCapabilities:
        return EngineRuntimeCapabilities(
            engine_kind="external",
            supported_session_kinds=frozenset({"agent_chat"}),
            workload=EngineWorkloadDeclaration(required_commands=("bash",)),
        )

    def sandbox_request(self, *, template: Any, model_access: Any) -> Any:
        _ = (template, model_access)
        return object()

    async def activate_runtime(self, context: Any) -> Any:
        _ = context
        raise NotImplementedError

    def slice_recovery_turn(self, raw_items, *, prompt_text):
        _ = prompt_text
        return list(raw_items)

    def has_transcript_terminal_evidence(self, raw_items) -> bool:
        return bool(raw_items)

    def project_settled_transcript(self, raw_items, *, done=False):
        from astrabox.core.service.orchestrator.engine.base import EngineSettledProjection

        _ = raw_items
        return EngineSettledProjection(
            blocks=[], assistant_text=None, completed=done, has_result=False
        )


@pytest.mark.parametrize(
    ("group", "target", "registry"),
    [
        (
            providers.EXTENSIONS_GROUP,
            _ExternalExtensionProvider,
            extension_seam.registered_extension_provider_names,
        ),
        (
            providers.MODEL_GROUP,
            _ExternalModelProvider,
            model_seam.registered_model_endpoint_names,
        ),
    ],
)
def test_class_entry_points_are_registered_without_import_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    target: type[Any],
    registry: Any,
) -> None:
    monkeypatch.setattr(providers, "_EAGER_GROUPS", (group,))
    monkeypatch.setattr(
        providers,
        "_select_entry_points",
        lambda selected_group: (
            {"external": _EntryPoint(target)} if selected_group == group else {}
        ),
    )
    monkeypatch.setattr(extension_seam, "_PROVIDERS", {})
    monkeypatch.setattr(extension_seam, "_DEFAULT_PROVIDER", None)
    monkeypatch.setattr(model_seam, "_PROVIDERS", {})
    monkeypatch.setattr(model_seam, "_DEFAULT_PROVIDER", None)

    providers.load_entry_point_providers()

    assert registry() == ["external"]


def test_engine_entry_point_registers_adapter_without_import_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "_EAGER_GROUPS", (providers.ENGINE_GROUP,))
    monkeypatch.setattr(
        providers,
        "_select_entry_points",
        lambda group: (
            {"external": _EntryPoint(_ExternalEngine)}
            if group == providers.ENGINE_GROUP
            else {}
        ),
    )
    monkeypatch.setattr(engine_registry, "_REGISTRY", {})

    providers.load_entry_point_providers()

    adapter = engine_registry.get_engine_adapter("external")
    assert adapter.capabilities.supported_session_kinds == frozenset({"agent_chat"})
