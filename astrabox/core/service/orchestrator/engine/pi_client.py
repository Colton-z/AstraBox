"""Pi engine client over an execd JSON-lines pipe.

Pi's supported headless surface is ``pi --mode rpc``: commands as JSON lines
on stdin, responses and events as JSON lines on stdout. AstraBox runs one such
process per conversation behind an execd pipe session, so pi opens no port and
no platform credential is copied into the sandbox.

Three of pi's readings drive this client, and each is the vendor's own:

* **Only ``agent_settled`` ends a model run.** Pi's ``turn`` is one
  assistant response plus its tool calls, and its ``agent_end`` can be
  followed by an automatic retry, a compaction retry, or a queued
  continuation. Settling on either would cut a turn short mid-answer.
  A registered extension command can instead finish without a model run;
  its correlated ``prompt`` response reports that its handler returned.
* **One command, one response.** A command carrying an ``id`` is answered
  once, with the same id, and ``success`` reports acceptance only — failures
  after acceptance arrive as events. That response is the consumption
  evidence ``data-input-consumed`` requires. A registered command's native
  dialog or model start also proves its handler is already executing, before
  that handler can return its final response.
* **A prompt sent during streaming must say how to queue.** Pi refuses one
  that does not. The platform FIFO means "after the work in flight", which is
  pi's ``followUp``; ``steer`` would cut into the running turn instead.

Self-registration lives in ``pi.py``; this module is the client only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineEventSink,
    EngineInputCommand,
    EngineStreamDetached,
    EngineTurnReceipt,
    ResidentOutputSink,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    EngineTurnEmission,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
    validated_question_answer_rows,
)
from astrabox.core.service.orchestrator.engine.pi_child_runs import (
    PiChildResources,
    inspect_command_line,
    is_async_status_widget,
    is_inspect_widget,
    stop_command_line,
)
from astrabox.core.service.orchestrator.engine.pi_events import (
    PiProtocolError,
    PiTurnTranslator,
    raw_event_frame,
)
from astrabox.core.service.orchestrator.engine.pi_child_transcript import PiChildTranscriptCapture
from astrabox.core.service.orchestrator.engine.pi_pipe import PiRpcProcess, PiWireRecord
from astrabox.core.service.orchestrator.engine.resident_relay import ResidentRelay
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    EXECD_PORT,
    ResolvedExecdEndpoint,
    resolve_sandbox_endpoint,
)

ENGINE_KIND = "pi"

_ANCHOR_PREFIX = "pi-rpc-v1."
_COMMAND_TIMEOUT_SECONDS = 120.0

#: Pi's extension-UI methods that block until the client answers. The rest
#: (notify, setStatus, setWidget, setTitle, set_editor_text) are
#: fire-and-forget: parking a turn on one would wait for a reply pi is not
#: going to read.
_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

#: The labels a confirm dialog offers. Pi's confirm carries no options of its
#: own — it answers with a boolean — so the adapter supplies the two it will
#: translate back.
_CONFIRM_YES = "Yes"
_CONFIRM_NO = "No"

logger = get_logger(__name__)


def encode_turn_anchor(
    *,
    pty_session_id: str,
    pi_session_id: str,
    command_id: str,
) -> str:
    payload = {
        "pty_session_id": str(pty_session_id or "").strip(),
        "pi_session_id": str(pi_session_id or "").strip(),
        "command_id": str(command_id or "").strip(),
    }
    if any(not value for value in payload.values()):
        raise ValueError("pi turn anchor requires PTY, pi session, and command ids")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _ANCHOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_turn_anchor(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text.startswith(_ANCHOR_PREFIX):
        raise ValueError("not a pi turn anchor")
    token = text[len(_ANCHOR_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid pi turn anchor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid pi turn anchor payload")
    result = {
        key: str(decoded.get(key) or "").strip()
        for key in ("pty_session_id", "pi_session_id", "command_id")
    }
    if any(not value for value in result.values()):
        raise ValueError("incomplete pi turn anchor")
    return result


def _interaction_contract(request: dict[str, Any]) -> dict[str, Any]:
    """One pi extension dialog as an AstraBox form contract.

    Every dialog becomes a form with a single question. Pi's four dialogs
    differ in how the answer is *encoded*, not in what the console has to
    render, and the method is carried on the question so the reply can be
    encoded pi's way without re-reading the request.
    """

    method = str(request.get("method") or "").strip()
    request_id = str(request.get("id") or "").strip()
    title = str(request.get("title") or "").strip()
    if method == "confirm":
        message = str(request.get("message") or "").strip()
        prompt = " ".join(part for part in (title, message) if part) or "Confirm"
        options = [{"label": _CONFIRM_YES}, {"label": _CONFIRM_NO}]
    elif method == "select":
        prompt = title or "Select an option"
        raw_options = request.get("options")
        options = [
            {"label": str(option)}
            for option in (raw_options if isinstance(raw_options, list) else [])
            if str(option).strip()
        ]
        if not options:
            raise PiProtocolError(f"pi select {request_id!r} offered no options")
    else:
        # input and editor take free text; an empty option list is how the
        # console is told to ask for it.
        prompt = title or ("Edit" if method == "editor" else "Enter a value")
        options = []
    return {
        "tool_name": f"pi.extension.{method}",
        "presentation": PRESENTATION_FORM,
        "prompt": prompt,
        "raw_input": dict(request),
        "questions": [
            {
                "id": request_id,
                "header": title or method,
                "question": prompt,
                "native_answer_key": method,
                "multi_select": False,
                "allow_free_text": method in {"input", "editor"},
                "allow_empty_text": method in {"input", "editor"},
                "options": options,
            }
        ],
    }


def _interaction_frame(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "interaction.request",
        "interactionId": str(request.get("id") or "").strip(),
        "payload": _interaction_contract(request),
    }


class PiEngineClient:
    """EngineClient backed by one ``pi --mode rpc`` process."""

    def __init__(
        self,
        *,
        endpoint: ResolvedExecdEndpoint,
        platform_session_id: str,
        cwd: str,
        command: str,
        resume_session_key: str | None = None,
        pty_session_id: str | None = None,
        switch_prepared_session: bool = False,
        http_transport: httpx.AsyncBaseTransport | None = None,
        resident_output_sink: ResidentOutputSink | None = None,
        event_sink: EngineEventSink | None = None,
        filesystem: Any = None,
        search_file_paths: Callable[[str, str], Awaitable[list[str]]] | None = None,
        session_root: str | None = None,
        subagent_temp_root: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._resident_output_sink = resident_output_sink
        self._event_sink = event_sink
        #: The one reader of this process's wire, started with the process.
        self._relay: ResidentRelay | None = None
        self._platform_session_id = str(platform_session_id)
        self._cwd = str(cwd)
        self._command = str(command)
        self._resume_session_key = str(resume_session_key or "").strip() or None
        self._configured_pty_session_id = str(pty_session_id or "").strip() or None
        self._switch_prepared_session = switch_prepared_session
        self._filesystem = filesystem
        self._search_file_paths = search_file_paths
        self._session_root = session_root
        self._http_transport = http_transport
        self._process: PiRpcProcess | None = None
        self._engine_session_key: str | None = None
        self._closed = False
        self._input_lock = asyncio.Lock()
        self._commands: dict[str, EngineInputCommand] = {}
        self._pending_command_ids: deque[str] = deque()
        self._consumed_command_ids: set[str] = set()
        self._unreported_consumed_command_ids: deque[str] = deque()
        self._delivery_sequence = 0
        self._active_receipt: EngineTurnReceipt | None = None
        self._abort_request: asyncio.Task[dict[str, Any]] | None = None
        self._active_command_id: str | None = None
        self._extension_command_id: str | None = None
        self._extension_acknowledged = False
        self._extension_agent_started = False
        self._extension_terminal: dict[str, Any] | None = None
        self._command_prelude: deque[PiWireRecord] = deque()
        #: Kept across a parked interaction so the continuation resumes the
        #: same stream state instead of reopening blocks that never closed.
        self._translator: PiTurnTranslator | None = None
        self._child_resources = PiChildResources()
        self._streaming = False
        self._child_transcript = (
            PiChildTranscriptCapture(filesystem=filesystem, session_root=session_root,
                                     temp_root=subagent_temp_root, sink=event_sink)
            if filesystem is not None and session_root and subagent_temp_root and event_sink is not None else None
        )

    # ── identity ─────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return not self._closed and (
            self._process is None or self._process.fatal is None
        )

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    @property
    def active_receipt(self) -> EngineTurnReceipt | None:
        return self._active_receipt

    # ── process ──────────────────────────────────────────────────────────
    async def _ensure_process(self) -> PiRpcProcess:
        if self._process is not None and self._process.is_connected:
            return self._process
        process = PiRpcProcess(
            endpoint=self._endpoint,
            cwd=self._cwd,
            command=self._command,
            pty_session_id=self._configured_pty_session_id,
            http_transport=self._http_transport,
        )
        await process.connect(since=0)
        self._process = process
        self._configured_pty_session_id = process.pty_session_id
        if self._switch_prepared_session:
            await self._resume_prepared_session(process)
            self._switch_prepared_session = False
        await self._adopt_session_identity(process)
        await self._start_relay(process)
        return process

    async def _resume_prepared_session(self, process: PiRpcProcess) -> None:
        """Use Pi's native switch before the parked child accepts user input."""
        if (
            not self._resume_session_key or not self._session_root
            or self._filesystem is None or self._search_file_paths is None
        ):
            raise PiProtocolError("prepared resume has no native Session file source")
        matches = []
        for path in await self._search_file_paths(
            self._session_root, f"*_{self._resume_session_key}.jsonl",
        ):
            content = await self._filesystem.read_file(path)
            first_line = str(content).splitlines()[0] if content else ""
            if not first_line:
                continue
            header = json.loads(first_line)
            if header.get("type") == "session" and header.get("id") == self._resume_session_key:
                matches.append(path)
        if len(matches) != 1:
            raise PiProtocolError(
                f"prepared resume requires one file for {self._resume_session_key!r}, "
                f"found {len(matches)}"
            )
        switched = await self._request(process, {
            "type": "switch_session", "sessionPath": matches[0],
        })
        if (switched.get("data") or {}).get("cancelled") is not False:
            raise PiProtocolError("pi did not confirm the prepared Session switch")

    async def _start_relay(self, process: PiRpcProcess) -> None:
        relay = self._relay
        if relay is not None:
            await relay.stop()
        relay = ResidentRelay(
            seam=_PiRelaySeam(self),
            session_id=self._platform_session_id,
            engine_session_key=self._engine_session_key,
            next_record=process.next_record,
            send_command=lambda payload: self._request(process, payload),
            current_sequence=process.current_output_offset,
            resident_output_sink=self._resident_output_sink,
            event_sink=self._event_sink,
        )
        self._relay = relay
        relay.start()

    async def _next_turn_record(self) -> PiWireRecord:
        """The next record of the platform's own turn, from the relay.

        The relay is the wire's one reader; a turn takes what it was handed.
        An exception it forwards is the process's, in wire order.
        """

        if self._command_prelude:
            return self._command_prelude.popleft()
        relay = self._relay
        if relay is None:
            raise EngineStreamDetached("pi relay is not running")
        item = await relay.turn_inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def _adopt_session_identity(self, process: PiRpcProcess) -> None:
        """Learn which native session this process runs, and prove it.

        ``get_state`` doubles as the readiness probe: pi announces nothing
        when it comes up, so the first answer it gives is the evidence that it
        can answer at all.
        """

        state = await self._request(process, {"type": "get_state"})
        data = state.get("data")
        if not isinstance(data, dict):
            raise PiProtocolError("pi get_state returned no state")
        session_id = str(data.get("sessionId") or "").strip()
        if not session_id:
            raise PiProtocolError("pi get_state returned no sessionId")
        if self._resume_session_key and session_id != self._resume_session_key:
            raise PiProtocolError(
                "pi resumed a different native conversation: "
                f"expected={self._resume_session_key!r} actual={session_id!r}"
            )
        self._engine_session_key = session_id
        self._streaming = bool(data.get("isStreaming"))
        if self._child_transcript is not None:
            session_file = data.get("sessionFile")
            if not isinstance(session_file, str) or not session_file:
                raise PiProtocolError("pi get_state returned no sessionFile")
            self._child_transcript.bind_owner(session_file)
            history = await self._request(process, {"type": "get_entries"})
            history_data = history.get("data")
            if not isinstance(history_data, dict) or not isinstance(history_data.get("entries"), list):
                raise PiProtocolError("pi get_entries returned no native history")
            for message in history_data["entries"]:
                if isinstance(message, dict):
                    for diagnostic in await self._child_transcript.observe(message):
                        logger.warning("Pi child transcript restore: %s", diagnostic["data"]["raw"])

    @staticmethod
    async def _request(
        process: PiRpcProcess,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        timeout: float = _COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Issue one pi command and refuse a rejection loudly.

        ``success: false`` is pi declining the command before acceptance, and
        it carries the reason. Passing that back as if it had been accepted is
        how a turn goes silent instead of reporting a missing credential.
        """

        command_id = request_id or f"astrabox-{time.monotonic_ns():x}"
        response = await process.command(command_id, payload, timeout=timeout)
        if not response.get("success"):
            raise PiProtocolError(
                f"pi refused {str(payload.get('type') or 'command')!r}: "
                f"{response.get('error') or 'no reason given'}"
            )
        return response

    # ── conversation binding ─────────────────────────────────────────────
    async def bind_conversation(self, binding: EngineConversationBinding) -> None:
        if binding.platform_session_id != self._platform_session_id:
            raise RuntimeError(
                "pi conversation identity mismatch: "
                f"client={self._platform_session_id!r} "
                f"binding={binding.platform_session_id!r}"
            )
        durable_key = str(binding.engine_session_key or "").strip() or None
        if durable_key != self._resume_session_key:
            raise RuntimeError(
                "pi resume key does not match the durable conversation: "
                f"configured={self._resume_session_key!r} durable={durable_key!r}"
            )
        await self._ensure_process()
        if durable_key and self._engine_session_key != durable_key:
            raise RuntimeError(
                "pi resumed a different native conversation: "
                f"expected={durable_key!r} actual={self._engine_session_key!r}"
            )

    # ── input FIFO ───────────────────────────────────────────────────────
    @staticmethod
    def _input_consumed_frame(command: EngineInputCommand) -> dict[str, Any]:
        return {
            "type": "data-input-consumed",
            "id": f"input-consumed:{command.input_id}",
            "transient": True,
            "data": {
                "inputId": command.input_id,
                "responseMessageId": input_response_message_id(command.input_id),
                "content": command.content,
            },
        }

    async def deliver(self, command: EngineInputCommand) -> None:
        """Accept a platform command into the per-conversation FIFO.

        Pi owns a native follow-up queue while an agent run is active.  A
        delivery accepted in that window must enter it here: the platform does
        not start a second turn consumer for ``SubmitInput``, because Pi drains
        the follow-up before the active run's single ``agent_settled``.
        """

        if self._closed:
            raise EngineStreamDetached("pi client is closed")
        if command.session_id != self._platform_session_id:
            raise RuntimeError(
                "pi input belongs to another conversation: "
                f"client={self._platform_session_id!r} "
                f"command={command.session_id!r}"
            )
        if command.sequence <= 0:
            raise ValueError("pi input sequence must be positive")
        input_response_message_id(command.input_id)
        submit_while_active = False
        async with self._input_lock:
            existing = self._commands.get(command.command_id)
            if existing is not None:
                if existing != command:
                    raise RuntimeError(
                        "pi input command identity collided with another payload"
                    )
                return
            if command.sequence <= self._delivery_sequence:
                raise RuntimeError(
                    "pi input commands are not a strict FIFO: "
                    f"last={self._delivery_sequence} next={command.sequence}"
                )
            self._delivery_sequence = command.sequence
            self._commands[command.command_id] = command
            self._pending_command_ids.append(command.command_id)
            submit_while_active = self._streaming and self._active_receipt is not None
        if submit_while_active:
            await self._submit_fifo_head(
                expected_command_id=command.command_id,
                activate=False,
            )

    async def _is_extension_command(self, process: PiRpcProcess, content: str) -> bool:
        if not content.startswith("/"):
            return False
        name = content[1:].split(" ", 1)[0]
        response = await self._request(process, {"type": "get_commands"})
        data = response.get("data")
        commands = data.get("commands") if isinstance(data, dict) else None
        if not isinstance(commands, list):
            raise PiProtocolError("pi get_commands returned no command catalog")
        return any(
            isinstance(command, dict)
            and command.get("name") == name
            and command.get("source") == "extension"
            for command in commands
        )

    async def _submit_extension_command(
        self, process: PiRpcProcess, command: EngineInputCommand, payload: dict[str, Any]
    ) -> None:
        # A handler may await a dialog before acknowledging its prompt. The
        # relay must collect that request while begin_delivery is still waiting
        # for native consumption evidence, not wait for an agent_start that a
        # command need never emit. No command waiter imposes an RPC deadline on
        # the human's answer; the correlated response stays on the same wire.
        self._extension_command_id = command.command_id
        assert self._relay is not None
        self._relay.platform_turn_reattached()
        await process.send_untracked({**payload, "id": command.command_id})
        prelude: list[PiWireRecord] = []
        while True:
            wire = await self._next_turn_record()
            prelude.append(wire)
            record = wire.record
            if record.get("type") == "response" and record.get("id") == command.command_id:
                if not record.get("success"):
                    raise PiProtocolError(f"pi refused extension command: {record.get('error')}")
                break
            if record.get("type") == "agent_start" or (
                record.get("type") == "extension_ui_request"
                and record.get("method") in _DIALOG_METHODS
            ):
                # This command's handler is executing and waiting on the
                # client. Requiring its final ACK first would deadlock it.
                break
        self._command_prelude.extend(prelude)

    async def _submit_fifo_head(
        self,
        *,
        expected_command_id: str,
        activate: bool = True,
    ) -> EngineInputCommand:
        async with self._input_lock:
            if not self._pending_command_ids:
                raise RuntimeError("pi FIFO head disappeared before dispatch")
            command_id = self._pending_command_ids[0]
            if command_id != expected_command_id:
                raise RuntimeError(
                    "pi input consumption is not the FIFO head: "
                    f"head={command_id!r} requested={expected_command_id!r}"
                )
            command = self._commands[command_id]
            try:
                process = await self._ensure_process()
                payload: dict[str, Any] = {"type": "prompt", "message": command.content}
                if self._streaming:
                    # Pi refuses a prompt sent mid-stream without a queueing
                    # behavior. Its followUp queue is drained before the active
                    # agent_settled; steer would instead cut into the answer
                    # already in flight.
                    payload["streamingBehavior"] = "followUp"
                extension_command = activate and await self._is_extension_command(process, command.content)
                if self._relay is not None:
                    self._relay.platform_input_submitted()
                try:
                    if extension_command:
                        await self._submit_extension_command(process, command, payload)
                    else:
                        await self._request(process, payload, request_id=command.command_id)
                except BaseException:
                    if self._relay is not None:
                        self._relay.platform_input_rejected()
                        if self._extension_command_id is not None:
                            self._relay.platform_turn_finished()
                    self._extension_command_id = None
                    raise
            except BaseException:
                # A submit that raised never proved consumption, and consumption
                # is proven by the engine's own boundary frame rather than by
                # this call returning. Leaving the command queued makes it the
                # head forever: the next turn arrives with its own command,
                # finds someone else at the front, and is refused — so one lost
                # sandbox silences the conversation for good. The durable FIFO
                # is the authority on what is owed and redelivers it.
                self._pending_command_ids.popleft()
                raise
            self._pending_command_ids.popleft()
            self._consumed_command_ids.add(command_id)
            self._unreported_consumed_command_ids.append(command_id)
            if activate:
                self._active_command_id = command_id
            self._streaming = True
            return command

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        if self._closed:
            raise EngineStreamDetached("pi client is closed")
        if self._active_receipt is not None:
            if self._active_command_id == command.command_id:
                return self._active_receipt
            raise RuntimeError("pi already has an active turn")
        if command.command_id not in self._commands:
            if consumption_confirmed:
                self._commands[command.command_id] = command
                self._delivery_sequence = max(self._delivery_sequence, command.sequence)
            else:
                await self.deliver(command)
        # Queued above, and everything from here to the submit can fail against
        # a sandbox that is gone. A command left in the queue becomes its head
        # forever: the next turn arrives with its own command, is told it is not
        # the head, and the conversation never speaks again — so one lost box
        # would be permanent. Nothing here proves consumption, which only the
        # engine's own boundary frame does, so the durable FIFO still owes this
        # input and redelivers it.
        try:
            process = await self._ensure_process()
            if consumption_confirmed:
                with contextlib.suppress(ValueError):
                    self._pending_command_ids.remove(command.command_id)
                self._active_command_id = command.command_id
                self._consumed_command_ids.add(command.command_id)
                if await self._is_extension_command(process, command.content):
                    self._extension_command_id = command.command_id
                if self._relay is not None:
                    self._relay.platform_turn_reattached()
            else:
                await self._submit_fifo_head(expected_command_id=command.command_id)
        except BaseException:
            async with self._input_lock:
                with contextlib.suppress(ValueError):
                    self._pending_command_ids.remove(command.command_id)
            raise
        assert process.pty_session_id is not None
        assert self._engine_session_key is not None
        receipt = EngineTurnReceipt(
            engine_turn_id=encode_turn_anchor(
                pty_session_id=process.pty_session_id,
                pi_session_id=self._engine_session_key,
                command_id=command.command_id,
            ),
            engine_session_key=self._engine_session_key,
            started_at_monotonic_ns=time.monotonic_ns(),
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )
        self._active_receipt = receipt
        self._translator = PiTurnTranslator(session_id=self._engine_session_key)
        return receipt

    # ── streaming ────────────────────────────────────────────────────────
    async def iter_turn_events(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[EngineTurnEmission]:
        process = self._process
        if process is None or not process.is_connected:
            raise EngineStreamDetached("pi process is not connected")
        translator = self._translator
        if translator is None:
            raise RuntimeError("pi turn has no translator; begin_delivery first")

        for consumed in self._unreported_consumption_emissions():
            yield consumed

        while True:
            wire = await self._next_turn_record()
            record = wire.record
            record_type = str(record.get("type") or "").strip()

            if self._extension_command_id is not None:
                if record_type == "agent_start":
                    self._extension_agent_started = True
                if record_type == "extension_error":
                    raise PiProtocolError(f"pi extension command failed: {record.get('error')}")
                if record_type == "response" and record.get("id") == self._extension_command_id:
                    if not record.get("success"):
                        raise PiProtocolError(f"pi refused extension command: {record.get('error')}")
                    self._extension_acknowledged = True
                    yield emission_from_translated_frame(raw_event_frame("prompt", record))
                    if not self._extension_agent_started or self._extension_terminal is not None:
                        terminal = self._extension_terminal or {
                            "type": "result", "finishReason": "stop",
                        }
                        self._settle_turn()
                        yield emission_from_translated_frame(terminal)
                        return
                    continue

            if self._child_transcript is not None:
                for diagnostic in await self._child_transcript.observe(record):
                    yield emission_from_translated_frame(diagnostic)

            # A concurrent SubmitInput wakes this stream with Pi's queue update
            # or the queued user-message boundary.  Report the correlated
            # prompt acknowledgement before any response frame for that input.
            for consumed in self._unreported_consumption_emissions():
                yield consumed

            if record_type == "extension_ui_request":
                method = str(record.get("method") or "").strip()
                if method in _DIALOG_METHODS:
                    yield emission_from_translated_frame(
                        _interaction_frame(record)
                    )
                    # Parked: pi is blocked on the answer, and the translator
                    # stays alive so the continuation resumes this same turn.
                    return
                # Fire-and-forget. Waiting on one would park the turn for a
                # reply pi never reads.
                if is_async_status_widget(record) or is_inspect_widget(record):
                    # The Agents panel owns both sub-agent widgets: the status
                    # snapshot the package repushes about once a second, and
                    # the inspect reply it emits and retracts. A raw card
                    # beside either would repeat the same run every tick, and
                    # the package tells hosts not to render the reply at all.
                    facts = (
                        self._child_resources.observe_ui_request(record)
                        if is_async_status_widget(record)
                        else self._child_resources.observe_inspect_reply(record)
                    )
                    for child in facts:
                        yield emission_from_translated_frame(child)
                    # A child the snapshot just changed is read for its own
                    # words. The command is an extension command, which pi's
                    # RPC guide says "executes immediately even during
                    # streaming", so it runs inside this turn without a model
                    # turn of its own; the reply lands on this same stream.
                    for request_id, reference in self._child_resources.inspect_requests():
                        await self._request(
                            process,
                            {
                                "type": "prompt",
                                "message": inspect_command_line(reference, request_id),
                            },
                        )
                    continue
                yield emission_from_translated_frame(raw_event_frame(method, record))
                continue

            if record_type == "response":
                # No command was waiting for it; the pipe reports rather than
                # drops, so it lands in the record as a diagnostic.
                yield emission_from_translated_frame(
                    raw_event_frame("unmatched_response", record)
                )
                continue

            if record_type == "entry_appended":
                # Extension SessionStore writes are not model-loop events.
                # A command may append after its model settles, before its ACK.
                yield emission_from_translated_frame(raw_event_frame(record_type, record))
                continue

            for child in self._child_resources.observe_tool_event(record):
                yield emission_from_translated_frame(child)
            # A blocking launch reports its children on the tool call itself,
            # and they are owed the same read as one the widget announced.
            for request_id, reference in self._child_resources.inspect_requests():
                await self._request(
                    process,
                    {"type": "prompt", "message": inspect_command_line(reference, request_id)},
                )
            frames = list(translator.translate(record))
            if translator.terminal_seen:
                if self._extension_command_id is not None and not self._extension_acknowledged:
                    # A handler can await its own model run before returning.
                    # Both native boundaries must finish, in either order.
                    for frame in frames:
                        if frame.get("type") == "result":
                            self._extension_terminal = frame
                        else:
                            yield emission_from_translated_frame(frame)
                    continue
                if self._abort_request is not None:
                    # Pi can report an aborted lazy model setup as an error.
                    # Only an acknowledged platform abort marks cancellation; an
                    # RPC rejection or an unsolicited engine error still fails.
                    await asyncio.shield(self._abort_request)
                    for frame in frames:
                        if frame.get("type") == "result":
                            frame["finishReason"] = "cancelled"
                            frame.pop("error", None)
                # Settle BEFORE the terminal frame is yielded. The consumer
                # stops iterating as soon as it has that frame, which closes
                # this generator — anything after the yield never runs, and
                # the turn slot would stay held. The next message in the same
                # conversation then fails with "pi already has an active
                # turn", which is a defect only a second turn can show.
                self._settle_turn()
            for frame in frames:
                yield emission_from_translated_frame(frame)
            if translator.terminal_seen:
                return

    def _unreported_consumption_emissions(self) -> list[EngineTurnEmission]:
        emissions: list[EngineTurnEmission] = []
        while self._unreported_consumed_command_ids:
            command_id = self._unreported_consumed_command_ids.popleft()
            command = self._commands.get(command_id)
            if command is None:
                raise RuntimeError(
                    "pi accepted input disappeared before its consumption boundary "
                    f"command={command_id!r}"
                )
            emissions.append(
                emission_from_translated_frame(self._input_consumed_frame(command))
            )
        return emissions

    def _settle_turn(self) -> None:
        if self._pending_command_ids:
            raise RuntimeError(
                "pi settled with accepted FIFO inputs that never entered its "
                f"native queue: {list(self._pending_command_ids)!r}"
            )
        if self._unreported_consumed_command_ids:
            raise RuntimeError(
                "pi settled before reporting every consumed FIFO input"
            )
        self._active_receipt = None
        if self._extension_command_id is not None and self._relay is not None:
            self._relay.platform_turn_finished()
        self._extension_command_id = None
        self._extension_acknowledged = False
        self._extension_agent_started = False
        self._extension_terminal = None
        self._abort_request = None
        self._translator = None
        self._streaming = False
        self._active_command_id = None
        for command_id in tuple(self._consumed_command_ids):
            self._commands.pop(command_id, None)
        self._consumed_command_ids.clear()

    # ── interaction ──────────────────────────────────────────────────────
    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        """Encode the console's answer the way pi's dialog expects it."""

        _ = receipt
        process = self._process
        if process is None or not process.is_connected:
            raise EngineStreamDetached("pi process is not connected")
        raw_input = pending.get("raw_input")
        request_id = str((raw_input or {}).get("id") or "").strip()
        if not request_id:
            raise PiProtocolError("pending pi interaction carries no request id")
        method = str((raw_input or {}).get("method") or "").strip()

        rows = validated_question_answer_rows(pending, response)
        answer_text = str(rows[0]["response_text"])

        reply: dict[str, Any] = {
            "type": "extension_ui_response",
            "id": request_id,
        }
        if method == "confirm":
            # Pi's confirm answers with a boolean, not with the label the
            # console showed.
            if answer_text not in {_CONFIRM_YES, _CONFIRM_NO}:
                raise PiProtocolError("pi confirm requires one of its boolean choices")
            reply["confirmed"] = answer_text == _CONFIRM_YES
        else:
            if method == "select" and answer_text not in (raw_input or {}).get("options", []):
                raise PiProtocolError("pi select requires one of its declared choices")
            reply["value"] = answer_text
        await process.send_untracked(reply)
        return True

    # ── stopping ─────────────────────────────────────────────────────────
    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        if self._active_receipt != receipt:
            return False
        return await self.interrupt_active_turn()

    async def interrupt_active_turn(self) -> bool:
        process = self._process
        if process is None or not process.is_connected or self._active_receipt is None:
            return False
        if self._abort_request is None:
            self._abort_request = asyncio.create_task(self._request(process, {"type": "abort"}))
        await asyncio.shield(self._abort_request)
        return True

    # ── capabilities / teardown ──────────────────────────────────────────
    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            # The tool names pi sends on the wire for a write, whose input
            # carries `path` and `content`.
            tools=[],
            # Pi ships no permission system. It says so itself and recommends
            # containment instead, which is what the sandbox already is.
            # Declaring modes the engine does not have would put a control in
            # the console that changes nothing.
            permission_modes=[],
            supports_interaction=True,
            # The image pins a sub-agent package whose stop is a command, and
            # pi executes an extension command sent as a prompt even mid-turn.
            supports_child_run_control=True,
            supports_server_info=False,
        )

    async def stop_child_run(self, control_id: str) -> None:
        """Stop one child through the package's own command.

        Pi's RPC has no per-child abort — its `abort` ends the whole turn,
        which would take the parent down with the child. What it does have is
        stated in its RPC guide: an extension command sent as a prompt
        "executes immediately even during streaming". That is the only door
        that opens on one child while the turn it lives in keeps running.
        """

        process = await self._ensure_process()
        await self._request(
            process, {"type": "prompt", "message": stop_command_line(control_id)}
        )

    async def park_process(self) -> str:
        """Start the child and prove it answers; return the pipe holding it.

        Preparation's whole purpose: after this returns, the expensive part of
        a start — pipe creation, the child's boot, and the first ``get_state``
        it can answer — is already paid, and a later claim attaches to the pipe
        named here instead of repeating it.
        """

        process = await self._ensure_process()
        pty_session_id = str(process.pty_session_id or "").strip()
        if not pty_session_id:
            raise RuntimeError("parked pi process reported no pipe id")
        return pty_session_id

    async def release_without_stopping(self) -> None:
        """Drop this client's transport, leaving the child and pipe running.

        ``close`` already detaches rather than killing, but it also marks the
        client closed; this says the same thing in the caller's vocabulary, so
        a reader of the preparation path does not have to know that closing is
        safe here.
        """

        await self.close()

    async def close(self) -> None:
        self._closed = True
        relay = self._relay
        self._relay = None
        if relay is not None:
            await relay.stop()
        process = self._process
        self._process = None
        if process is not None:
            with contextlib.suppress(BaseException):
                await process.detach()

    async def dispose(self) -> bool:
        """Final teardown: destroy the PTY session behind this conversation."""

        self._closed = True
        relay = self._relay
        self._relay = None
        if relay is not None:
            await relay.stop()
        process = self._process
        self._process = None
        if process is None:
            return False
        with contextlib.suppress(BaseException):
            await self._request(process, {"type": "abort"}, timeout=5.0)
        return await process.delete()


class _PiRelaySeam:
    """Pi's answers to the relay, taken from its own RPC vocabulary.

    A model run opens with ``agent_start`` and is over at ``agent_settled`` — the
    event pi documents as the one after which it "will not continue running
    automatically"; ``agent_end`` is not it, since a retry or a queued
    follow-up may still follow. Records are ordered by the pipe's output
    offset, which is pi's own stdout position and survives a reconnect. The
    client owns extension-command completion, which also awaits its prompt ACK.
    """

    engine_kind = ENGINE_KIND

    def __init__(self, client: PiEngineClient) -> None:
        self._client = client
        self._natives: list[dict[str, Any]] = []
        self._latest_child_snapshot: dict[str, Any] | None = None

    @staticmethod
    def sequence(wire: PiWireRecord) -> int:
        return int(wire.output_offset)

    @staticmethod
    def record(wire: PiWireRecord) -> dict[str, Any]:
        return wire.record

    @staticmethod
    def starts_run(record: dict[str, Any]) -> bool:
        return str(record.get("type") or "") == "agent_start"

    def settles_run(self, record: dict[str, Any]) -> bool:
        return (
            self._client._extension_command_id is None
            and str(record.get("type") or "") == "agent_settled"
        )

    def response_id(self, record: dict[str, Any], sequence: int) -> str:
        # Pi names no run; the native session plus the run's own position on
        # pi's stdout is the identity, unique per session and stable across
        # a reconnect that replays the same bytes.
        key = str(self._client.engine_session_key or "").strip()
        if not key:
            raise PiProtocolError("pi run began before the native session was known")
        return f"{key}:{sequence}"

    def new_translator(self) -> PiTurnTranslator:
        return PiTurnTranslator(session_id=self._client.engine_session_key)

    def translate(
        self, translator: PiTurnTranslator, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        record_type = str(record.get("type") or "").strip()
        if record_type == "extension_ui_request":
            method = str(record.get("method") or "").strip()
            if method in _DIALOG_METHODS or is_async_status_widget(record) or is_inspect_widget(record):
                # The dialog is an interaction and the widgets are child
                # facts; both are read by their own seam methods.
                return []
            return [raw_event_frame(method, record)]
        if record_type == "response":
            return [raw_event_frame("unmatched_response", record)]
        return list(translator.translate(record))

    async def child_facts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        resources = self._client._child_resources
        diagnostics = (
            await self._client._child_transcript.observe(record)
            if self._client._child_transcript is not None else []
        )
        if str(record.get("type") or "") == "extension_ui_request":
            if is_async_status_widget(record):
                frames = resources.observe_ui_request(record)
                self._latest_child_snapshot = dict(record)
            elif is_inspect_widget(record):
                frames = resources.observe_inspect_reply(record)
                if frames and self._latest_child_snapshot is not None:
                    # A single-step alias may arrive without a lifecycle change.
                    # Keep its native mapping before the reply for cold replay.
                    self._natives.append(self._latest_child_snapshot)
            else:
                frames = []
        else:
            frames = resources.observe_tool_event(record)
        if frames:
            # The record itself is what the adapter's durable fold reads back.
            self._natives.append(dict(record))
        return [*frames, *diagnostics]

    def native_records(self) -> list[dict[str, Any]]:
        natives, self._natives = self._natives, []
        return natives

    def owed_child_reads(self) -> list[dict[str, Any]]:
        return [
            {"type": "prompt", "message": inspect_command_line(reference, request_id)}
            for request_id, reference in self._client._child_resources.inspect_requests()
        ]

    @staticmethod
    def carries_child_facts(record: dict[str, Any]) -> bool:
        return is_async_status_widget(record) or is_inspect_widget(record)

    @staticmethod
    def interaction(record: dict[str, Any]) -> dict[str, Any] | None:
        if str(record.get("type") or "") != "extension_ui_request":
            return None
        if str(record.get("method") or "").strip() not in _DIALOG_METHODS:
            return None
        return _interaction_frame(record)


async def connect_pi_client(
    sandbox: Any,
    *,
    platform_session_id: str,
    cwd: str,
    command: str,
    resume_session_key: str | None = None,
    pty_session_id: str | None = None,
    switch_prepared_session: bool = False,
    port: int = EXECD_PORT,
    service_credential: str | None = None,
    resident_output_sink: ResidentOutputSink | None = None,
    event_sink: EngineEventSink | None = None,
    session_root: str,
    subagent_temp_root: str,
) -> PiEngineClient:
    """Resolve the sandbox's execd endpoint and build a client against it.

    ``pty_session_id`` names a pipe that already holds an initialized pi — a
    prepared unit's parked child. The client then attaches instead of creating,
    which is what moves the child's whole startup off the claim path; without
    it the first use creates the pipe and pays that startup inline.
    """

    endpoint = await resolve_sandbox_endpoint(sandbox, port)
    if port != EXECD_PORT:
        if not service_credential:
            raise RuntimeError("isolated pi service has no platform control credential")
        endpoint = ResolvedExecdEndpoint(
            origin=endpoint.origin,
            headers={**endpoint.headers, "X-EXECD-ACCESS-TOKEN": service_credential},
        )
    filesystem_headers = (
        {"X-EXECD-ACCESS-TOKEN": service_credential} if port != EXECD_PORT else None
    )
    return PiEngineClient(
        endpoint=endpoint,
        platform_session_id=platform_session_id,
        cwd=cwd,
        command=command,
        resume_session_key=resume_session_key,
        pty_session_id=pty_session_id,
        switch_prepared_session=switch_prepared_session,
        resident_output_sink=resident_output_sink,
        event_sink=event_sink,
        session_root=session_root,
        subagent_temp_root=subagent_temp_root,
        filesystem=await sandbox.get_filesystem(
            port, headers=filesystem_headers,
        ),
        search_file_paths=lambda path, pattern: sandbox.search_file_paths(
            path, pattern, port=port, headers=filesystem_headers,
        ),
    )


__all__ = [
    "ENGINE_KIND",
    "PiEngineClient",
    "connect_pi_client",
    "decode_turn_anchor",
    "encode_turn_anchor",
]
