"""One AstraBox conversation, driven as one Codex thread.

The mapping is one-to-one and deliberately shallow: an AstraBox conversation
is a Codex **thread**, an AstraBox turn is a Codex **turn**, and the durable
key the platform stores to rejoin later is the thread id Codex minted. Nothing
here keeps a second copy of the conversation; the thread lives in the box and
`thread/resume` is how a restarted host finds it again.

The consumption boundary is unusually easy here and the reason is worth
stating, because the reference adapter had to work for it: `turn/start` is a
request, not a fire-and-forget, and its response carries the turn Codex just
created. That response IS the evidence the engine took this message off the
queue — there is no need to watch the event stream for an echo. The client
also supplies `clientUserMessageId`, so the message Codex stores carries the
platform's own input id and a re-delivery can be recognised on either side.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.resident_relay import (
    CountedRecord,
    ResidentRelay,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineStreamDetached,
    EngineTurnReceipt,
    EngineEventSink,
    ResidentOutputSink,
)
from astrabox.core.service.orchestrator.engine.codex_events import (
    APPROVAL_METHODS,
    INTERACTION_METHODS,
    QUESTION_METHOD,
    CodexProtocolError,
    CodexTurnTranslator,
    approval_response_value,
    build_approval_contract,
    build_question_contract,
    question_response_value,
)
from astrabox.core.service.orchestrator.engine.codex_child_runs import (
    COLLAB_ITEM_TYPE,
    CodexChildResources,
    spawned_thread_ids,
)
from astrabox.core.service.orchestrator.engine.codex_link import CodexAppServerLink
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    EngineTurnEmission,
    emission_from_translated_frame,
)

logger = get_logger(__name__)

ENGINE_KIND = "codex"

#: Codex's own `SandboxMode`, verbatim. These are the names the CLI's `-s`
#: flag takes and the strings `thread/start` accepts, so the console offers
#: the engine's vocabulary rather than a set invented here.
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: The `SandboxPolicy` Codex itself derives from each mode. `thread/start`
#: takes the mode by name, but a per-turn override takes the structured policy,
#: so a switch mid-conversation needs the expansion. Every entry here was read
#: off a running server's `thread/start` response for that mode rather than
#: assembled from the type definition, and a test pins them to that source.
CODEX_SANDBOX_POLICIES: dict[str, dict[str, Any]] = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {
        "type": "workspaceWrite",
        "writableRoots": [],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    },
    "danger-full-access": {"type": "dangerFullAccess"},
}


class CodexEngineClient:
    """`EngineClient` over one app-server link."""

    def __init__(
        self,
        *,
        session_id: str,
        link: CodexAppServerLink,
        native_thread_id: str | None = None,
        cwd: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        sandbox_mode: str | None = None,
        turn_options: dict[str, Any] | None = None,
        thread_config: dict[str, Any] | None = None,
        resident_output_sink: ResidentOutputSink | None = None,
        event_sink: EngineEventSink | None = None,
    ) -> None:
        self._session_id = session_id
        self._link = link
        self._resident_output_sink = resident_output_sink
        self._event_sink = event_sink
        #: The one reader of this link's inbound, started with the first use.
        self._relay: ResidentRelay | None = None
        self._inbound_sequence = 0
        self._thread_id = str(native_thread_id or "").strip() or None
        self._cwd = str(cwd or "").strip() or None
        self._instructions = str(instructions or "").strip() or None
        self._model = str(model or "").strip() or None
        self._sandbox_mode = str(sandbox_mode or "").strip() or None
        self._turn_options = dict(turn_options or {})
        self._effective_model = self._model
        #: Config overrides that ride `thread/start`. The model gateway lives
        #: here rather than in the image: Codex takes a whole `model_providers`
        #: table per thread, so a deployment's address never has to be baked
        #: into a container that is not per-deployment.
        self._thread_config = dict(thread_config or {})
        #: Set when a mode is chosen after the thread exists, so the next
        #: `turn/start` carries the override that actually moves enforcement.
        self._pending_mode_override: str | None = None
        self._receipts: dict[str, EngineTurnReceipt] = {}
        #: input id → what was sent, so the consumption frame can carry the
        #: message back verbatim rather than an empty string.
        self._sent_content: dict[str, str] = {}
        #: Inputs whose `data-input-consumed` frame has already gone out.
        #: Per input, not per client: a conversation can hold more than one
        #: live receipt — an input sent while a turn runs joins the FIFO and
        #: is followed by its own stream — and a single flag let the first
        #: turn answer for all of them. The queued input then announced no
        #: FIFO head at all, and its stream, whose first frame this is,
        #: yielded nothing and ended.
        self._consumed_inputs: set[str] = set()
        self._active_receipt: EngineTurnReceipt | None = None
        #: Inputs accepted while the thread was busy, in arrival order.
        #: The running stream prompts them as the thread frees up.
        self._buffered: list[EngineInputCommand] = []
        #: Clear while a turn is this thread's to steer. It decides which of
        #: the vendor's two methods the next input takes: `turn/start` on a
        #: free thread, `turn/steer` on the running turn. `iter_turn_events`
        #: owns the transitions, because how that generator ends is what says
        #: whether the turn is still there.
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        #: Held across `iter_turn_events` calls: an interaction ends the
        #: browser's segment while the engine's turn stays open, and a
        #: translator rebuilt on re-entry would forget the open blocks.
        self._translator: CodexTurnTranslator | None = None
        self._inbound: AsyncIterator[dict[str, Any]] | None = None
        self._child_resources: CodexChildResources | None = None
        #: interaction id → the server request it answers, so the reply can be
        #: routed by the id Codex is waiting on.
        self._open_requests: dict[str, dict[str, Any]] = {}
        #: Set on a recovered input, whose receipt carries the platform's
        #: input id because Codex's turn id was never durable here.
        self._adopt_turn_from_stream = False
        self._adopted_turn_id: str | None = None

    # ── identity ─────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return bool(self._link.is_live)

    @property
    def engine_session_key(self) -> str | None:
        return self._thread_id

    @property
    def active_receipt(self) -> EngineTurnReceipt | None:
        return self._active_receipt

    async def bind_conversation(self, binding: EngineConversationBinding) -> None:
        if binding.platform_session_id != self._session_id:
            raise RuntimeError(
                "codex conversation identity mismatch: "
                f"client={self._session_id!r} binding={binding.platform_session_id!r}"
            )
        durable_key = str(binding.engine_session_key or "").strip() or None
        if durable_key is not None:
            if self._thread_id is not None and durable_key != self._thread_id:
                raise RuntimeError(
                    "codex resume key does not match the configured conversation: "
                    f"configured={self._thread_id!r} durable={durable_key!r}"
                )
            # The thread outlives the socket. Resuming names it; it is not
            # recreated, so its history and its permission profile stand.
            #
            # Its provider table does not. `model_providers` is configuration
            # the app-server process holds, not state the thread carries, and
            # this deployment hands it over the protocol rather than writing
            # it into the box — the gateway is resolved per Agent and the
            # image is not per-Agent. So a start carrying
            # `resume_engine_session_key` — a NEW box for a conversation that
            # already has a thread — reaches a server that was never told
            # where the model is, and refuses the resume with ``Model provider
            # `astrabox` not found``: a config error naming nothing about the
            # box change behind it.
            #
            # The model is deliberately NOT re-sent with it. That one IS
            # thread state, and overriding it on every rejoin would move a
            # running conversation to a different model without anyone asking.
            resume: dict[str, Any] = {"threadId": durable_key}
            if self._thread_config:
                resume["config"] = dict(self._thread_config)
            resumed = await self._link.call("thread/resume", resume)
            self._remember_effective_model(resumed)
            self._thread_id = durable_key
        elif self._thread_id is None:
            params: dict[str, Any] = {}
            if self._cwd:
                params["cwd"] = self._cwd
            if self._model:
                params["model"] = self._model
            if self._sandbox_mode:
                params["sandbox"] = self._sandbox_mode
            if self._thread_config:
                params["config"] = dict(self._thread_config)
            if self._instructions:
                # The Agent's instructions, delivered the way Codex takes them.
                # There is no file to write: `thread/start` accepts them, so they
                # are in place before the thread composes rather than racing it.
                params["developerInstructions"] = self._instructions
            created = await self._link.call("thread/start", params)
            thread = (created or {}).get("thread")
            thread_id = str((thread or {}).get("id") or "").strip()
            if not thread_id:
                raise RuntimeError("codex thread/start returned no thread id")
            self._remember_effective_model(created)
            self._thread_id = thread_id

    def _remember_effective_model(self, response: Any) -> None:
        if not isinstance(response, dict):
            return
        model = str(response.get("model") or "").strip()
        if model:
            self._effective_model = model

    def _require_thread(self) -> str:
        if not self._thread_id:
            raise RuntimeError("codex client has no thread bound")
        return self._thread_id

    # ── sending ──────────────────────────────────────────────────────────
    async def deliver(self, command: EngineInputCommand) -> None:
        """Accept one input; prompt now, or hold it until the thread is free.

        The seam requires strictly ordered inputs and says how an engine that
        cannot queue is to meet it: the adapter buffers until the vendor
        accepts its next prompt. Codex cannot queue — `turn/start` on a busy
        thread is taken by the running turn, and `turn/steer` is the vendor's
        interrupt, which answers the new message BEFORE the running one and
        breaks the order this seam exists to keep.

        So a busy thread keeps the input and returns. `iter_turn_events`
        prompts it when the running turn reaches its terminal, and reports its
        consumption on that same stream — one dispatch, two consumptions,
        which is the shape the platform's own FIFO produces.
        """

        if self._turn_idle.is_set():
            await self._submit(command)
            return
        if command.command_id in self._receipts:
            return
        if all(held.command_id != command.command_id for held in self._buffered):
            self._buffered.append(command)
            self._sent_content.setdefault(command.input_id, command.content)

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        if consumption_confirmed:
            # Durable recovery evidence: Codex already took this input and
            # created a turn for it. Sending it again would run the same
            # message twice, so the receipt is rebuilt from the durable
            # command identity and the stream is rejoined instead.
            receipt = self._receipts.get(command.command_id)
            if receipt is None:
                # The platform's durable record is the input, so Codex's turn
                # id is not known here. It is learned from the stream instead:
                # the thread has exactly one turn in flight — the one being
                # recovered — so the first notification that names a turn
                # names that one.
                self._adopt_turn_from_stream = True
                receipt = EngineTurnReceipt(
                    engine_turn_id=command.input_id,
                    engine_session_key=self._require_thread(),
                    started_at_monotonic_ns=time.monotonic_ns(),
                    input_id=command.input_id,
                    input_consumed=True,
                )
                self._receipts[command.command_id] = receipt
                self._sent_content.setdefault(command.input_id, command.content)
            self._active_receipt = receipt
            if self._translator is None:
                self._translator = CodexTurnTranslator()
            # The consumption frame belongs to the segment that saw it, and
            # that segment is gone; re-emitting it would duplicate the user's
            # message in the transcript.
            self._consumed_inputs.add(str(command.input_id or ""))
            # A rejoined turn is a running turn: the next send waits for it
            # exactly as it would for one this process started.
            self._turn_idle.clear()
            self._ensure_relay().platform_turn_reattached()
            return receipt
        return await self._submit(command)

    async def _submit(self, command: EngineInputCommand) -> EngineTurnReceipt:
        existing = self._receipts.get(command.command_id)
        if existing is not None:
            # Idempotent by durable command id: a redelivery must not prompt
            # the engine a second time.
            self._active_receipt = existing
            return existing
        params: dict[str, Any] = {
            **self._turn_options,
            "threadId": self._require_thread(),
            "clientUserMessageId": command.input_id,
            "input": [{"type": "text", "text": command.content, "text_elements": []}],
        }
        override, self._pending_mode_override = self._pending_mode_override, None
        if override is not None:
            # "for this turn and subsequent turns" is the vendor's own wording
            # for this field, which is why one override carries the switch.
            params["sandboxPolicy"] = CODEX_SANDBOX_POLICIES[override]
        collaboration = params.get("collaborationMode")
        if isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict):
                if "model" in settings:
                    raise ValueError(
                        "codex collaborationMode.settings.model is managed by AstraBox"
                    )
                if not self._effective_model:
                    raise RuntimeError("codex did not report the thread's effective model")
                params["collaborationMode"] = {
                    **collaboration,
                    "settings": {**settings, "model": self._effective_model},
                }
        relay = self._ensure_relay()
        # Marked before the request is written: its acknowledgement and the
        # run's first notification are two messages on one socket, and the
        # relay may take the second before this coroutine takes the first.
        relay.platform_input_submitted()
        try:
            started = await self._link.call("turn/start", params)
        except BaseException:
            relay.platform_input_rejected()
            raise
        turn = (started or {}).get("turn")
        turn_id = str((turn or {}).get("id") or "").strip()
        if not turn_id:
            raise RuntimeError("codex turn/start returned no turn id")
        receipt = EngineTurnReceipt(
            engine_turn_id=turn_id,
            engine_session_key=self._require_thread(),
            started_at_monotonic_ns=time.monotonic_ns(),
            input_id=command.input_id,
            # False on a fresh delivery, and that is the platform's rule, not
            # a judgement about Codex: this flag is platform state that means
            # "durable recovery or a parked interaction already proved the
            # boundary was crossed", and a fresh send has proved no such
            # thing. What Codex's synchronous `turn/start` answer buys is that
            # the `data-input-consumed` frame below can be emitted without
            # waiting for an echo — the boundary is reported on the stream,
            # where the platform reads it.
            input_consumed=False,
        )
        self._receipts[command.command_id] = receipt
        self._sent_content[command.input_id] = command.content
        # A fresh send knows its own turn id, so nothing has to be learned.
        self._adopt_turn_from_stream = False
        self._adopted_turn_id = None
        self._active_receipt = receipt
        self._translator = CodexTurnTranslator()
        self._consumed_inputs.discard(str(command.input_id or ""))
        self._turn_idle.clear()
        return receipt

    async def iter_turn_events(
        self, receipt: EngineTurnReceipt
    ) -> AsyncIterator[EngineTurnEmission]:
        translator = self._translator
        if translator is None:
            translator = self._translator = CodexTurnTranslator()
        relay = self._ensure_relay()
        # Whether the thread is free for the next prompt, decided by how this
        # generator ends. A terminal frees it. Parking on an interaction does
        # not: Codex is still running that turn, waiting for the answer. Any
        # other ending — the platform stopped consuming, the request went away
        # — leaves the turn's fate unknown to this process, which is the same
        # as free: the next send starts a turn, as it does after a close.
        parked = False
        current = receipt
        try:
            for consumed in self._consumption_frames(str(current.input_id or "")):
                yield consumed
            while True:
                message = await self._next_turn_message(relay)
                method = str(message.get("method") or "").strip()
                if method in INTERACTION_METHODS:
                    yield self._interaction_emission(message)
                    # The turn parks here: Codex is still running it and
                    # waiting on this request's id, so the segment ends and the
                    # answer re-enters through `submit_interaction_response`.
                    parked = True
                    return
                # A child thread's own notification says nothing to this
                # turn's translator, but its completion is when the child's
                # thread is worth reading again.
                for child in await self._child_thread_frames(message):
                    yield emission_from_translated_frame(child)
                if not self._belongs_to_turn(message, current):
                    continue
                for child in await self._child_frames(message):
                    yield emission_from_translated_frame(child)
                for frame in translator.translate(message):
                    if frame.get("type") == "result":
                        # A child spawned during this turn may have finished
                        # after its announcement, and the parent stream says
                        # nothing more about it. Reading the child threads once
                        # at the terminal is what closes them.
                        for child in await self._settle_child_frames():
                            yield emission_from_translated_frame(child)
                        # The thread is free, so an input held while it was
                        # busy goes now — and its answer belongs to this
                        # stream, because the platform dispatched one turn and
                        # this is still that turn's FIFO batch. Only when
                        # nothing is held does the terminal go out and the
                        # turn end.
                        self._active_receipt = None
                        self._turn_idle.set()
                        held = await self._prompt_next_buffered()
                        if held is None:
                            yield emission_from_translated_frame(frame)
                            return
                        current = held
                        translator = self._translator = CodexTurnTranslator()
                        for consumed in self._consumption_frames(str(current.input_id or "")):
                            yield consumed
                        continue
                    yield emission_from_translated_frame(frame)
            raise EngineStreamDetached("codex app-server stream ended without a terminal")
        finally:
            if not parked:
                self._turn_idle.set()

    def _consumption_frames(self, input_id: str) -> list[EngineTurnEmission]:
        """The `data-input-consumed` frame for one input, at most once."""

        if not input_id or input_id in self._consumed_inputs:
            return []
        self._consumed_inputs.add(input_id)
        return [
            emission_from_translated_frame(
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": input_id,
                        # Platform-derived from the input id, not Codex's turn
                        # id: this identifies the FIFO head the platform is
                        # waiting to see consumed, and dispatch compares it
                        # exactly. A vendor id here reads as "some other
                        # message was consumed".
                        "responseMessageId": input_response_message_id(input_id),
                        "content": self._sent_content.get(input_id, ""),
                    },
                },
            )
        ]

    async def _prompt_next_buffered(self) -> EngineTurnReceipt | None:
        """Send the next held input now that the thread has finished a turn.

        Returns its receipt, or None when nothing is held. The caller keeps
        streaming on the same generator: the platform dispatched one turn and
        this is still that turn's FIFO batch, so the new input's boundary is
        reported here rather than on a stream nobody opened.
        """

        if not self._buffered:
            return None
        return await self._submit(self._buffered.pop(0))

    def _belongs_to_turn(self, message: dict[str, Any], receipt: EngineTurnReceipt) -> bool:
        """Filter by the turn the engine named, not by arrival order.

        One thread can be driven by more than one client and Codex tags every
        notification with its `turnId`, so a notification from someone else's
        turn is theirs. A notification with no turn id is thread-level and is
        kept, because dropping it would hide errors and warnings.
        """

        params = message.get("params")
        if not isinstance(params, dict):
            return True
        turn_id = params.get("turnId")
        if turn_id is None:
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if turn_id is None:
            return True
        if self._adopt_turn_from_stream:
            if self._adopted_turn_id is None:
                self._adopted_turn_id = str(turn_id)
            return str(turn_id) == self._adopted_turn_id
        return str(turn_id) == str(receipt.engine_turn_id)

    def _interaction_emission(self, message: dict[str, Any]) -> EngineTurnEmission:
        method = str(message.get("method") or "").strip()
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        request_id = message.get("id")
        interaction_id = f"codex-{request_id}"
        self._open_requests[interaction_id] = message
        contract = (
            build_question_contract(params)
            if method == QUESTION_METHOD
            else build_approval_contract(method, params)
        )
        return emission_from_translated_frame(
            {
                "type": "interaction.request",
                "interactionId": interaction_id,
                "payload": contract,
            },
        )

    def _interaction_frame(self, message: dict[str, Any]) -> dict[str, Any]:
        """The same request as a frame, for a response the relay publishes."""

        return dict(self._interaction_emission(message).as_frame())

    # ── the relay ────────────────────────────────────────────────────────
    def _ensure_relay(self) -> ResidentRelay:
        relay = self._relay
        if relay is None:
            relay = ResidentRelay(
                seam=_CodexRelaySeam(self),
                session_id=self._session_id,
                engine_session_key=self._thread_id,
                next_record=self._next_inbound,
                send_command=self._send_relay_command,
                current_sequence=self._current_inbound_sequence,
                resident_output_sink=self._resident_output_sink,
                event_sink=self._event_sink,
            )
            self._relay = relay
            relay.start()
        failure = relay.failure
        if failure is not None:
            # The one reader of this link is gone, so nothing the platform
            # writes now would ever be answered: the link is detached.
            raise EngineStreamDetached(
                f"codex app-server reader stopped (session={self._session_id}): {failure}"
            ) from failure
        return relay

    async def _next_inbound(self) -> CountedRecord:
        if self._inbound is None:
            self._inbound = self._link.iter_inbound()
        message = await self._inbound.__anext__()
        self._inbound_sequence += 1
        return CountedRecord(sequence=self._inbound_sequence, record=message)

    async def _current_inbound_sequence(self) -> int:
        return self._inbound_sequence

    async def _send_relay_command(self, payload: dict[str, Any]) -> Any:
        return await self._link.call(str(payload.get("method") or ""), payload.get("params"))

    async def _next_turn_message(self, relay: ResidentRelay) -> dict[str, Any]:
        item = await relay.turn_inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item.record

    async def _child_thread_frames(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Fold live child notifications; reconcile history at child completion."""

        params = message.get("params")
        thread_id = (
            str(params.get("threadId") or "").strip() if isinstance(params, dict) else ""
        )
        if not thread_id or thread_id == self._thread_id:
            return []
        method = str(message.get("method") or "")
        if method not in {
            "thread/status/changed",
            "turn/started",
            "item/started",
            "item/completed",
            "turn/completed",
        }:
            return []
        projector = self._child_resources
        if projector is None or not projector.contains(thread_id):
            if method != "turn/started":
                return []
            # Codex starts a child's turn before returning its spawn result.
            # Metadata verifies parent ownership before accepting that event.
            projector = self._child_resource_projector()
        return await projector.refresh_thread(thread_id, notification=message)

    # ── answering ────────────────────────────────────────────────────────
    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        """Encode one browser answer as the reply Codex's server request wants.

        The interaction id comes off the durable record rather than from an
        argument the platform does not pass. What cannot come from the record
        is the JSON-RPC id to reply to: a server request belongs to the
        connection that carried it, so a link this process did not open has no
        request to answer and the answer is reported as landing nowhere.
        """

        _ = receipt
        interaction_id = str(pending.get("interaction_id") or "").strip()
        if not interaction_id:
            return False
        request = self._open_requests.pop(interaction_id, None)
        if request is None:
            # Already answered, or answered on a link that has since been
            # replaced. Reported rather than raised: a stale answer is an
            # outcome the platform decides about, not a transport failure.
            return False
        method = str(request.get("method") or "").strip()
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        value = (
            question_response_value(params, response)
            if method == QUESTION_METHOD
            else approval_response_value(method, params, response)
        )
        await self._link.respond(request.get("id"), value)
        return True

    # ── stopping ─────────────────────────────────────────────────────────
    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        return await self._interrupt(receipt.engine_turn_id)

    async def interrupt_active_turn(self) -> bool:
        active = self._active_receipt
        if active is None:
            return False
        return await self._interrupt(active.engine_turn_id)

    async def _interrupt(self, turn_id: str) -> bool:
        try:
            await self._link.call(
                "turn/interrupt",
                {"threadId": self._require_thread(), "turnId": turn_id},
            )
        except Exception as exc:
            logger.warning("codex turn/interrupt failed for %s: %s", turn_id, exc)
            return False
        # Native: Codex settles the turn itself and sends `turn/completed`
        # with status `interrupted`, so the terminal comes down the stream
        # rather than being synthesized here.
        return True

    # ── child runs ───────────────────────────────────────────────────────
    def _child_resource_projector(self) -> CodexChildResources:
        projector = self._child_resources
        if projector is None:
            projector = CodexChildResources(
                root_thread_id=self._require_thread(),
                call=self._link.call,
            )
            self._child_resources = projector
        return projector

    async def _child_frames(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Child facts owed after one parent-stream message.

        The spawn announcement supplies the child's identity for its first
        metadata-only read. Subsequent child item and turn notifications carry
        live work through ``_child_thread_frames`` without reading active history.
        """

        item = (message.get("params") or {}).get("item")
        if not isinstance(item, dict) or str(item.get("type") or "") != COLLAB_ITEM_TYPE:
            return []
        projector = self._child_resource_projector()
        if not projector.observe_item(item):
            return []
        return await projector.refresh()

    async def _settle_child_frames(self) -> list[dict[str, Any]]:
        """Child facts owed at a turn terminal, if any child was announced."""

        projector = self._child_resources
        if projector is None:
            return []
        return await projector.refresh()

    async def stop_child_run(self, control_id: str) -> None:
        """Interrupt one spawned child, addressed by its own thread.

        Codex has no parent-side close for a sub-agent: the vendor's
        `closeAgent` and `interruptAgent` are tools the model calls. What the
        protocol offers a client is the ordinary per-thread interrupt, and a
        child thread takes it like any other.
        """

        thread_id = str(control_id or "").strip()
        if not thread_id:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message="codex child-run control reference is empty",
                status_code=409,
            )
        value = await self._link.call(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = (value or {}).get("thread") if isinstance(value, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        live = [
            turn
            for turn in (turns or [])
            if isinstance(turn, dict) and str(turn.get("status") or "") == "inProgress"
        ]
        if not live:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message="codex child run has no turn left to interrupt",
                status_code=409,
            )
        await self._link.call(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": str(live[-1].get("id") or "")},
        )

    # ── modes, capabilities, lifecycle ───────────────────────────────────
    async def set_permission_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip()
        if normalized not in CODEX_SANDBOX_POLICIES:
            raise ValueError(f"codex does not offer sandbox mode {mode!r}")
        if self._thread_id is None:
            # Before the thread exists the mode rides `thread/start`, where
            # Codex takes it by name and derives the policy itself.
            self._sandbox_mode = normalized
            return
        self._sandbox_mode = normalized
        self._pending_mode_override = normalized

    async def get_capabilities(self) -> EngineCapabilityManifest:
        info = self._link.server_info or {}
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            permission_modes=list(CODEX_SANDBOX_MODES),
            supports_interaction=True,
            supports_server_info=True,
            # A spawned sub-agent is an ordinary thread, so the per-thread
            # interrupt the protocol already offers is a real stop.
            supports_child_run_control=True,
            # What `initialize` answered, verbatim. It names the binary the
            # box is actually running, which is the fact a support question
            # about "which Codex is this" needs.
            extra={
                key: info[key]
                for key in ("userAgent", "codexHome", "platformOs", "platformFamily")
                if key in info
            },
        )

    async def get_server_info(self) -> dict[str, Any] | None:
        return self._link.server_info

    async def close(self) -> None:
        relay = self._relay
        self._relay = None
        if relay is not None:
            await relay.stop()
        self._turn_idle.set()
        with contextlib.suppress(BaseException):
            await self._link.close()


class _CodexRelaySeam:
    """Codex's answers to the relay, taken from its app-server protocol.

    A run is a turn on the conversation's own thread: `turn/started` opens it
    and `turn/completed` is the vendor's end. Notifications carry `threadId`,
    so a turn on a child thread is not a run of this conversation; it is the
    moment that child's thread is worth reading. The response identity is the
    vendor's own turn id.
    """

    engine_kind = ENGINE_KIND

    def __init__(self, client: CodexEngineClient) -> None:
        self._client = client

    @staticmethod
    def sequence(wire: CountedRecord) -> int:
        return wire.sequence

    @staticmethod
    def record(wire: CountedRecord) -> dict[str, Any]:
        return wire.record

    def _on_root(self, record: dict[str, Any]) -> bool:
        params = record.get("params")
        if not isinstance(params, dict):
            return False
        thread_id = str(params.get("threadId") or "").strip()
        return bool(thread_id) and thread_id == self._client._thread_id

    def starts_run(self, record: dict[str, Any]) -> bool:
        return str(record.get("method") or "") == "turn/started" and self._on_root(record)

    def settles_run(self, record: dict[str, Any]) -> bool:
        return str(record.get("method") or "") == "turn/completed" and self._on_root(record)

    @staticmethod
    def response_id(record: dict[str, Any], sequence: int) -> str:
        params = record.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        turn_id = str((turn or {}).get("id") or "").strip()
        if not turn_id:
            raise CodexProtocolError("codex turn/started carries no turn id")
        return turn_id

    @staticmethod
    def new_translator() -> CodexTurnTranslator:
        return CodexTurnTranslator()

    def translate(
        self, translator: CodexTurnTranslator, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        method = str(record.get("method") or "").strip()
        if method in INTERACTION_METHODS:
            return []
        params = record.get("params")
        thread_id = (
            str(params.get("threadId") or "").strip() if isinstance(params, dict) else ""
        )
        if thread_id and thread_id != self._client._thread_id:
            # Another thread's notification: a child's, read through its own
            # thread rather than translated as this response's output.
            return []
        return list(translator.translate(record))

    async def child_facts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        projector = self._client._child_resource_projector()
        item = (record.get("params") or {}).get("item") if isinstance(record.get("params"), dict) else None
        if isinstance(item, dict) and self._on_root(record):
            if projector.observe_item(item):
                return await projector.refresh()
            return []
        return await self._client._child_thread_frames(record)

    def native_records(self) -> list[dict[str, Any]]:
        projector = self._client._child_resources
        if projector is None:
            return []
        records, projector.native_records = projector.native_records, []
        return records

    @staticmethod
    def owed_child_reads() -> list[dict[str, Any]]:
        # Codex children are read with `thread/read` inside the fold itself.
        return []

    def carries_child_facts(self, record: dict[str, Any]) -> bool:
        params = record.get("params")
        if not isinstance(params, dict):
            return False
        if self._on_root(record):
            item = params.get("item")
            return isinstance(item, dict) and bool(spawned_thread_ids(item))
        projector = self._client._child_resources
        thread_id = str(params.get("threadId") or "").strip()
        if thread_id and record.get("method") == "turn/started":
            return True
        return projector is not None and thread_id in projector.known_thread_ids()

    def interaction(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if str(record.get("method") or "") not in INTERACTION_METHODS:
            return None
        return self._client._interaction_frame(record)
