"""One resident Hermes TUI Gateway process per (sandbox, Assistant profile).

The pinned ``hermes-agent`` gateway is a native multi-session runtime:
``session.create`` allocates ids into a process-wide registry, every RPC and
event is ``session_id``-scoped, and each turn runs on its own per-session
thread. That is the same class of vendor seam as Codex ``thread/start`` and
Copilot ``session.create`` in the prepared-runtime audit
(``docs/maintainers/claude-runtime-preparation.md``), so Hermes' prepared unit
is legitimately multi-use: one gateway process per (user, assistant) profile,
activated per conversation through ``session.create`` / ``session.resume``.
The engine-specific decision record is
``docs/maintainers/hermes-runtime-preparation.md``.

This module owns host-side residency for that process:

* :class:`HermesGatewayHandle` holds exactly one execd pipe attachment
  (:class:`~.hermes_client.HermesTuiProcess`) and one pump task that fans wire
  events out to per-``tui_session_id`` subscriptions, so every conversation
  client is a session-scoped consumer and every RPC write is serialized
  through the one attachment.
* :func:`resolve_gateway_handle` obtains the handle: within one host process
  an asyncio lock admits exactly one attacher per (sandbox, profile), so the
  pump and its subscriptions are built once.

There is nothing to elect across hosts. The backend is `hermes serve`, a
service of the image supervised inside the box, so no host owns it and every
host simply connects — where a PTY-era resolution had to arbitrate which host
had spawned the one true gateway, publish that under a compare-and-set, probe
a recorded PTY to tell dead from live, and destroy the loser's spawn. All of
that is deleted; see docs/maintainers/hermes-transport.md for why the
transport moved and what it cost.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable, Protocol

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiProcess,
    HermesTuiRpcError,
    HermesTuiWireEvent,
    _RPC_TIMEOUT_SECONDS,
)
logger = get_logger(__name__)

#: How long an attach may spend waiting for a backend that is still starting.
#: It covers the box-side chain a first materialization releases — profile
#: written, Hermes composing its profile, forwarder waiting for an answer —
#: and no more: past it, a backend that is not listening is a broken box.
_ATTACH_BUDGET_SECONDS = 120.0
_ATTACH_RETRY_SECONDS = 1.0

#: Builds one attachment to the resident backend. The default is the real
#: :class:`HermesTuiProcess`; tests substitute a fake wire.
GatewayProcessFactory = Callable[..., HermesTuiProcess]


class HermesGatewaySubscription:
    """One conversation's ordered view of the shared gateway event stream.

    Offsets are byte positions in the gateway's single stdout stream, so they
    are strictly increasing across the whole process lifetime. The
    subscription drops any event at or below the last offset it delivered,
    which makes an execd replay (a handle-level reconnect at an earlier
    cursor) transparent to consumers that already saw those bytes.
    """

    def __init__(self, *, tui_session_id: str, after_offset: int = 0) -> None:
        self.tui_session_id = str(tui_session_id)
        self._queue: asyncio.Queue[HermesTuiWireEvent | BaseException] = (
            asyncio.Queue()
        )
        self._last_offset = int(after_offset)

    def _offer(self, item: HermesTuiWireEvent | BaseException) -> None:
        if isinstance(item, HermesTuiWireEvent):
            if item.output_offset <= self._last_offset:
                return
            self._last_offset = item.output_offset
        self._queue.put_nowait(item)

    async def next_event(self) -> HermesTuiWireEvent:
        item = await self._queue.get()
        if isinstance(item, BaseException):
            # A terminal failure stays terminal: re-queue it so a later read
            # raises the same error instead of waiting forever.
            self._queue.put_nowait(item)
            raise item
        return item


class HermesGatewayHandle:
    """Ownership of one resident gateway process for one (sandbox, profile).

    All conversation clients of the profile share this handle: RPC writes are
    serialized by the underlying channel's send lock, and wire events reach
    each client through its own :class:`HermesGatewaySubscription`. The handle
    never decides what an event means — routing is by the vendor's own
    ``session_id`` field, and events without one (process-level notices such
    as ``gateway.stderr``) are broadcast to every subscription so each
    adapter loop can classify them exactly as it would on a private process.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        dial: tuple[str, int] | None = None,
        sandbox_id: str,
        profile_key: str,
        spawn_fingerprint: str = "",
        process_factory: GatewayProcessFactory | None = None,
    ) -> None:
        self._url = str(url)
        self._headers = dict(headers or {})
        self._dial = dial
        self.sandbox_id = str(sandbox_id)
        self.profile_key = str(profile_key)
        self._spawn_fingerprint = str(spawn_fingerprint or "")
        self._process_factory: GatewayProcessFactory = (
            process_factory if process_factory is not None else HermesTuiProcess
        )
        self._process: HermesTuiProcess | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._subscriptions: dict[str, list[HermesGatewaySubscription]] = {}
        self._lock = asyncio.Lock()
        self._ever_answered = False

    # ── identity and liveness ────────────────────────────────────────────
    @property
    def spawn_fingerprint(self) -> str:
        return self._spawn_fingerprint

    @property
    def is_live(self) -> bool:
        process = self._process
        return (
            process is not None
            and process.is_connected
            and process.fatal is None
        )

    @property
    def ever_answered(self) -> bool:
        """Whether the backend behind this attachment answered one RPC.

        An RPC *error* reply counts: it proves the vendor's request loop is
        alive, which is the distinction worth keeping — an attachment that
        connected and never got an answer is a wedged backend, not a healthy
        one this host merely lost.
        """

        return self._ever_answered

    # ── attachment lifecycle ─────────────────────────────────────────────
    async def start(self, *, require_gateway_ready: bool) -> None:
        """Attach this handle to the box's resident backend.

        There is nothing to spawn: `hermes serve` is a service of the image,
        supervised there, and this only opens a socket to it. A connected
        socket proves the backend is listening; ``gateway.ready`` is Hermes'
        own statement that it can answer, which a fresh attachment waits for.

        On first workspace materialization, writing the profile env file
        releases `astrabox-hermes-serve` to start Hermes.
        `astrabox-hermes-forward` publishes the port after the backend answers.
        An immediate attachment was observed to return ``Connection refused``
        during that startup interval, so connection attempts retry within
        ``_ATTACH_BUDGET_SECONDS`` and propagate the error when it expires.

        On resume with a persistent profile, startup does not wait for the host
        to write that file; the backend starts with the box.

        There is no byte cursor to attach at, so recovery resumes live rather
        than replaying the suffix a departed host missed. The gap is stated at
        its caller in `hermes_client` and in docs/maintainers/hermes-transport.md.
        """

        async with self._lock:
            if self.is_live:
                return
            deadline = time.monotonic() + _ATTACH_BUDGET_SECONDS
            attempt = 0
            while True:
                attempt += 1
                process = self._process_factory(
                    url=self._url, headers=self._headers, dial=self._dial
                )
                try:
                    await process.connect(require_gateway_ready=require_gateway_ready)
                except BaseException as exc:
                    if time.monotonic() >= deadline:
                        raise
                    logger.info(
                        "resident Hermes backend not listening yet; retrying: "
                        "sandbox=%s profile=%s attempt=%d err=%s",
                        self.sandbox_id,
                        self.profile_key,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(_ATTACH_RETRY_SECONDS)
                    continue
                self._install_process(process)
                return

    async def shutdown(self) -> None:
        """Release this attachment. The backend keeps running.

        Detaching is all this handle can do. The backend is supervised by the
        image, so neither destroying it nor keeping it alive is a decision
        available here — a profile whose configuration changed is restarted by
        supervisord, not from this side of the connection.

        Subscribers are handed a terminal detach: their sessions cannot be
        carried by this handle any more.
        """

        async with self._lock:
            await self._stop_pump()
            process, self._process = self._process, None
            if process is not None:
                with contextlib.suppress(BaseException):
                    await process.detach()
            self._broadcast(
                EngineStreamDetached(
                    "Hermes gateway attachment was shut down for profile "
                    f"{self.profile_key}"
                )
            )

    # ── RPC and event fan-out ────────────────────────────────────────────
    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        process = self._process
        if process is None or not process.is_connected:
            raise EngineStreamDetached(
                f"Hermes gateway for profile {self.profile_key} is not attached"
            )
        try:
            result = await process.request(method, params, timeout=timeout)
        except HermesTuiRpcError:
            self._ever_answered = True
            raise
        self._ever_answered = True
        return result

    def subscribe(
        self, tui_session_id: str, *, after_offset: int = 0
    ) -> HermesGatewaySubscription:
        subscription = HermesGatewaySubscription(
            tui_session_id=tui_session_id, after_offset=after_offset
        )
        self._subscriptions.setdefault(subscription.tui_session_id, []).append(
            subscription
        )
        process = self._process
        if process is not None and process.fatal is not None:
            # A subscriber arriving after the failure must still learn it.
            subscription._offer(process.fatal)
        return subscription

    def unsubscribe(self, subscription: HermesGatewaySubscription) -> None:
        entries = self._subscriptions.get(subscription.tui_session_id)
        if not entries:
            return
        with contextlib.suppress(ValueError):
            entries.remove(subscription)
        if not entries:
            self._subscriptions.pop(subscription.tui_session_id, None)

    # ── internals ────────────────────────────────────────────────────────
    def _install_process(self, process: HermesTuiProcess) -> None:
        self._process = process
        self._pump_task = asyncio.create_task(
            self._pump(process),
            name=f"hermes-gateway-pump:{self.sandbox_id}:{self.profile_key}",
        )

    async def _stop_pump(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def _pump(self, process: HermesTuiProcess) -> None:
        try:
            while True:
                self._route(await process.next_event())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._process is process:
                # A failure of a superseded attachment (replay swap already
                # detached it) is a consequence, not news for subscribers.
                self._broadcast(exc)

    def _route(self, wire: HermesTuiWireEvent) -> None:
        scoped_session_id = str(wire.event.get("session_id") or "").strip()
        if scoped_session_id:
            for subscription in tuple(
                self._subscriptions.get(scoped_session_id, ())
            ):
                subscription._offer(wire)
            return
        for entries in tuple(self._subscriptions.values()):
            for subscription in tuple(entries):
                subscription._offer(wire)

    def _broadcast(self, exc: BaseException) -> None:
        for entries in tuple(self._subscriptions.values()):
            for subscription in tuple(entries):
                subscription._offer(exc)


# One handle per (sandbox_id, profile_key) in this host process. The dict is
# module state on purpose: the resident gateway outlives every conversation
# runtime, so no SessionRuntime can own it.
_HANDLES: dict[tuple[str, str], HermesGatewayHandle] = {}
_RESOLVE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def reset_gateway_registry() -> None:
    """Forget every handle without touching sandboxes. Test isolation only."""

    _HANDLES.clear()
    _RESOLVE_LOCKS.clear()


def gateway_handle_for_sandbox(sandbox_id: str) -> HermesGatewayHandle | None:
    """This host's live attachment to a box's backend, if it has one.

    Keyed by sandbox alone because a box carries one Assistant workspace —
    `astrabox-hermes-serve` refuses to start against a second profile rather
    than guess — so the (sandbox, profile) registry has at most one live entry
    per box. Callers that hold only an opaque turn id use this to ride an
    existing attachment instead of opening one for a single RPC.
    """

    target = str(sandbox_id or "").strip()
    if not target:
        return None
    for (handle_sandbox, _profile), handle in _HANDLES.items():
        if handle_sandbox == target and handle.is_live:
            return handle
    return None


async def resolve_gateway_handle(
    *,
    url: str,
    headers: dict[str, str] | None,
    dial: tuple[str, int] | None = None,
    sandbox_id: str,
    profile_key: str,
    spawn_fingerprint: str | None,
    process_factory: GatewayProcessFactory | None = None,
) -> HermesGatewayHandle:
    """Return this host's attachment to the box's resident Hermes backend.

    Resolving it elects nothing. The backend is a service of the image,
    supervised there and owned by no host, so there is no ownership to
    arbitrate — no durable record of a winner, no compare-and-set to keep two
    hosts from both spawning one, no probe to tell a dead record from a live
    process, and no destroy for a loser. Every host simply connects.

    What this does hold is the per-host handle registry, because the pump and
    its subscriptions are per-host state, and the configuration fence:
    ``spawn_fingerprint`` is the identity of the profile the backend is
    serving, and an attachment established under different content must not
    keep gaining sessions under the new one. Restarting the backend so it
    re-reads a changed profile happens where the change is written
    (`_prepare_hermes_profile`), because Hermes reads that surface once at
    start and no amount of reconnecting corrects a process already running
    under the old one. Detaching is only how this host stops using it.
    """

    target_sandbox = str(sandbox_id or "").strip()
    target_profile = str(profile_key or "").strip()
    if not target_sandbox or not target_profile:
        raise APIError(
            code="HERMES_GATEWAY_START_FAILED",
            message="Hermes gateway resolution requires sandbox and profile ids",
            status_code=500,
        )
    key = (target_sandbox, target_profile)
    factory: GatewayProcessFactory = (
        process_factory if process_factory is not None else HermesTuiProcess
    )
    lock = _RESOLVE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        handle = _HANDLES.pop(key, None)
        if handle is not None:
            if handle.is_live and (
                spawn_fingerprint is None
                or handle.spawn_fingerprint == spawn_fingerprint
            ):
                _HANDLES[key] = handle
                return handle
            if handle.is_live:
                logger.info(
                    "detaching Hermes backend attachment for changed profile "
                    "config: sandbox=%s profile=%s",
                    target_sandbox,
                    target_profile,
                )
            await handle.shutdown()

        fresh = HermesGatewayHandle(
            url=url,
            headers=headers,
            dial=dial,
            sandbox_id=target_sandbox,
            profile_key=target_profile,
            spawn_fingerprint=spawn_fingerprint or "",
            process_factory=factory,
        )
        try:
            await fresh.start(require_gateway_ready=True)
        except BaseException as exc:
            with contextlib.suppress(BaseException):
                await fresh.shutdown()
            raise APIError(
                code="HERMES_GATEWAY_START_FAILED",
                message=(
                    "failed to attach the resident Hermes backend for profile "
                    f"{target_profile} in sandbox {target_sandbox}: {exc}"
                ),
                status_code=502,
            ) from exc
        _HANDLES[key] = fresh
        logger.info(
            "attached resident Hermes backend: sandbox=%s profile=%s",
            target_sandbox,
            target_profile,
        )
        return fresh


__all__ = [
    "HermesGatewayHandle",
    "HermesGatewaySubscription",
    "gateway_handle_for_sandbox",
    "reset_gateway_registry",
    "resolve_gateway_handle",
]
