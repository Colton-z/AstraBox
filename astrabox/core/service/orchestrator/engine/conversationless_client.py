"""The runtime client of an Assistant runtime that hosts no conversations.

Some runtimes serve their owner through a page of their own rather than through
conversations the platform hosts (``EngineAssistantDeclaration.conversations``
is false). The platform still starts them through one hidden Session per
workspace, and that Session's runtime needs a client: this one. It is live
while the runtime it was connected to is, refuses every conversation input with
a code the console understands, and reports an idle runtime with nothing open.
An engine subclasses it to name its ``engine_kind``.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine.runtime import (
    InteractionNotOpen,
    RuntimeControlRefused,
    RuntimeCursor,
    RuntimeEvent,
    RuntimeInput,
    RuntimeInputRefused,
    RuntimeProtocolError,
    RuntimeState,
)


class ConversationlessRuntimeClient:
    """A runtime client for a runtime with no conversations to carry."""

    engine_kind: str = ""

    def __init__(self) -> None:
        if not self.engine_kind:
            raise TypeError(f"{type(self).__name__} must name its engine_kind")
        self._live = True

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def engine_session_key(self) -> str | None:
        return None

    async def submit(self, item: RuntimeInput) -> None:
        raise RuntimeInputRefused(
            "ASSISTANT_CONVERSATIONS_UNSUPPORTED",
            f"{self.engine_kind} hosts no conversations; input {item.input_id} was not delivered",
        )

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        raise RuntimeProtocolError(f"{self.engine_kind} hosts no conversations to read")
        yield  # pragma: no cover - makes this an async generator, as the contract requires

    async def state(self) -> RuntimeState:
        return RuntimeState(status="idle")

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        raise InteractionNotOpen(interaction_id)

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        raise RuntimeControlRefused(
            "ASSISTANT_CONVERSATIONS_UNSUPPORTED",
            f"{self.engine_kind} hosts no conversations to interrupt",
        )

    async def committed(self, cursor: RuntimeCursor) -> None:
        return None

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind=self.engine_kind, input_content_types=[], permission_modes=[]
        )

    async def close(self) -> None:
        self._live = False
