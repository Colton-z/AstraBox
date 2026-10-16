"""Optional vendor process-disposal capability for engine adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

@runtime_checkable
class EngineProcessDisposalCapability(Protocol):
    """Optional finalizer for a resident engine process identified durably.

    Normal runtime eviction only disconnects a client so another platform
    process can resume it. Delete/archive/end may happen after that platform
    process restarted, so an engine that owns a resident process can also
    dispose it from the native conversation identity and recorded reader cursor.
    """

    async def dispose_process(
        self,
        *,
        sandbox_id: str,
        engine_session_key: str,
        runtime_cursor: dict | None,
        workspace_ref: dict,
    ) -> None:
        ...
