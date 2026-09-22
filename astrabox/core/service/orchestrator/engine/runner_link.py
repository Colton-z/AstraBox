"""Host half of the runner envelope protocol (``sandbox_runner.py``).

``RunnerLink`` is the transport the claude_code EngineClient drives a slot
through: prepare before a Session exists, activate exactly once, attach on a
later host connection, then inputs and controls. The runner journal replays
every retained frame strictly after the attach cursor. If that cursor expired,
the runner sends one ordered ``gap`` instruction before the retained suffix;
the EngineClient turns it into a SessionStore rebuild instruction. Missing
frames are never synthesized by this wire.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached

logger = get_logger(__name__)

#: Hello frames must match this value exactly. ``sandbox_runner.py`` repeats
#: the constant because it ships standalone, and
#: ``tests/runner_link_wire_test.py`` pins both copies equal.
RUNNER_PROTOCOL = "astrabox.runner-wire.v1"


class RunnerLinkError(Exception):
    """Protocol violation or runner-reported error. Fail loud."""


class _InputAckConnectionLost(Exception):
    """The current transport closed while an input receipt was outstanding."""


@dataclass(frozen=True, slots=True)
class DeliveryCommand:
    command_id: str
    session_id: str
    sequence: int
    sdk_input: dict[str, Any]


def _engine_requirements(
    option_keys: Collection[str],
    *,
    permission_mode: str | None,
) -> dict[str, Any]:
    mode = str(permission_mode or "default").strip()
    if not mode:
        raise RunnerLinkError("permission mode requirement is empty")
    return {
        "adapter": "claude_code",
        "required_option_keys": sorted(
            {str(key).strip() for key in option_keys if str(key).strip()}
        ),
        "permission_mode": mode,
    }


def _validate_runner_engine_contract(
    hello: dict[str, Any],
    requirements: dict[str, Any],
) -> None:
    contract = hello.get("engine_contract")
    if not isinstance(contract, dict):
        raise RunnerLinkError("runner hello has no engine_contract")
    if contract.get("adapter") != requirements["adapter"]:
        raise RunnerLinkError(
            "runner engine adapter mismatch "
            f"expected={requirements['adapter']!r} actual={contract.get('adapter')!r}"
        )
    sdk_version = str(contract.get("sdk_version") or "").strip()
    if not sdk_version:
        raise RunnerLinkError("runner engine contract has no sdk_version")
    accepted_option_keys = contract.get("accepted_option_keys")
    permission_modes = contract.get("permission_modes")
    if not isinstance(accepted_option_keys, list) or any(
        not isinstance(key, str) or not key.strip() for key in accepted_option_keys
    ):
        raise RunnerLinkError(
            "runner engine contract accepted_option_keys must be a list of names"
        )
    if not isinstance(permission_modes, list) or any(
        not isinstance(mode, str) or not mode.strip() for mode in permission_modes
    ):
        raise RunnerLinkError(
            "runner engine contract permission_modes must be a list of names"
        )
    missing = sorted(
        set(requirements["required_option_keys"]) - set(accepted_option_keys)
    )
    if missing:
        raise RunnerLinkError(
            f"runner image does not accept required Claude options {missing!r}"
        )
    required_mode = str(requirements["permission_mode"])
    if required_mode not in permission_modes:
        raise RunnerLinkError(
            "runner image does not support required Claude permission mode "
            f"{required_mode!r}"
        )


class RunnerLink:
    """One websocket to one box's runner. Use as an async context manager."""

    def __init__(
        self,
        uri: str,
        *,
        persistent_event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._uri = uri
        self._persistent_event_handler = persistent_event_handler
        self._ws: Any = None
        self._recv_task: asyncio.Task[None] | None = None
        self._prepared: asyncio.Future[dict[str, Any]] | None = None
        self._hello: asyncio.Future[dict[str, Any]] | None = None
        self._input_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._answer_acks: dict[str, asyncio.Future[bool]] = {}
        self._permission_mode_acks: dict[str, asyncio.Future[str]] = {}
        self._init_info: asyncio.Future[dict[str, Any] | None] | None = None
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._expected_seq: int | None = None
        self._gap_received = False
        self._session_id: str | None = None
        self._opening_engine_requirements: dict[str, Any] | None = None
        self._connection_generation = 0
        self._reconnect_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "RunnerLink":
        await self._connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def is_live(self) -> bool:
        """True while this link can still carry frames.

        False after :meth:`close`, and false once the peer went away — the
        recv loop exits on a remote close or transport error, so its task
        being done is the death certificate. The runtime manager consults
        this before reusing a registered runtime: a dead link's runtime must
        be evicted and re-attached, not handed another turn to fail on.
        """
        if self._closed:
            return False
        task = self._recv_task
        return task is not None and not task.done()

    async def close(self) -> None:
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001 - best-effort transport cleanup
                logger.info("runner link close failed: %s", exc)
            self._ws = None

    async def _connect(self) -> None:
        try:
            self._ws = await websocket_connect(self._uri)
        except Exception as exc:  # noqa: BLE001 - vendor transport boundary
            raise EngineStreamDetached(
                f"runner transport could not connect to {self._uri!r}: {exc}"
            ) from exc
        self._connection_generation += 1
        generation = self._connection_generation
        self._recv_task = asyncio.create_task(
            self._recv_loop(self._ws, generation),
            name="runner-link-recv",
        )

    async def _replace_connection(self) -> None:
        """Drop this transport without declaring the logical link closed."""
        self._connection_generation += 1
        recv_task = self._recv_task
        ws = self._ws
        self._recv_task = None
        self._ws = None
        if recv_task is not None:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001 - replacing a dead transport
                logger.info("runner link replacement close failed: %s", exc)
        await self._connect()

    async def _send_wire(self, payload: str, *, action: str) -> None:
        ws = self._ws
        if ws is None:
            raise EngineStreamDetached(
                f"runner transport detached before {action}"
            )
        try:
            await ws.send(payload)
        except (ConnectionClosed, OSError) as exc:
            raise EngineStreamDetached(
                f"runner transport detached during {action}: {exc}"
            ) from exc

    # -- opening handshakes ---------------------------------------------------

    async def prepare(
        self,
        slot_id: str,
        *,
        activation_token: str,
        options: dict[str, Any] | None = None,
        claimed_by: str | None = None,
        store: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Spawn and initialize one Claude process.

        Unclaimed by default — a prepared slot has no Session and no resume.
        A direct start passes ``claimed_by`` and ``store`` because its
        ``options.resume`` is read during the connect this performs, and the
        vendor materializes a resume from the store only if one is bound.
        """

        if self._ws is None:
            raise RunnerLinkError("link not connected")
        target = str(slot_id or "").strip()
        if not target:
            raise RunnerLinkError("prepare requires slot_id")
        token = str(activation_token or "").strip()
        if not token:
            raise RunnerLinkError("prepare requires activation_token")
        resolved_options = dict(options or {})
        claimant = str(claimed_by or "").strip()
        if str(resolved_options.get("resume") or "").strip() and not (
            claimant and store
        ):
            # Reject an unsatisfiable resume before launching the CLI, so the
            # caller receives the missing prerequisite rather than a link error.
            raise RunnerLinkError(
                "prepare carries options.resume without a claimant and store; "
                "the vendor materializes a resume from the session store, and "
                "an unclaimed slot has neither"
            )
        requirements = _engine_requirements(
            resolved_options.keys(),
            permission_mode=str(
                resolved_options.get("permission_mode") or "default"
            ),
        )
        loop = asyncio.get_running_loop()
        self._prepared = loop.create_future()
        self._expected_seq = None
        frame = {
            "op": "prepare",
            "slot_id": target,
            "activation_token": token,
            "options": resolved_options,
            "engine_requirements": requirements,
            **({"claimed_by": claimant} if claimant else {}),
            **({"store": store} if claimant and store else {}),
        }
        await self._send_wire(json.dumps(frame), action="prepare handshake")
        prepared = await self._prepared
        _validate_runner_engine_contract(prepared, requirements)
        if str(prepared.get("slot_id") or "") != target:
            raise RunnerLinkError(
                "runner prepared a different slot "
                f"expected={target!r} actual={prepared.get('slot_id')!r}"
            )
        self._opening_engine_requirements = dict(requirements)
        self._session_id = None
        return prepared

    async def activate(
        self,
        slot_id: str,
        session_id: str,
        *,
        activation_token: str,
        store: dict[str, Any] | None = None,
        death_notice: dict[str, Any] | None = None,
        required_option_keys: Collection[str] | None = None,
        permission_mode: str | None = None,
        mcp_servers: Collection[str] | None = None,
        resume_session_key: str | None = None,
    ) -> dict[str, Any]:
        """Bind one prepared slot to one platform Session."""

        target_slot = str(slot_id or "").strip()
        if not target_slot:
            raise RunnerLinkError("activate requires slot_id")
        token = str(activation_token or "").strip()
        if not token:
            raise RunnerLinkError("activate requires activation_token")
        requirements = (
            dict(self._opening_engine_requirements)
            if required_option_keys is None
            and permission_mode is None
            and self._opening_engine_requirements is not None
            else _engine_requirements(
                required_option_keys or (), permission_mode=permission_mode
            )
        )
        activation_mode = str(
            permission_mode or requirements["permission_mode"]
        ).strip()
        return await self._open_session({
            "op": "activate",
            "slot_id": target_slot,
            "activation_token": token,
            "session_id": session_id,
            "permission_mode": activation_mode,
            "mcp_servers": [
                str(name).strip()
                for name in (mcp_servers or ())
                if str(name).strip()
            ],
            "engine_requirements": requirements,
            **({"store": store} if store is not None else {}),
            **({"death_notice": death_notice} if death_notice is not None else {}),
            **({"resume": resume_session_key} if resume_session_key else {}),
        })

    async def configure(
        self,
        session_id: str,
        *,
        options: dict[str, Any] | None = None,
        store: dict[str, Any] | None = None,
        death_notice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Cold starts use the same two barriers as pooled starts.  Keeping this
        # composite at the host API avoids duplicating a fresh-session flow;
        # there is no ``configure`` operation on the wire.
        slot_id = f"direct-{uuid.uuid4().hex}"
        activation_token = uuid.uuid4().hex + uuid.uuid4().hex
        resolved_options = dict(options or {})
        if "resume_transcript" in resolved_options:
            raise RunnerLinkError(
                "configure options cannot override runner resume_transcript"
            )
        if str(resolved_options.get("resume") or "").strip():
            target_session = str(session_id or "").strip()
            if not target_session:
                raise RunnerLinkError("configure resume requires session_id")
            if not isinstance(store, dict) or not store.get("base_url"):
                raise RunnerLinkError(
                    "configure resume requires transcript store.base_url"
                )
            # Claude's SDK restores SessionStore during connect(), which is
            # the prepare barrier, and it materializes the transcript from the
            # store whenever the box has no local copy — which is every rebuild
            # after a sandbox death. So the read source is bound BEFORE that
            # connect, not on the activate that follows it; the in-box target
            # still refuses platform writes until activate proves this same
            # Session and store binding.
            resolved_options["resume_transcript"] = {
                "platform_session_id": target_session,
                "store": dict(store),
            }
        await self.prepare(
            slot_id,
            activation_token=activation_token,
            options=resolved_options,
            claimed_by=session_id,
            store=store,
        )
        await self._replace_connection()
        return await self.activate(
            slot_id,
            session_id,
            activation_token=activation_token,
            store=store,
            death_notice=death_notice,
        )

    async def attach(
        self,
        session_id: str,
        *,
        last_seen_seq: int,
        required_option_keys: Collection[str] | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(last_seen_seq, bool)
            or not isinstance(last_seen_seq, int)
            or last_seen_seq < 0
        ):
            raise RunnerLinkError("last_seen_seq must be a non-negative int")
        requirements = (
            dict(self._opening_engine_requirements)
            if required_option_keys is None
            and permission_mode is None
            and self._opening_engine_requirements is not None
            else _engine_requirements(
                required_option_keys or (),
                permission_mode=permission_mode,
            )
        )
        return await self._open_session({
            "op": "attach",
            "session_id": session_id,
            "last_seen_seq": last_seen_seq,
            "engine_requirements": requirements,
        })

    async def _open_session(self, frame: dict[str, Any]) -> dict[str, Any]:
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        loop = asyncio.get_running_loop()
        self._hello = loop.create_future()
        self._gap_received = False
        requested_cursor = (
            int(frame["last_seen_seq"])
            if frame["op"] == "attach"
            else 0
        )
        # Install the expectation before the opening frame goes out. The
        # runner sends hello and immediately replays; setting this after hello
        # races the receive task and either duplicates or drops the first item.
        self._expected_seq = requested_cursor + 1
        await self._send_wire(json.dumps(frame), action=f"{frame['op']} handshake")
        hello = await self._hello
        requirements = frame.get("engine_requirements")
        if not isinstance(requirements, dict):
            raise RunnerLinkError("opening frame has no engine_requirements")
        _validate_runner_engine_contract(hello, requirements)
        self._opening_engine_requirements = dict(requirements)
        self._session_id = str(frame["session_id"])
        return hello

    # -- host → runner ---------------------------------------------------------

    async def deliver(
        self,
        command: DeliveryCommand,
        *,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Send one input, requiring a correlated runner receipt.

        A websocket accepting bytes is not delivery proof: another attach may
        already own runner output, leaving this connection alive but unable to
        receive its acknowledgement. One missing receipt reattaches from this
        link's cursor: a receipt already journaled is replayed; otherwise the
        same idempotency key is sent again. A second missing receipt is a
        protocol failure, not an unbounded bridge wait.
        """
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        command_id = str(command.command_id or "").strip()
        if not command_id:
            raise RunnerLinkError("input requires command_id")
        session_id = str(command.session_id or "").strip()
        if not session_id:
            raise RunnerLinkError("input requires session_id")
        if session_id != self._session_id:
            raise RunnerLinkError(
                "input belongs to a different session "
                f"expected={self._session_id!r} actual={session_id!r}"
            )
        if (
            isinstance(command.sequence, bool)
            or not isinstance(command.sequence, int)
            or command.sequence <= 0
        ):
            raise RunnerLinkError("input requires a positive sequence")
        if not isinstance(command.sdk_input, dict):
            raise RunnerLinkError("input requires sdk_input")
        if command_id in self._input_acks:
            raise RunnerLinkError(f"input already in flight for command {command_id!r}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._input_acks[command_id] = future
        frame = json.dumps(
            {
                "op": "input",
                "command_id": command_id,
                "session_id": session_id,
                "sequence": command.sequence,
                "sdk_input": command.sdk_input,
            }
        )
        logger.info(
            "input delivery: session=%s command_id=%s sequence=%s",
            session_id,
            command_id,
            command.sequence,
        )
        try:
            for attempt in range(2):
                try:
                    if not future.done():
                        assert self._ws is not None
                        try:
                            await self._send_wire(frame, action="input delivery")
                        except EngineStreamDetached as exc:
                            raise _InputAckConnectionLost from exc
                    return await self._wait_for_input_ack(
                        future,
                        timeout_s=timeout_s,
                    )
                except (TimeoutError, _InputAckConnectionLost) as exc:
                    if attempt == 1:
                        raise EngineStreamDetached(
                            f"runner did not acknowledge input {command_id!r} "
                            f"after reattach within its {timeout_s:g}s receipt budget"
                        ) from exc
                    await self._reattach_for_unconfirmed_input(
                        command_id,
                        timeout_s=timeout_s,
                    )
            raise AssertionError("input receipt retry loop exhausted")
        finally:
            self._input_acks.pop(command_id, None)

    async def _wait_for_input_ack(
        self,
        future: asyncio.Future[dict[str, Any]],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        recv_task = self._recv_task
        if recv_task is None:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        done, _pending = await asyncio.wait(
            {future, recv_task},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if future in done:
            return future.result()
        if recv_task in done:
            raise _InputAckConnectionLost
        raise TimeoutError

    async def _reattach_for_unconfirmed_input(
        self,
        command_id: str,
        *,
        timeout_s: float,
    ) -> None:
        session_id = str(self._session_id or "").strip()
        if not session_id:
            raise RunnerLinkError(
                f"cannot reattach unacknowledged input {command_id!r}: session is unknown"
            )
        async with self._reconnect_lock:
            future = self._input_acks.get(command_id)
            if future is None or future.done():
                return
            last_seen_seq = self.last_seen_seq
            logger.warning(
                "runner input receipt missing; reattaching session=%s command=%s "
                "after_seq=%d",
                session_id,
                command_id,
                last_seen_seq,
            )
            try:
                await self._replace_connection()
                await asyncio.wait_for(
                    self.attach(session_id, last_seen_seq=last_seen_seq),
                    timeout=timeout_s,
                )
            except TimeoutError as exc:
                await self.close()
                raise EngineStreamDetached(
                    f"runner did not acknowledge reattach for input {command_id!r} "
                    f"within {timeout_s:g}s"
                ) from exc
            except BaseException:
                await self.close()
                raise

    async def answer(
        self,
        interaction_id: str,
        decision: str,
        *,
        updated_input: dict[str, Any] | None = None,
        message: str | None = None,
        timeout_s: float = 10.0,
    ) -> bool:
        """Send one interaction answer and wait for the runner's verdict."""
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        interaction_id = str(interaction_id or "").strip()
        if not interaction_id:
            raise RunnerLinkError("answer requires interaction_id")
        if interaction_id in self._answer_acks:
            raise RunnerLinkError(
                f"answer already in flight for interaction {interaction_id!r}"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._answer_acks[interaction_id] = future
        try:
            await self._send_wire(json.dumps({
                "op": "answer",
                "interaction_id": interaction_id,
                "decision": decision,
                "updated_input": updated_input,
                "message": message,
            }), action="interaction answer")
            try:
                return await asyncio.wait_for(future, timeout=timeout_s)
            except TimeoutError as exc:
                raise EngineStreamDetached(
                    f"runner did not acknowledge interaction answer {interaction_id!r} "
                    f"within {timeout_s:g}s"
                ) from exc
        finally:
            self._answer_acks.pop(interaction_id, None)

    async def interrupt(self, command_id: str) -> None:
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        command_id = str(command_id or "").strip()
        if not command_id:
            raise RunnerLinkError("interrupt requires command_id")
        await self._send_wire(json.dumps({
            "op": "interrupt",
            "command_id": command_id,
        }), action="turn interrupt")

    async def stop_task(self, task_id: str) -> None:
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        await self._send_wire(
            json.dumps({"op": "stop_task", "task_id": task_id}),
            action="background task stop",
        )

    async def set_permission_mode(self, mode: str, *, timeout_s: float = 10.0) -> None:
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        mode = str(mode or "").strip()
        if not mode:
            raise RunnerLinkError("set_permission_mode requires mode")
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._permission_mode_acks[request_id] = future
        try:
            await self._send_wire(
                json.dumps({
                    "op": "set_permission_mode",
                    "request_id": request_id,
                    "mode": mode,
                }),
                action="permission-mode change",
            )
            try:
                applied_mode = await asyncio.wait_for(future, timeout=timeout_s)
            except TimeoutError as exc:
                raise EngineStreamDetached(
                    "runner did not acknowledge permission-mode change "
                    f"{request_id!r} within {timeout_s:g}s"
                ) from exc
            if applied_mode != mode:
                raise RunnerLinkError(
                    "runner acknowledged a different permission mode "
                    f"requested={mode!r} applied={applied_mode!r}"
                )
        finally:
            self._permission_mode_acks.pop(request_id, None)

    async def get_init_info(self, *, timeout_s: float = 10.0) -> dict[str, Any] | None:
        """Fetch the SDK's initialize snapshot (commands/skills/model).

        Request/response over the envelope: one in flight at a time — the
        platform reads this once per runtime bring-up.
        """
        if self._ws is None:
            raise RunnerLinkError("link not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        self._init_info = future
        try:
            await self._send_wire(
                json.dumps({"op": "get_init_info"}),
                action="initialization metadata read",
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout_s)
            except TimeoutError as exc:
                raise EngineStreamDetached(
                    "runner did not answer the initialization metadata read "
                    f"within {timeout_s:g}s"
                ) from exc
        finally:
            self._init_info = None

    # -- runner → host ---------------------------------------------------------

    async def next_frame(self) -> dict[str, Any] | None:
        """The next enveloped frame in order, or ``None`` once the link is dead.

        One reader at a time: a frame taken here is gone from the link, so the
        engine client hands the read position between its resident observer
        and a turn consumer instead of letting both call this. The call is
        safe to cancel while it waits — a frame is only taken once the wait
        has returned, so a cancelled wait leaves it in place for the next
        reader.
        """
        while True:
            frame = await self._events.get()
            if frame is None:
                # A receipt recovery replaces a connection before the turn
                # consumer starts. The old receive task may already have
                # published its EOF; once the rebuilt link is live that EOF is
                # stale and cannot terminate the new stream.
                if self.is_live:
                    continue
                return None
            return frame

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Enveloped frames in order, with an ordered runner ``gap`` when the
        requested cursor expired. Ends when the connection closes."""
        while True:
            frame = await self.next_frame()
            if frame is None:
                return
            yield frame

    @property
    def last_seen_seq(self) -> int:
        return (self._expected_seq or 1) - 1

    async def _recv_loop(self, ws: Any, generation: int) -> None:
        try:
            async for raw in ws:
                await self._route(json.loads(raw), ws=ws)
        except Exception as exc:  # noqa: BLE001 — connection teardown path
            if not self._closed:
                logger.info("runner link closed: %s", exc)
        finally:
            if generation != self._connection_generation:
                return
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001 - still wake every waiter
                logger.info("runner link close failed: %s", exc)
            self._events.put_nowait(None)
            if self._prepared is not None and not self._prepared.done():
                self._prepared.set_exception(
                    EngineStreamDetached("runner link closed during preparation")
                )
            if self._hello is not None and not self._hello.done():
                self._hello.set_exception(
                    EngineStreamDetached("runner link closed during handshake")
                )
            for future in self._answer_acks.values():
                if not future.done():
                    future.set_exception(
                        EngineStreamDetached(
                            "runner link closed before answer acknowledgement"
                        )
                    )
            for future in self._permission_mode_acks.values():
                if not future.done():
                    future.set_exception(
                        EngineStreamDetached(
                            "runner link closed before permission-mode acknowledgement"
                        )
                    )
            if self._init_info is not None and not self._init_info.done():
                self._init_info.set_exception(
                    EngineStreamDetached(
                        "runner link closed before initialization metadata response"
                    )
                )
    async def _route(self, frame: dict[str, Any], *, ws: Any) -> None:
        op = frame.get("op")
        if op == "prepared":
            actual_protocol = frame.get("protocol")
            if actual_protocol != RUNNER_PROTOCOL:
                error = RunnerLinkError(
                    "runner protocol mismatch "
                    f"expected={RUNNER_PROTOCOL!r} actual={actual_protocol!r}"
                )
                if self._prepared is not None and not self._prepared.done():
                    self._prepared.set_exception(error)
                raise error
            if self._prepared is not None and not self._prepared.done():
                self._prepared.set_result(frame)
            return
        if op == "hello":
            actual_protocol = frame.get("protocol")
            if actual_protocol != RUNNER_PROTOCOL:
                error = RunnerLinkError(
                    "runner protocol mismatch "
                    f"expected={RUNNER_PROTOCOL!r} actual={actual_protocol!r}"
                )
                if self._hello is not None and not self._hello.done():
                    self._hello.set_exception(error)
                raise error
            if self._hello is not None and not self._hello.done():
                self._hello.set_result(frame)
            return
        if op == "error":
            detail = str(frame.get("detail") or "runner error")
            if self._prepared is not None and not self._prepared.done():
                self._prepared.set_exception(RunnerLinkError(detail))
                return
            if self._hello is not None and not self._hello.done():
                self._hello.set_exception(RunnerLinkError(detail))
                return
            raise RunnerLinkError(detail)
        if op == "answer_ack":
            interaction_id = str(frame.get("interaction_id") or "")
            future = self._answer_acks.get(interaction_id)
            if future is None or future.done():
                raise RunnerLinkError(
                    f"unexpected answer acknowledgement for {interaction_id!r}"
                )
            accepted = frame.get("accepted")
            if not isinstance(accepted, bool):
                error = RunnerLinkError(
                    f"malformed answer acknowledgement for {interaction_id!r}"
                )
                future.set_exception(error)
                raise error
            future.set_result(accepted)
            return
        if op == "permission_mode_ack":
            request_id = str(frame.get("request_id") or "").strip()
            future = self._permission_mode_acks.get(request_id)
            if future is None or future.done():
                raise RunnerLinkError(
                    f"unexpected permission-mode acknowledgement for {request_id!r}"
                )
            mode = str(frame.get("mode") or "").strip()
            if not mode:
                error = RunnerLinkError(
                    f"malformed permission-mode acknowledgement for {request_id!r}"
                )
                future.set_exception(error)
                raise error
            future.set_result(mode)
            return
        if op == "gap":
            if self._gap_received:
                raise RunnerLinkError("runner sent more than one gap for one attach")
            after_sequence = frame.get("after_sequence")
            first_retained = frame.get("first_retained_sequence")
            last_sequence = frame.get("last_sequence")
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
                or isinstance(last_sequence, bool)
                or not isinstance(last_sequence, int)
                or last_sequence < 0
                or last_sequence <= after_sequence
                or (
                    first_retained is not None
                    and (
                        isinstance(first_retained, bool)
                        or not isinstance(first_retained, int)
                        or first_retained < 1
                        or first_retained > last_sequence
                        or after_sequence >= first_retained - 1
                    )
                )
            ):
                raise RunnerLinkError(f"malformed runner journal gap: {frame!r}")
            self._gap_received = True
            self._expected_seq = (
                first_retained if isinstance(first_retained, int) else last_sequence + 1
            )
            self._events.put_nowait(frame)
            return
        seq = frame.get("seq")
        if isinstance(seq, int) and self._expected_seq is not None:
            if seq < self._expected_seq:
                # A replay is strictly-after-cursor. Seeing an already applied
                # frame is not another event; discard it before routing.
                return
            if seq > self._expected_seq:
                raise RunnerLinkError(
                    "runner replay is non-contiguous "
                    f"(expected {self._expected_seq}, received {seq})"
                )
            await self._persist_required_event(frame, ws=ws)
            self._expected_seq = seq + 1
        if op == "init_info":
            future = self._init_info
            if future is not None and not future.done():
                info = frame.get("info")
                future.set_result(info if isinstance(info, dict) else None)
            return
        if op == "input_ack":
            command_id = str(frame.get("command_id") or "")
            future = self._input_acks.get(command_id)
            if future is None or future.done():
                return
            duplicate = frame.get("duplicate")
            seq = frame.get("seq")
            if (
                not isinstance(duplicate, bool)
                or isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq < 1
            ):
                error = RunnerLinkError(
                    f"malformed input acknowledgement for {command_id!r}"
                )
                future.set_exception(error)
                raise error
            future.set_result(frame)
            return
        self._events.put_nowait(frame)

    async def _persist_required_event(self, frame: dict[str, Any], *, ws: Any) -> None:
        """Commit a durable SDK event before it can advance the live cursor."""

        requires_persistence = frame.get("requires_persistence")
        if requires_persistence is None:
            return
        if requires_persistence is not True:
            raise RunnerLinkError("requires_persistence must be true when present")
        if frame.get("op") != "event":
            raise RunnerLinkError("only runner event frames may require persistence")
        sequence = frame.get("seq")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
        ):
            raise RunnerLinkError("durable runner event requires a positive seq")
        message = frame.get("message")
        if not isinstance(message, dict):
            raise RunnerLinkError("durable runner event requires an SDK message object")
        handler = self._persistent_event_handler
        if handler is None:
            raise RunnerLinkError(
                "runner emitted a durable SDK event without a persistence handler"
            )
        try:
            await handler(dict(frame))
        except Exception as exc:  # noqa: BLE001 - persistence failure kills the link
            logger.exception(
                "runner durable event persistence failed: seq=%s type=%s",
                sequence,
                frame.get("message_type"),
            )
            raise RunnerLinkError(
                f"runner durable event persistence failed at seq {sequence}"
            ) from exc
        await ws.send(json.dumps({"op": "event_persisted", "seq": sequence}))
