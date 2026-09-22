"""Optional vendor process-disposal capability for engine adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

@runtime_checkable
class EngineProcessDisposalCapability(Protocol):
    """Optional finalizer for a resident engine process identified durably.

    Normal runtime eviction only disconnects a client so another platform
    process can resume it. Delete/archive/end may happen after that platform
    process restarted, so an engine that owns a resident process can also
    dispose it from the opaque turn id saved in the journal.
    """

    async def dispose_process(
        self,
        *,
        sandbox_id: str,
        engine_turn_id: str,
    ) -> None:
        ...
