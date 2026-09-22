"""Agent program adapter abstraction.

Each Agent program integrates with AstraBox through the platform-side
``EngineAdapter``.

The adapter translates its native protocol into typed engine emissions. Public
UI frames keep the AI SDK's open extension shape; input, interaction, child
resource, terminal and diagnostic facts use closed envelope types. The
platform owns ordering, persistence and sandbox lifecycle without interpreting
an Agent program's native status or event vocabulary.

Adapters available:
- ``claude_code`` drives Claude Code through the Agent SDK runner link.
- ``assistant`` drives Hermes through its TUI Gateway protocol.
- ``deepseek_harness`` drives DSH through its host API and event mux.
- ``pi`` drives pi through its RPC mode over the sandbox's execd pipe.

Additional Agent programs can be registered by a third-party plugin at the same
registry.

Selection: workspace.engine_kind in the session/assistant_workspace doc.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineCapabilityManifest,
    EngineClient,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.registry import (
    get_engine_adapter,
    register_engine_adapter,
)

# Built-in engines register through the same two mechanisms as every other
# seam: ``register_builtin_providers()`` (the in-tree fallback,
# astrabox/providers/__init__.py) imports the adapter modules in-proc, and
# ``load_entry_point_providers()`` loads them by the
# ``astrabox.providers.engine`` entry-points at app bootstrap. An unregistered
# engine kind fails loud in ``get_engine_adapter(...)``.

__all__ = [
    "EngineAdapter",
    "EngineCapabilityManifest",
    "EngineClient",
    "EngineTurnReceipt",
    "get_engine_adapter",
    "register_engine_adapter",
]
