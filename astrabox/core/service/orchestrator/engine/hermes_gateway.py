"""This host's connection to the Hermes gateway of one (sandbox, Assistant profile).

The box runs Hermes' gateway daemon (``hermes gateway run``) as a service of
its image: one session owner for every conversation of the profile, with each
conversation one gateway session (``session.create`` / ``session.resume``),
every RPC and event scoped by ``session_id``, and turns admitted into the
session's durable FIFO. The image's bridge publishes the daemon's canonical
``hermes-gateway-v1`` WebSocket to this host
(``containers/sandbox-hermes/hermes_gateway_bridge.py``).

This module owns host-side residency for that connection:

* :class:`HermesGatewayHandle` holds exactly one connection
  (:class:`~.hermes_client.HermesGatewayConnection`) and one pump task that
  fans the gateway's events and requests out to per-``session_id``
  subscriptions, so every conversation client is a session-scoped consumer
  and every RPC write is serialized through the one connection. Each
  connection reads ``runtime.describe`` before it is used, which names the
  gateway instance and proves the box serves the protocol this adapter speaks.
* :func:`resolve_gateway_handle` obtains the handle: within one host process
  an asyncio lock admits exactly one connector per (sandbox, profile), so the
  pump and its subscriptions are built once.

There is nothing to elect across hosts. The gateway is supervised inside the
box, so no host owns it and every host simply connects; each host's
connection is a separate viewer of the sessions it resumes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Awaitable, Callable

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesGatewayConnection,
    HermesGatewayFrame,
    HermesGatewayRpcError,
    _RPC_TIMEOUT_SECONDS,
)
from astrabox.core.service.orchestrator.engine.runtime import RuntimeProtocolError

logger = get_logger(__name__)

#: How long an attach may spend waiting for a gateway that is still starting.
#: It covers the box-side chain a first materialization releases — profile
#: written, Hermes composing its profile, bridge waiting for the gateway — and
#: no more: past it, a gateway that is not reachable is a broken box.
_ATTACH_BUDGET_SECONDS = 120.0
_ATTACH_RETRY_SECONDS = 1.0

#: How often, and for how long, a restart of the gateway waits for the work
#: already running in it. The budget matches the image's own profile wait
#: (`ASTRABOX_HERMES_PROFILE_WAIT_SECONDS`): a turn is allowed to run long,
#: and a conversation start that has waited half an hour is reported rather
#: than left hanging.
_IDLE_POLL_SECONDS = 2.0
_IDLE_BUDGET_SECONDS = 1800.0

#: `session.active_list`'s word for a live session with nothing running or
#: queued (``gateway/session_active_list.py``). Every other status —
#: ``working`` (a turn), ``waiting`` (a turn parked on an approval or
#: question), ``starting`` (an admitted input not yet claimed) — is work a
#: restart would end, and so is a status this adapter does not know.
_IDLE_SESSION_STATUS = "idle"

#: What ``runtime.describe`` must advertise for this adapter to drive the
#: gateway (``describe`` in ``gateway/session_controls.py``): durable admission
#: keyed by the caller's input id, per-session event replay, and the image's
#: per-Session tool environment on ``session.create``
#: (``containers/sandbox-hermes/patches/session-tool-environment.patch``).
REQUIRED_CAPABILITIES = frozenset({"durable-admission-v1", "event-replay-v1"})
REQUIRED_CREATE_PARAMETERS = frozenset({"request_id", "source", "tool_environment"})

#: Builds one connection to the gateway. The default is the real
#: :class:`HermesGatewayConnection`; tests substitute a fake wire.
GatewayConnectionFactory = Callable[..., HermesGatewayConnection]


class HermesGatewaySubscription:
    """One conversation's ordered view of the shared gateway stream.

    Offsets count frames on the handle's one connection, so they increase
    strictly for as long as it lives. The subscription drops any frame at or
    below the last offset it delivered, so a subscriber that asks to start
    after an offset is not handed what a sibling subscription already saw.
    """

    def __init__(self, *, session_id: str, after_offset: int = 0) -> None:
        self.session_id = str(session_id)
        self._queue: asyncio.Queue[HermesGatewayFrame | BaseException] = asyncio.Queue()
        self._last_offset = int(after_offset)

    @property
    def last_offset(self) -> int:
        """The output offset of the last event this subscription accepted."""

        return self._last_offset

    def _offer(self, item: HermesGatewayFrame | BaseException) -> None:
        if isinstance(item, HermesGatewayFrame):
            if item.output_offset <= self._last_offset:
                return
            self._last_offset = item.output_offset
        self._queue.put_nowait(item)

    async def next_event(self) -> HermesGatewayFrame:
        item = await self._queue.get()
        if isinstance(item, BaseException):
            # A terminal failure stays terminal: re-queue it so a later read
            # raises the same error instead of waiting forever.
            self._queue.put_nowait(item)
            raise item
        return item


def _check_describe(describe: dict[str, Any]) -> None:
    """Refuse a gateway that does not advertise what this adapter relies on."""

    capabilities = describe.get("capabilities")
    create = describe.get("session_create")
    parameters = create.get("parameters") if isinstance(create, dict) else None
    missing = sorted(REQUIRED_CAPABILITIES - set(capabilities if isinstance(capabilities, list) else ()))
    missing += sorted(
        f"session.create {name}"
        for name in REQUIRED_CREATE_PARAMETERS - set(parameters if isinstance(parameters, list) else ())
    )
    if missing or not str(describe.get("instance_id") or "").strip():
        raise RuntimeProtocolError(
            "The box's Hermes gateway does not advertise "
            + (", ".join(missing) if missing else "its instance id")
            + ". Deploy the sandbox image matching this core before starting or recovering conversations."
        )


class HermesGatewayHandle:
    """Ownership of this host's one connection to the gateway of one (sandbox, profile).

    All conversation clients of the profile share this handle: RPC writes are
    serialized by the underlying channel's send lock, and wire events reach
    each client through its own :class:`HermesGatewaySubscription`. The handle
    never decides what an event means — routing is by the vendor's own
    ``session_id`` field, and events without one (process-level notices) are
    broadcast to every subscription so each client can classify them.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        sandbox_id: str,
        profile_key: str,
        spawn_fingerprint: str = "",
        connection_factory: GatewayConnectionFactory | None = None,
    ) -> None:
        self._url = str(url)
        self._headers = dict(headers or {})
        self.sandbox_id = str(sandbox_id)
        self.profile_key = str(profile_key)
        self._spawn_fingerprint = str(spawn_fingerprint or "")
        self._connection_factory: GatewayConnectionFactory = (
            connection_factory if connection_factory is not None else HermesGatewayConnection
        )
        self._connection: HermesGatewayConnection | None = None
        self._describe: dict[str, Any] = {}
        self._pump_task: asyncio.Task[None] | None = None
        self._subscriptions: dict[str, list[HermesGatewaySubscription]] = {}
        self._lock = asyncio.Lock()

    # ── identity and liveness ────────────────────────────────────────────
    @property
    def spawn_fingerprint(self) -> str:
        return self._spawn_fingerprint

    @property
    def instance_id(self) -> str:
        """The gateway instance this connection reached (``runtime.describe``).

        A restarted gateway is a new instance; a session's event numbering
        belongs to the instance that numbered it.
        """

        return str(self._describe.get("instance_id") or "")

    @property
    def is_live(self) -> bool:
        connection = self._connection
        return connection is not None and connection.is_connected and connection.fatal is None

    # ── connection lifecycle ─────────────────────────────────────────────
    async def start(self) -> None:
        """Connect this handle to the box's gateway.

        There is nothing to spawn: the gateway is a service of the image,
        supervised there, and this only opens a socket to it through the
        bridge. ``gateway.ready`` is Hermes' own statement that the connection
        can be used, and ``runtime.describe`` names the gateway instance and
        proves the protocol before any session is opened on the connection.

        On first workspace materialization, writing the profile env file
        releases `astrabox-hermes-serve` to start Hermes, and the bridge
        publishes its port once the gateway is ready. An attempt that nothing
        accepted retries within ``_ATTACH_BUDGET_SECONDS`` and propagates the
        error when it expires. Every other failure propagates at once: a bridge
        that answered and refused the upgrade says the same thing on every
        retry, and waiting out the budget only hides the status that names the
        cause.
        """

        async with self._lock:
            if self.is_live:
                return
            deadline = time.monotonic() + _ATTACH_BUDGET_SECONDS
            attempt = 0
            while True:
                attempt += 1
                connection = self._connection_factory(url=self._url, headers=self._headers)
                try:
                    await connection.connect(require_gateway_ready=True)
                except Exception as exc:
                    if not connection.backend_not_listening or time.monotonic() >= deadline:
                        raise
                    logger.info(
                        "Hermes gateway not reachable yet; retrying: "
                        "sandbox=%s profile=%s attempt=%d err=%s",
                        self.sandbox_id,
                        self.profile_key,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(_ATTACH_RETRY_SECONDS)
                    continue
                try:
                    try:
                        describe = await connection.request("runtime.describe", {})
                    except HermesGatewayRpcError as exc:
                        if exc.code != -32601:
                            raise
                        raise RuntimeProtocolError(
                            "The box's Hermes does not serve the canonical gateway protocol "
                            "(runtime.describe). Deploy the sandbox image matching this core."
                        ) from exc
                    _check_describe(describe)
                except BaseException:
                    with contextlib.suppress(BaseException):
                        await connection.detach()
                    raise
                self._describe = describe
                self._install(connection)
                return

    async def shutdown(self) -> None:
        """Release this connection. The gateway keeps running.

        Detaching is all this handle can do. The gateway is supervised by the
        image, so neither destroying it nor keeping it alive is a decision
        available here — a profile whose configuration changed is restarted by
        supervisord, not from this side of the connection. The gateway drops
        this connection's session subscriptions when the socket closes.

        Subscribers are handed a terminal detach: their sessions cannot be
        carried by this handle any more.
        """

        async with self._lock:
            await self._stop_pump()
            connection, self._connection = self._connection, None
            if connection is not None:
                with contextlib.suppress(BaseException):
                    await connection.detach()
            self._broadcast(
                EngineStreamDetached(
                    "Hermes gateway connection was shut down for profile "
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
        return await self._connected().request(method, params, timeout=timeout)

    async def respond_error(self, request_id: str, *, code: int, message: str) -> None:
        """Answer one of the gateway's requests with a JSON-RPC error."""

        await self._connected().respond_error(request_id, code=code, message=message)

    def _connected(self) -> HermesGatewayConnection:
        connection = self._connection
        if connection is None or not connection.is_connected:
            raise EngineStreamDetached(
                f"Hermes gateway for profile {self.profile_key} is not connected"
            )
        return connection

    def subscribe(self, session_id: str, *, after_offset: int = 0) -> HermesGatewaySubscription:
        subscription = HermesGatewaySubscription(session_id=session_id, after_offset=after_offset)
        self._subscriptions.setdefault(subscription.session_id, []).append(subscription)
        connection = self._connection
        if connection is not None and connection.fatal is not None:
            # A subscriber arriving after the failure must still learn it.
            subscription._offer(connection.fatal)
        return subscription

    def unsubscribe(self, subscription: HermesGatewaySubscription) -> None:
        entries = self._subscriptions.get(subscription.session_id)
        if not entries:
            return
        with contextlib.suppress(ValueError):
            entries.remove(subscription)
        if not entries:
            self._subscriptions.pop(subscription.session_id, None)

    def subscribed(self, session_id: str) -> bool:
        """Whether a client of this connection still follows ``session_id``."""

        return bool(self._subscriptions.get(str(session_id)))

    # ── internals ────────────────────────────────────────────────────────
    def _install(self, connection: HermesGatewayConnection) -> None:
        self._connection = connection
        self._pump_task = asyncio.create_task(
            self._pump(connection),
            name=f"hermes-gateway-pump:{self.sandbox_id}:{self.profile_key}",
        )

    async def _stop_pump(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def _pump(self, connection: HermesGatewayConnection) -> None:
        try:
            while True:
                self._route(await connection.next_event())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._connection is connection:
                # A failure of a superseded connection (already detached) is a
                # consequence, not news for subscribers.
                self._broadcast(exc)

    def _route(self, wire: HermesGatewayFrame) -> None:
        scoped_session_id = wire.session_id
        if scoped_session_id:
            for subscription in tuple(self._subscriptions.get(scoped_session_id, ())):
                subscription._offer(wire)
            return
        for entries in tuple(self._subscriptions.values()):
            for subscription in tuple(entries):
                subscription._offer(wire)

    def _broadcast(self, exc: BaseException) -> None:
        for entries in tuple(self._subscriptions.values()):
            for subscription in tuple(entries):
                subscription._offer(exc)


async def wait_until_backend_idle(
    handle: HermesGatewayHandle,
    *,
    budget_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Return once nothing is running or queued in the box's Hermes gateway.

    Asked of the gateway itself, because it is the one that knows: every live
    session on it, from any host, reports its status through
    ``session.active_list``, and background delegations that outlive their
    turn are listed by ``delegation.status`` (the whole daemon's, which is what
    a restart ends). Quiet means every session idle and no delegation active.

    The caller is about to restart the gateway, which ends everything in it,
    so this waits for the work to finish rather than for a deadline to pass
    over it; past the budget it fails, naming what is still running, and the
    restart does not happen.
    """

    budget = _IDLE_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    poll = _IDLE_POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = time.monotonic() + budget
    reported: tuple[str, ...] | None = None
    while True:
        live = await handle.request("session.active_list", {})
        delegations = await handle.request("delegation.status", {})
        busy = tuple(
            sorted(
                f"{row.get('id')}:{row.get('status')}"
                for row in live.get("sessions") or []
                if isinstance(row, dict) and row.get("status") != _IDLE_SESSION_STATUS
            )
        ) + tuple(
            f"delegation:{item.get('subagent_id')}"
            for item in delegations.get("active") or []
            if isinstance(item, dict)
        )
        if not busy:
            return
        if busy != reported:
            reported = busy
            logger.info(
                "Hermes gateway restart waits for running work: sandbox=%s "
                "profile=%s running=%s",
                handle.sandbox_id,
                handle.profile_key,
                ", ".join(busy),
            )
        if time.monotonic() >= deadline:
            raise APIError(
                code="HERMES_GATEWAY_START_FAILED",
                message=(
                    "the Assistant's Hermes gateway must restart to apply a "
                    "changed configuration, and work is still running in it "
                    f"after {budget:.0f}s: {', '.join(busy)}"
                ),
                status_code=502,
            )
        await asyncio.sleep(poll)


# One handle per (sandbox_id, profile_key) in this host process. The dict is
# module state on purpose: the gateway connection outlives every conversation
# runtime, so no SessionRuntime can own it.
_HANDLES: dict[tuple[str, str], HermesGatewayHandle] = {}
_RESOLVE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


async def resolve_gateway_handle(
    *,
    url: str,
    headers: dict[str, str] | None,
    sandbox_id: str,
    profile_key: str,
    spawn_fingerprint: str | None,
    connection_factory: GatewayConnectionFactory | None = None,
    wait_until_answering: Callable[[], Awaitable[None]] | None = None,
) -> HermesGatewayHandle:
    """Return this host's connection to the box's Hermes gateway.

    Resolving it elects nothing. The gateway is a service of the image,
    supervised there and owned by no host, so there is no ownership to
    arbitrate; every host simply connects.

    What this does hold is the per-host handle registry, because the pump and
    its subscriptions are per-host state, and the configuration fence:
    ``spawn_fingerprint`` is the identity of the profile the gateway is
    serving, and a connection established under different content must not
    keep gaining sessions under the new one. Restarting the gateway so it
    re-reads a changed profile happens where the change is written
    (`_prepare_hermes_profile`), because Hermes reads that surface once at
    start and no amount of reconnecting corrects a process already running
    under the old one. Detaching is only how this host stops using it.

    ``wait_until_answering`` runs before a new connection is opened: the box
    reports when its gateway answers, because how a connection to a port that
    is not yet serving fails is the sandbox provider's, not a readiness signal.

    A gateway that does not speak this adapter's protocol raises
    :class:`RuntimeProtocolError` unchanged; every other failure to connect is
    ``HERMES_GATEWAY_START_FAILED``.
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
    lock = _RESOLVE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        handle = _HANDLES.pop(key, None)
        if handle is not None:
            if handle.is_live and (
                spawn_fingerprint is None or handle.spawn_fingerprint == spawn_fingerprint
            ):
                _HANDLES[key] = handle
                return handle
            if handle.is_live:
                logger.info(
                    "detaching Hermes gateway connection for changed profile "
                    "config: sandbox=%s profile=%s",
                    target_sandbox,
                    target_profile,
                )
            await handle.shutdown()

        fresh = HermesGatewayHandle(
            url=url,
            headers=headers,
            sandbox_id=target_sandbox,
            profile_key=target_profile,
            spawn_fingerprint=spawn_fingerprint or "",
            connection_factory=connection_factory,
        )
        if wait_until_answering is not None:
            # Its own failure names the cause; it is not a connection failure.
            await wait_until_answering()
        try:
            await fresh.start()
        except RuntimeProtocolError:
            raise
        except BaseException as exc:
            with contextlib.suppress(BaseException):
                await fresh.shutdown()
            raise APIError(
                code="HERMES_GATEWAY_START_FAILED",
                message=(
                    "failed to connect to the Hermes gateway for profile "
                    f"{target_profile} in sandbox {target_sandbox}: {exc}"
                ),
                status_code=502,
            ) from exc
        _HANDLES[key] = fresh
        logger.info(
            "connected to the Hermes gateway: sandbox=%s profile=%s instance=%s",
            target_sandbox,
            target_profile,
            fresh.instance_id,
        )
        return fresh


__all__ = [
    "HermesGatewayHandle",
    "HermesGatewaySubscription",
    "resolve_gateway_handle",
    "wait_until_backend_idle",
]
