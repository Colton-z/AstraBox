"""EngineAdapter registry — engine_kind → adapter singleton.

Adapters self-register at import time:

    from astrabox.core.service.orchestrator.engine.registry import register_engine_adapter
    register_engine_adapter("claude_code", ClaudeCodeEngineAdapter())

Lookup raises EngineKindNotRegistered for unknown engines so the platform
fails loud rather than silently falling back to a default — engine selection
is an explicit invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

_REGISTRY: dict[str, "EngineAdapter"] = {}


class EngineKindNotRegistered(KeyError):
    """Raised when get_engine_adapter is called with an unregistered engine_kind."""


def _validate_assistant_obligations(engine_kind: str, adapter: "EngineAdapter") -> None:
    """An Assistant runtime implements what its declaration obliges.

    The platform releases an Assistant's box only after the adapter has
    stopped its writers and saved its state, and it opens a declared panel
    through the adapter's own login; an adapter that declares either without
    implementing it would fail at the first hibernation or the first open.
    """
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

    surface = adapter.capabilities.assistant
    if surface is None:
        return
    implementation = type(adapter)
    if implementation.quiesce_and_save_runtime_state is EngineAdapter.quiesce_and_save_runtime_state:
        raise ValueError(
            f"engine_kind={engine_kind!r} serves Assistants without implementing "
            "quiesce_and_save_runtime_state"
        )
    if (
        surface.panel is not None
        and implementation.panel_launch_path is EngineAdapter.panel_launch_path
    ):
        raise ValueError(
            f"engine_kind={engine_kind!r} declares an Assistant panel without "
            "implementing panel_launch_path"
        )


def register_engine_adapter(engine_kind: str, adapter: "EngineAdapter") -> None:
    """Register an adapter for the given engine_kind.

    Re-registering the same kind raises ValueError — adapters must be
    singletons and registration must be deterministic.
    """
    normalized_kind = str(engine_kind or "").strip()
    from astrabox.core.service.orchestrator.engine.capabilities import (
        validate_engine_capabilities,
    )
    from astrabox.core.service.orchestrator.engine.base import (
        validate_engine_client_type,
    )
    validate_engine_capabilities(normalized_kind, adapter.capabilities)
    validate_engine_client_type(adapter.engine_client_type)
    _validate_assistant_obligations(normalized_kind, adapter)
    if normalized_kind in _REGISTRY:
        existing = type(_REGISTRY[normalized_kind]).__name__
        new = type(adapter).__name__
        if existing != new:
            raise ValueError(
                f"engine_kind={normalized_kind!r} already registered to {existing}; "
                f"cannot re-register as {new}"
            )
        return
    _REGISTRY[normalized_kind] = adapter


def get_engine_adapter(engine_kind: str) -> "EngineAdapter":
    """Look up an adapter by engine_kind.

    Raises EngineKindNotRegistered if the kind has no registered adapter.
    """
    try:
        return _REGISTRY[engine_kind]
    except KeyError as exc:
        registered = sorted(_REGISTRY.keys())
        raise EngineKindNotRegistered(
            f"engine_kind={engine_kind!r} not registered; available={registered}"
        ) from exc


def known_engine_kinds() -> list[str]:
    """Return the list of currently registered engine_kinds (test/diag use)."""
    return sorted(_REGISTRY.keys())


def registered_engine_adapters() -> dict[str, "EngineAdapter"]:
    """Snapshot of installed adapters for capability-based discovery."""
    return dict(_REGISTRY)


def _reset_for_tests() -> None:
    """Test-only: clear the registry. Do NOT call from production code."""
    _REGISTRY.clear()
