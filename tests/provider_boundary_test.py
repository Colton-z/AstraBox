"""Provider vocabulary must not leak across the core seam boundary."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
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

_ROOT = Path(__file__).resolve().parents[1]
_GENERIC_TARGETS = (
    _ROOT / "astrabox" / "core",
    _ROOT / "astrabox" / "seams",
    _ROOT / "astrabox" / "api",
    _ROOT / "astrabox" / "common",
    _ROOT / "astrabox" / "identity",
    _ROOT / "astrabox" / "observability",
    _ROOT / "astrabox" / "persistence",
    _ROOT / "astrabox" / "web",
    _ROOT / "astrabox" / "bootstrap.py",
    _ROOT / "frontend" / "src",
)
_PROVIDER_PROTOCOL_TERMS = (
    "litellm",
    "proxy_admin",
    "virtual_key",
    "virtual key",
    "x-litellm",
    "allowed_routes",
    "gateway_auth",
    "litellm_server_id",
)


def _source_files(target: Path):
    if target.is_file():
        yield target
    else:
        yield from (
            path
            for path in target.rglob("*")
            if path.suffix in {".css", ".json", ".py", ".ts", ".tsx"}
        )


def test_provider_protocol_vocabulary_stays_in_adapters() -> None:
    leaks: list[str] = []
    for target in _GENERIC_TARGETS:
        for path in _source_files(target):
            text = path.read_text(encoding="utf-8").casefold()
            for term in _PROVIDER_PROTOCOL_TERMS:
                # Product copy names its supplier; wire vocabulary remains adapter-owned.
                if term == "litellm" and path.is_relative_to(
                    _ROOT / "frontend/src/i18n/locales"
                ):
                    continue
                if term in text:
                    leaks.append(f"{path.relative_to(_ROOT)}: {term}")

    assert leaks == [], (
        "provider-specific names and wire fields belong under astrabox/providers, "
        "deploy, or containers:\n" + "\n".join(leaks)
    )


def test_engine_sdk_imports_stay_inside_the_engine_implementation() -> None:
    engine_root = _ROOT / "astrabox/core/service/orchestrator/engine"
    # This is the Claude image's in-box entrypoint, not host platform code.
    in_box_runner = _ROOT / "astrabox/core/service/orchestrator/sandbox_runner.py"
    offenders: list[str] = []
    for path in (_ROOT / "astrabox").rglob("*.py"):
        if path == in_box_runner or path.is_relative_to(engine_root):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...]
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
            else:
                continue
            if any(
                module == "claude_agent_sdk"
                or module.startswith("claude_agent_sdk.")
                for module in modules
            ):
                offenders.append(str(path.relative_to(_ROOT)))
                break

    assert offenders == [], (
        "vendor SDK types belong to the Claude adapter or its in-box runner:\n"
        + "\n".join(offenders)
    )


def test_host_code_does_not_import_the_standalone_in_box_runner() -> None:
    runner_module = "astrabox.core.service.orchestrator.sandbox_runner"
    runner_path = _ROOT / "astrabox/core/service/orchestrator/sandbox_runner.py"
    offenders: list[str] = []
    for path in (_ROOT / "astrabox").rglob("*.py"):
        if path == runner_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imports_runner = False
            if isinstance(node, ast.Import):
                imports_runner = any(
                    alias.name == runner_module for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                imports_runner = node.module == runner_module or (
                    node.module == "astrabox.core.service.orchestrator"
                    and any(alias.name == "sandbox_runner" for alias in node.names)
                )
            if imports_runner:
                offenders.append(str(path.relative_to(_ROOT)))
                break

    assert offenders == [], (
        "sandbox_runner.py is copied standalone into the Claude image; host code "
        "must use adapter contracts instead of importing it:\n" + "\n".join(offenders)
    )


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
