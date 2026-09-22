"""Runtime data models used by the runtime manager.

NOTE: Do NOT add `from __future__ import annotations` to this file.
It triggers a dataclass crash under certain import setups.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest

@dataclass
class SessionRuntime:
    """Per-session in-memory runtime handle."""

    session_id: str
    agent: Any
    engine_kind: str
    user_id: Optional[str] = None
    terminal_cwd: Optional[str] = None
    sandbox: Any = None
    sandbox_id: Optional[str] = None
    engine_session_key: Optional[str] = None
    permission_mode: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_task: Optional[asyncio.Task] = None
    current_execution_id: Optional[str] = None
    interrupting: bool = False
    engine_client: Any = None
    # Bound once, before the runtime enters the live map. Engine controls and
    # recovery read this copy instead of querying a possibly broken transport.
    engine_manifest: Optional["EngineCapabilityManifest"] = None
    # Set only after EngineClient.bind_conversation has proved this
    # process-local runtime adopted the durable engine conversation.
    conversation_bound: bool = False
    runtime_identity: Optional[dict[str, Any]] = None
    # Host-side preparation that must finish before a new root input reaches
    # the resident engine. Protected MCP credentials use it to refresh the
    # process-local egress Vault after rotation without entering engine config.
    prepare_engine_input: Optional[Callable[[], Awaitable[None]]] = None
    #: Under the shared-sandbox tenancy, the isolated session this conversation
    #: runs in. ``sandbox_id`` stays the box — every lifecycle operation
    #: addresses the box, and many conversations share one — so this is what
    #: teardown closes and what a reattach rebuilds from. None on the
    #: per-session tenancy, where the box is itself the conversation's context.
    isolated_session_id: Optional[str] = None
    # Tracks this sandbox's current lease expiry (set on lazy renew). The owner
    # replica renews the lease on turn activity only when the remaining lease drops
    # below the renew threshold; an abandoned session stops renewing and the sandbox
    # auto-expires ~one lease after the last turn. None means "renew on next activity".
    sandbox_lease_expires_at: Optional[datetime] = None
    owner_loop: Optional[asyncio.AbstractEventLoop] = None

    def __post_init__(self) -> None:
        # engine_client is created on the loop that's running this
        # constructor; pin that loop so later close paths can dispatch back
        # to it via run_coroutine_threadsafe instead of crashing on a
        # different loop.
        if self.owner_loop is None:
            try:
                self.owner_loop = asyncio.get_running_loop()
            except RuntimeError:
                self.owner_loop = None
