"""In-box runner — the translation shell between the Claude SDK and the host.

Runs inside the sandbox image (copied standalone; see
``containers/sandbox-claude-code/Dockerfile``). It must import nothing from
the AstraBox host package graph — standard library plus ``claude_agent_sdk``
only. Host-side code imports this module only in tests.

Duties (docs/design-translation-shell-2026-07.md):

* hold one ``ClaudeSDKClient`` streaming session; accept host-delivered inputs
  immediately, then feed each one into ``client.query()`` after the preceding
  SDK Result so a later prompt cannot steer an unfinished response;
* forward every typed SDK message verbatim to the host under a
  ``(session_id, seq)`` envelope; ``seq`` is one monotonic counter across all
  runner→host frames so the host can detect gaps and refill from the store;
* gate ordinary tools through a ``PreToolUse`` hook that forwards to the host
  and awaits the user's decision. On answer it returns allow/deny; on
  wait-budget expiry or a dead host link it returns ``permissionDecision:
  "defer"`` — the run stops with the call carried in
  ``ResultMessage.deferred_tool_use`` and resume re-issues it under a fresh
  ``tool_use_id`` (proven by ``scripts/spikes/defer_resume_spike.py``; this is
  also why nothing here keys durable state on ``tool_use_id``). Claude Code's
  native ``AskUserQuestion`` is different: the CLI exposes and dispatches it
  only through the SDK's ``can_use_tool`` control channel. Its PreToolUse hook
  therefore passes through without a decision and the callback owns its one
  host interaction; any other callback is allowed immediately because the
  hook has already made that tool's platform decision. The price of the hook
  seam is a clock: the CLI runs a matcher under
  ``HookMatcher.timeout`` (vendor default 60s) and aborts the callback when it
  expires, which drops the gate instead of deciding it. So the matcher declares
  its own timeout and the broker always defers before that elapses;
* persist the transcript through a spool-then-flush ``SessionStore`` adapter:
  ``append`` acks the SDK after an fsync'd local write, a flusher drains
  batches to the platform store and deletes them on success. This sidesteps
  the SDK's best-effort mirror semantics (drop after 3 attempts) — host
  downtime queues instead of losing, and a runner restart re-flushes whatever
  is left in the spool directory.

The session state machine is the SDK's; this file deliberately has none. The
runner owns only transport bookkeeping: the envelope counter, spool directory,
in-flight interaction futures, and the delivery/consumption identities needed
to make the external durable FIFO idempotent.

Transport is injected (``HostLink``): unit tests drive the core directly, and
the websocket wiring arrives with the host-side EngineClient counterpart.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import secrets
import shlex
import time
import uuid as uuid_mod
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from types import UnionType
from typing import Any, Protocol, Union, cast, get_args, get_origin, get_type_hints

logger = logging.getLogger("astrabox.sandbox_runner")

#: The wire contract this runner speaks, self-declared in its hello frame.
#: An identity for equality matching only — the digit is the next unambiguous
#: name for an incompatible wire change, never a negotiable version: a host
#: that reads a different stamp is from another release era, and the box's
#: disposition is reclaim, not adaptation. Dot-delimited so the same token is
#: valid as a WebSocket subprotocol name if the check ever moves into the
#: RFC 6455 handshake. The host keeps its own copy (runner_link.py) because
#: this file ships standalone into the image; the wire test pins the two
#: copies equal.
RUNNER_PROTOCOL = "astrabox.runner-wire.v1"

#: An optional script an external provider or deployment termination trigger
#: may run when this box is torn down. The runner writes the content once it
#: learns the box-scoped URL. Writing the file does not activate notification:
#: current standard OpenSandbox create installs no termination trigger.
DEATH_NOTICE_SCRIPT = "/tmp/astrabox_death_notice.sh"

#: Seconds a pending tool approval keeps waiting after the host link has gone,
#: before the PreToolUse hook answers ``defer`` and lets the run stop cleanly.
#:
#: It is not a budget on the human. A person deciding whether to allow a tool
#: routinely takes longer than this, and a platform restart certainly does: as a
#: budget on the human it would defer a wait nobody had abandoned, and resolve
#: the eventual answer as INTERACTION_EXPIRED. The vendor's own model is that
#: ``can_use_tool`` blocks until it is answered; the only obligation on top of
#: that is not leaving a run pinned to a host that will never come back.
DEFAULT_INTERACTION_WAIT_S = 120.0

#: What the CLI is told a PreToolUse hook may take (``HookMatcher.timeout``).
#:
#: The vendor's default is 60 seconds — "Timeout in seconds for all hooks in
#: this matcher (default: 60)" in ``HookMatcher``. This gate is not a scripted
#: check that returns in milliseconds; it is a person deciding, so the default
#: expires the hook while the approval is still on someone's screen. The CLI
#: then aborts the callback, this module's ``finally`` drops the interaction,
#: and an answer that arrives afterwards resolves as ``INTERACTION_EXPIRED``.
#:
#: An hour is generous for a person and still finite, so a forgotten approval
#: cannot pin a run forever.
INTERACTION_HOOK_TIMEOUT_S = 3600.0

#: Margin by which the runner's wait gives up before the CLI would abort the hook.
#:
#: Both ends must not race: whoever ends the wait decides how it ends, and only
#: the runner's end can answer ``defer`` — the vendor-supported outcome that stops the
#: run cleanly with the call in ``ResultMessage.deferred_tool_use``. Letting the
#: CLI's timeout win instead produces an aborted callback and a dropped gate,
#: which is the same silent loss under a different clock.
_INTERACTION_HOOK_MARGIN_S = 60.0

#: How often the wait re-checks whether the host is still there. Small enough
#: that an answer is not delayed by the check itself — the poll bounds latency
#: on every approval, not just on the disconnected path.
_INTERACTION_LINK_POLL_S = 0.05


def _write_death_notice(notice: Any) -> None:
    """Lay down an optional box-scoped termination-notice script.

    The host sends ``{"url"}``, already scoped to the box: it is the box that is
    torn down, and which conversations that affects is the platform's lookup.
    So there is nothing to say in the body — the address is the whole message.
    Writing the script does not schedule it. Current standard OpenSandbox create
    leaves it inert unless an external provider or deployment trigger invokes it.
    Absent entirely is a legitimate state (a deployment with no reachable
    callback base configures boxes without one) and the probe sweep converges
    those the slow way.

    Failing to write is not fatal to the session: the box can serve turns
    perfectly well without being able to announce its own death. It is logged,
    never swallowed silently.
    """
    if not isinstance(notice, dict):
        return
    url = str(notice.get("url") or "").strip()
    if not url:
        logger.warning("death notice not written: no url")
        return
    script = (
        "#!/bin/sh\n"
        "# Written by the AstraBox runner when this box took its session.\n"
        "# A provider or deployment termination trigger may run this script.\n"
        f"exec curl -sS -m 5 -X POST {shlex.quote(url)} \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{}'\n"
    )
    try:
        path = Path(DEATH_NOTICE_SCRIPT)
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)
    except OSError as exc:
        logger.warning("death notice not written to %s: %s", DEATH_NOTICE_SCRIPT, exc)


class RunnerProtocolError(Exception):
    """A frame violated the runner protocol. Fail loud — no tolerant parsing."""


@dataclasses.dataclass(frozen=True, slots=True)
class HistoryStoreSequence:
    """A durable-store coordinate; deliberately not orderable with live seq."""

    value: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value < 0
        ):
            raise ValueError("history store sequence must be a non-negative int")


@dataclasses.dataclass(frozen=True, slots=True)
class HistoryLiveSequence:
    """A runner-journal coordinate; deliberately not orderable with store seq."""

    value: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value < 0
        ):
            raise ValueError("history live sequence must be a non-negative int")


@dataclasses.dataclass(frozen=True, slots=True)
class ResultHistoryCheckpoint:
    """The two independent coordinates observed at one SDK Result boundary."""

    store: HistoryStoreSequence
    live: HistoryLiveSequence

    def __post_init__(self) -> None:
        if not isinstance(self.store, HistoryStoreSequence):
            raise TypeError("checkpoint.store must be HistoryStoreSequence")
        if not isinstance(self.live, HistoryLiveSequence):
            raise TypeError("checkpoint.live must be HistoryLiveSequence")

    def wire_fields(self) -> dict[str, int]:
        return {
            "store_sequence": self.store.value,
            "history_live_sequence": self.live.value,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class JournalReplay:
    """One cursor lookup against the runner's retained journal window."""

    first_retained_sequence: int | None
    last_sequence: int
    gap: bool
    replayed: int


@dataclasses.dataclass(frozen=True, slots=True)
class _PendingPromptConsumption:
    input_id: str
    #: What ``UserPromptSubmit`` will hand back for this input. An input that
    #: carried images still reaches that hook as its text alone, so the FIFO
    #: head is matched on the text and never on the blocks.
    prompt: str


@dataclasses.dataclass(frozen=True, slots=True)
class DeliveryCommand:
    command_id: str
    session_id: str
    sequence: int
    sdk_input: dict[str, Any]


@dataclasses.dataclass(slots=True)
class _QueuedDelivery:
    command: DeliveryCommand
    accepted: asyncio.Future[None]


@dataclasses.dataclass(frozen=True, slots=True)
class _BufferedDelivery:
    command: DeliveryCommand
    sdk_input: dict[str, Any]
    pending_consumption: _PendingPromptConsumption


_STOP = object()


def _detached_exception(exc: Exception) -> Exception:
    """Do not share a live pump traceback with the submitter task."""

    try:
        return exc.__class__(*exc.args)
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {exc}")


#: prepare.options keys consumed by the runner itself, not the SDK.
RUNNER_OPTION_KEYS: frozenset[str] = frozenset({
    "interaction_wait_s",
    "resume_transcript",
})

# Select the image-pinned CLI through the SDK's native override. The SDK's
# bundled executable can lag the separately released Claude Code package.
CLAUDE_CLI_PATH = "/usr/local/bin/claude"

#: SDK constructor fields that this standalone runner, rather than the host,
#: owns or deliberately refuses. Every other field is accepted exactly when
#: the SDK installed in this image declares it; host and runner therefore do
#: not depend on one another's package graph or duplicate an allowlist.
_RUNNER_CONTROLLED_SDK_OPTION_KEYS: frozenset[str] = frozenset({
    "can_use_tool",
    "cli_path",
    "continue_conversation",
    "debug_stderr",
    "hooks",
    "permission_prompt_tool_name",
    "session_id",
    "session_store",
    "session_store_flush",
    "stderr",
    "user",
})


def _runner_engine_contract() -> dict[str, Any]:
    """Describe the Claude SDK surface actually installed in this image."""

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import PermissionMode

    accepted_option_keys = sorted(
        (
            {field.name for field in dataclasses.fields(ClaudeAgentOptions)}
            - _RUNNER_CONTROLLED_SDK_OPTION_KEYS
        )
        | set(RUNNER_OPTION_KEYS)
    )
    permission_modes = sorted(
        {
            str(mode).strip()
            for mode in get_args(PermissionMode)
            if str(mode).strip()
        }
    )
    if not permission_modes:
        raise RunnerProtocolError("Claude SDK declares no permission modes")
    try:
        sdk_version = package_version("claude-agent-sdk")
    except PackageNotFoundError as exc:
        raise RunnerProtocolError(
            "Claude SDK distribution metadata is unavailable"
        ) from exc
    return {
        "adapter": "claude_code",
        "sdk_version": sdk_version,
        "accepted_option_keys": accepted_option_keys,
        "permission_modes": permission_modes,
    }


def _validate_host_engine_requirements(
    requirements: Any,
    contract: dict[str, Any],
) -> None:
    """Reject a host/image mismatch before constructing the SDK session."""

    if not isinstance(requirements, dict):
        raise RunnerProtocolError("opening frame requires engine_requirements")
    if requirements.get("adapter") != contract["adapter"]:
        raise RunnerProtocolError(
            "engine adapter mismatch "
            f"host={requirements.get('adapter')!r} image={contract['adapter']!r}"
        )
    required_option_keys = requirements.get("required_option_keys")
    if not isinstance(required_option_keys, list) or any(
        not isinstance(key, str) or not key.strip() for key in required_option_keys
    ):
        raise RunnerProtocolError(
            "engine_requirements.required_option_keys must be a list of names"
        )
    missing = sorted(
        set(required_option_keys) - set(contract["accepted_option_keys"])
    )
    if missing:
        raise RunnerProtocolError(
            f"runner image does not accept required Claude options {missing!r}"
        )
    permission_mode = str(requirements.get("permission_mode") or "").strip()
    if not permission_mode:
        raise RunnerProtocolError("engine_requirements.permission_mode is required")
    if permission_mode not in contract["permission_modes"]:
        raise RunnerProtocolError(
            "runner image does not support required Claude permission mode "
            f"{permission_mode!r}"
        )

#: SDK messages whose live envelope is durable engine state rather than a
#: turn-scoped rendering detail. The host acknowledges each only after the
#: engine event has committed; until then the runner journal cannot compact it
#: behind a later Result checkpoint.
_PERSISTENT_SDK_MESSAGE_TYPES: frozenset[str] = frozenset({
    "HookEventMessage",
    "TaskStartedMessage",
    "TaskProgressMessage",
    "TaskNotificationMessage",
    "TaskUpdatedMessage",
})


def _jsonable(obj: Any) -> Any:
    """SDK dataclasses → JSON-safe structures, verbatim field names.

    Each dataclass is stamped with ``__sdk_type`` (its class name):
    ``dataclasses.asdict`` erases the type, and the host translator dispatches
    on it — content blocks (TextBlock/ThinkingBlock/ToolUseBlock/...) are
    indistinguishable by fields alone once nested."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {
            f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)
        }
        return {"__sdk_type": type(obj).__name__, **fields}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


class HostLink(Protocol):
    """Runner→host frame sink. One live link at a time; send never raises
    protocol-level errors (a dead link is reported by ``is_connected``).

    ``send`` returns whether the frame reached the socket. ``is_connected``
    cannot answer that on its own: a link discovers it is dead by failing a
    write, so the frame that discovers it is exactly the one a caller would
    wrongly believe it had delivered.
    """

    def is_connected(self) -> bool: ...

    async def send(self, frame: dict[str, Any]) -> bool: ...


class SdkSession(Protocol):
    """The slice of ``ClaudeSDKClient`` the runner drives (injectable)."""

    async def connect(self) -> None: ...

    async def query(
        self,
        prompt: Any,
        session_id: str = "default",
    ) -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def stop_task(self, task_id: str) -> None: ...

    async def set_permission_mode(self, mode: str) -> None: ...

    async def reconnect_mcp_server(self, server_name: str) -> None: ...

    async def get_mcp_status(self) -> dict[str, Any]: ...

    async def get_server_info(self) -> dict[str, Any] | None: ...

    async def disconnect(self) -> None: ...


class EnvelopeSender:
    """Single monotonic cursor journal across every runner→host frame.

    Every frame stays addressable by its original runner sequence until a
    Result checkpoint proves that the platform store covers the preceding
    interval. The size is a compaction trigger, never ``deque(maxlen=...)``:
    arbitrary suffix retention can start on a content-block delta whose start
    was discarded.
    """

    _JOURNAL_COMPACTION_THRESHOLD = 2048

    def __init__(self, link: HostLink, session_id: str) -> None:
        self._link = link
        self._session_id = session_id
        self._seq = 0
        self._lock = asyncio.Lock()
        self._journal: deque[dict[str, Any]] = deque()
        # An injected HostLink is immediately usable by the transport-neutral
        # core. RunnerWsServer explicitly re-attaches it before start so the
        # websocket variant can hold frames behind its hello handshake.
        self._link_ready = True
        self._last_sent_seq = 0
        self._history_checkpoint: ResultHistoryCheckpoint | None = None
        self._previous_result_store_sequence = HistoryStoreSequence(0)
        self._events_requiring_persistence: set[int] = set()
        self._persisted_event_sequences: set[int] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def undelivered_count(self) -> int:
        """Retained frames newer than the last frame this link accepted."""
        return sum(
            1 for frame in self._journal if int(frame["seq"]) > self._last_sent_seq
        )

    @property
    def first_retained_seq(self) -> int | None:
        return int(self._journal[0]["seq"]) if self._journal else None

    @property
    def journal_count(self) -> int:
        return len(self._journal)

    @property
    def history_checkpoint(self) -> ResultHistoryCheckpoint | None:
        return self._history_checkpoint

    @property
    def host_connected(self) -> bool:
        """Whether a host is listening right now. Reattach swaps the link in."""
        return self._link_ready and self._link.is_connected()

    @property
    def link(self) -> HostLink:
        """The host link this sender currently emits on."""
        return self._link

    def set_link(self, link: HostLink) -> None:
        """Reattach: the newest connection wins, unconditionally. There is no
        arbitration between an old and a new consumer — the old link is simply
        never written to again. A stale writer can only race a live one where
        two consumers are allowed at once; this sender never has two."""
        # Logged because a pending approval's budget runs on the host's absence:
        # when one expires, the diagnosis turns on whether a host came back and
        # when. Without this line the box's own log ends at "host link closed"
        # and says nothing about what followed.
        logger.info(
            "host link attached (seq=%d, journal=%d first=%s)",
            self._seq,
            len(self._journal),
            self.first_retained_seq,
        )
        self._link = link
        # No frame may overtake the replay selected by the attach cursor. Sends
        # keep journaling while hello is in flight, then replay includes them.
        self._link_ready = False
        self._last_sent_seq = 0

    def bind_prepared_session(self, link: HostLink, session_id: str) -> None:
        """Bind an unused prepared sender to its one platform Session.

        Preparation is not a Session and therefore must not leave a journal
        under the slot id.  The runner calls this exactly once, before it
        starts either event pump; refusing any prior sequence makes a partial
        activation disposable instead of letting its frames cross identities.
        """

        target = str(session_id or "").strip()
        if not target:
            raise RunnerProtocolError("activation missing session_id")
        if self._seq != 0 or self._journal:
            raise RunnerProtocolError(
                "prepared sender emitted frames before Session activation"
            )
        self._session_id = target
        self.set_link(link)

    def cursor_window(self, after_sequence: int) -> JournalReplay:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise RunnerProtocolError("last_seen_seq must be a non-negative int")
        if after_sequence > self._seq:
            raise RunnerProtocolError(
                "last_seen_seq is ahead of the runner journal "
                f"({after_sequence} > {self._seq})"
            )
        first = self.first_retained_seq
        gap = (
            (first is None and after_sequence < self._seq)
            or (first is not None and after_sequence < first - 1)
        )
        return JournalReplay(first, self._seq, gap, 0)

    async def replay_after(
        self,
        after_sequence: int,
        *,
        emit_gap: bool = False,
    ) -> JournalReplay:
        """Replay strictly after ``after_sequence``, or report one exact hole.

        The websocket attach path asks this method to emit the gap itself so
        the gap and retained suffix share this lock. Otherwise a Result could
        compact a second prefix between those two writes, invalidating the
        resume point the host had just received.
        """

        async with self._lock:
            window = self.cursor_window(after_sequence)
            if window.gap:
                if not emit_gap:
                    return window
                if not self._link.is_connected() or not await self._link.send({
                    "op": "gap",
                    "after_sequence": after_sequence,
                    "first_retained_sequence": window.first_retained_sequence,
                    "last_sequence": window.last_sequence,
                }):
                    self._link_ready = False
                    return window
                replay_cursor = (
                    window.first_retained_sequence - 1
                    if window.first_retained_sequence is not None
                    else window.last_sequence
                )
            else:
                replay_cursor = after_sequence
            self._last_sent_seq = replay_cursor
            replayed = 0
            for frame in self._journal:
                sequence = int(frame["seq"])
                if sequence <= replay_cursor:
                    continue
                if not self._link.is_connected() or not await self._link.send(frame):
                    self._link_ready = False
                    return dataclasses.replace(window, replayed=replayed)
                self._last_sent_seq = sequence
                replayed += 1
            self._link_ready = True
            if replayed:
                logger.info(
                    "replayed %d runner frame(s) after cursor %d",
                    replayed,
                    replay_cursor,
                )
            return dataclasses.replace(window, replayed=replayed)

    async def send(
        self,
        op: str,
        *,
        result_store_sequence: HistoryStoreSequence | None = None,
        **fields: Any,
    ) -> int:
        """Number, journal and optionally deliver one frame."""

        async with self._lock:
            is_result = (
                op == "event" and fields.get("message_type") == "ResultMessage"
            )
            if is_result != (result_store_sequence is not None):
                raise RunnerProtocolError(
                    "exactly ResultMessage events must carry a history checkpoint"
                )
            self._seq += 1
            seq = self._seq
            requires_persistence = fields.get("requires_persistence")
            if requires_persistence is not None and requires_persistence is not True:
                raise RunnerProtocolError(
                    "requires_persistence must be true when present"
                )
            if requires_persistence is True and op != "event":
                raise RunnerProtocolError(
                    "only SDK event frames can require host persistence"
                )
            frame = {"op": op, "seq": seq, "session_id": self._session_id, **fields}
            if result_store_sequence is not None:
                checkpoint = ResultHistoryCheckpoint(
                    store=result_store_sequence,
                    live=HistoryLiveSequence(seq),
                )
                frame.update(checkpoint.wire_fields())
                self._previous_result_store_sequence = (
                    self._history_checkpoint.store
                    if self._history_checkpoint is not None
                    else HistoryStoreSequence(0)
                )
                self._history_checkpoint = checkpoint
            self._journal.append(frame)
            if requires_persistence is True:
                self._events_requiring_persistence.add(seq)
            if self._link_ready and self._link.is_connected():
                if await self._link.send(frame):
                    self._last_sent_seq = seq
                else:
                    self._link_ready = False
            return seq

    async def acknowledge_event_persistence(self, sequence: int) -> None:
        """Record the host's commit receipt for one durable engine event."""

        async with self._lock:
            if sequence in self._persisted_event_sequences:
                return
            if sequence not in self._events_requiring_persistence:
                raise RunnerProtocolError(
                    f"persistence receipt names no durable event: {sequence}"
                )
            self._persisted_event_sequences.add(sequence)

    async def compact_after_result(
        self,
        result_sequence: int,
        *,
        store_fully_flushed: bool,
    ) -> int:
        """Drop only a complete, store-covered prefix before one Result."""

        async with self._lock:
            if (
                self._history_checkpoint is None
                or self._history_checkpoint.live.value != result_sequence
            ):
                raise RunnerProtocolError(
                    "journal compaction must name the latest Result checkpoint"
                )
            if (
                not store_fully_flushed
                or len(self._journal) <= self._JOURNAL_COMPACTION_THRESHOLD
            ):
                return 0
            return self._compact_result_prefix_locked()

    async def compact_after_store_flush(
        self,
        confirmed_store_sequence: HistoryStoreSequence,
    ) -> int:
        """Compact the latest Result once an asynchronous store drain covers it.

        The flusher and event pump are independent tasks. Resolve the latest
        checkpoint under the sender lock instead of carrying a Result sequence
        through the callback: a newer Result may have arrived while the drain
        callback was waiting for this lock.
        """

        if not isinstance(confirmed_store_sequence, HistoryStoreSequence):
            raise TypeError(
                "confirmed_store_sequence must be HistoryStoreSequence"
            )
        async with self._lock:
            checkpoint = self._history_checkpoint
            if checkpoint is None:
                return 0
            if confirmed_store_sequence.value < checkpoint.store.value:
                raise RunnerProtocolError(
                    "confirmed store sequence moved behind the latest Result "
                    f"checkpoint ({confirmed_store_sequence.value} < "
                    f"{checkpoint.store.value})"
                )
            if (
                confirmed_store_sequence.value
                <= self._previous_result_store_sequence.value
                or len(self._journal) <= self._JOURNAL_COMPACTION_THRESHOLD
            ):
                return 0
            return self._compact_result_prefix_locked()

    def _compact_result_prefix_locked(self) -> int:
        """Drop the safe prefix before the latest Result; caller holds the lock."""

        checkpoint = self._history_checkpoint
        assert checkpoint is not None
        result_sequence = checkpoint.live.value
        unpersisted = [
            sequence
            for sequence in self._events_requiring_persistence
            if sequence < result_sequence
            and sequence not in self._persisted_event_sequences
        ]
        compact_before = min(unpersisted) if unpersisted else result_sequence
        removed_sequences: list[int] = []
        while self._journal and int(self._journal[0]["seq"]) < compact_before:
            removed_sequences.append(int(self._journal.popleft()["seq"]))
        for sequence in removed_sequences:
            self._events_requiring_persistence.discard(sequence)
            self._persisted_event_sequences.discard(sequence)
        if removed_sequences:
            logger.info(
                "runner journal compacted: session=%s result_seq=%d "
                "store_seq=%d removed=%d first=%s",
                self._session_id,
                result_sequence,
                checkpoint.store.value,
                len(removed_sequences),
                self.first_retained_seq,
            )
        return len(removed_sequences)


class _PermanentFlushRejection(RuntimeError):
    """The platform refused a batch in a way that re-sending cannot change.

    A rejection of the request (the platform understood it and said no) is not
    the transient unreachability the spool exists to ride out. Retrying one is
    an infinite loop whose only observable effect is a log line every couple of
    seconds — the shape a stale sandbox image produces, which lets such a box
    keep serving turns for days while its transcript never advances.
    """


class SpoolSessionStore:
    """``SessionStore`` adapter: fsync-ack locally, flush to the platform.

    ``append`` writes the batch as one JSON file in ``spool_dir`` and returns
    — the SDK is acked as soon as the bytes are on the box's disk. A single
    flusher task posts batches (oldest first) through ``flush_fn`` and unlinks
    each file only on success; failures back off and retry forever. Durable
    writes are serialized but run on a worker thread, so filename order remains
    durability order without blocking the runner's event loop. Files are
    self-describing ``{key, entries, append_id}`` so a fresh runner
    over the same directory resumes flushing with no handshake. ``load``
    delegates to ``load_fn`` (the platform store is the durable truth a resume
    reads); pending spool batches for the same key are appended after it so a
    resume racing the flusher still sees its own tail. ``list_subkeys`` does the
    same union for subagent transcript names, because the SDK cannot restore
    child histories on a replacement host unless the store enumerates them.

    The ``append_id`` is minted here, on the fsync'd write, and not at flush
    time: it is the platform's idempotency key, so it must survive the same
    crash the spool file survives. A runner that regenerated the id on restart
    would turn every re-flush into a fresh batch — losing the guarantee exactly
    where it is needed.

    Filenames order the queue, so the counter behind them resumes above the
    highest name already in the directory. Starting it from zero after a restart
    would file new batches ahead of unflushed older ones and send the transcript
    out of order.

    """

    def __init__(
        self,
        spool_dir: str | os.PathLike[str],
        *,
        flush_fn: Callable[
            [dict[str, Any], list[dict[str, Any]], str], Awaitable[None]
        ],
        load_fn: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]] | None]],
        list_subkeys_fn: Callable[[dict[str, Any]], Awaitable[list[str]]],
        sequence_fn: Callable[[], int] | None = None,
        retry_delay_s: float = 2.0,
    ) -> None:
        self._dir = Path(spool_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._flush_fn = flush_fn
        self._load_fn = load_fn
        self._list_subkeys_fn = list_subkeys_fn
        self._sequence_fn = sequence_fn
        self._retry_delay_s = retry_delay_s
        self._counter = self._highest_spooled_ordinal()
        self._accepted_appends = 0
        self._append_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._drain_handler: Callable[[], Awaitable[None]] | None = None
        #: Set when the flusher stopped because the platform refuses a batch.
        #: The spool files stay on disk: the batch is undelivered, and deleting
        #: it to keep the queue moving would trade a visible stall for silent
        #: transcript loss.
        self._permanent_rejection: _PermanentFlushRejection | None = None

    @property
    def permanent_rejection(self) -> _PermanentFlushRejection | None:
        """The refusal that stopped mirroring, or None while it is running."""
        return self._permanent_rejection

    def _highest_spooled_ordinal(self) -> int:
        highest = 0
        for path in self._dir.glob("*.batch.json"):
            ordinal = path.name.split("-", 1)[0]
            if ordinal.isdigit():
                highest = max(highest, int(ordinal))
        return highest

    def confirmed_store_sequence(self) -> int:
        """Lower bound on what the platform has confirmed; 0 when unknown."""
        return self._sequence_fn() if self._sequence_fn is not None else 0

    # -- SessionStore protocol ------------------------------------------------

    def _write_batch(
        self,
        name: str,
        key: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> None:
        tmp = self._dir / (name + ".tmp")
        payload = json.dumps(
            {
                "key": key,
                "entries": entries,
                "append_id": str(uuid_mod.uuid4()),
            },
            ensure_ascii=False,
        )
        tmp.write_text(payload, encoding="utf-8")
        with tmp.open("rb+") as fh:
            os.fsync(fh.fileno())
        tmp.rename(self._dir / name)

    async def _append_locked(
        self, key: dict[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        self._counter += 1
        name = f"{self._counter:012d}-{uuid_mod.uuid4().hex[:8]}.batch.json"
        await asyncio.to_thread(self._write_batch, name, dict(key), list(entries))
        self._accepted_appends += 1
        self._wakeup.set()

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        async with self._append_lock:
            await self._append_locked(key, entries)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        stored = await self._load_fn(dict(key))
        tail: list[dict[str, Any]] = []
        for path in self._pending_batches():
            batch = json.loads(path.read_text(encoding="utf-8"))
            if batch.get("key") == dict(key):
                tail.extend(batch.get("entries") or [])
        if not tail:
            return stored
        return list(stored or []) + tail

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        """Return durable and locally-spooled child transcript keys in order."""

        project_key = key.get("project_key")
        session_id = key.get("session_id")
        pending: list[str] = []
        pending_seen: set[str] = set()
        for path in self._pending_batches():
            batch = json.loads(path.read_text(encoding="utf-8"))
            batch_key = batch.get("key")
            if not isinstance(batch_key, dict):
                raise RunnerProtocolError("spooled transcript batch has no key")
            if (
                batch_key.get("project_key") != project_key
                or batch_key.get("session_id") != session_id
            ):
                continue
            subpath = batch_key.get("subpath")
            if isinstance(subpath, str) and subpath and subpath not in pending_seen:
                pending_seen.add(subpath)
                pending.append(subpath)

        durable = await self._list_subkeys_fn(dict(key))
        if not isinstance(durable, list) or any(
            not isinstance(item, str) or not item for item in durable
        ):
            raise RunnerProtocolError(
                "transcript list_subkeys response must be a list of non-empty strings"
            )
        seen: set[str] = set()
        subkeys: list[str] = []
        for subpath in [*durable, *pending]:
            if subpath not in seen:
                seen.add(subpath)
                subkeys.append(subpath)
        return subkeys

    # -- flusher --------------------------------------------------------------

    def _pending_batches(self) -> list[Path]:
        return sorted(p for p in self._dir.glob("*.batch.json"))

    def pending_batch_count(self) -> int:
        return len(self._pending_batches())

    @property
    def has_accepted_append(self) -> bool:
        """Whether this process has fsync-acked any SessionStore batch."""
        return self._accepted_appends > 0

    def set_drain_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Register the owning session's action after the spool reaches empty."""

        if self._drain_handler is not None:
            raise RuntimeError("spool drain handler is already registered")
        self._drain_handler = handler

    def start_flusher(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop(), name="spool-flusher")

    async def stop_flusher(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def flush_once(self) -> int:
        """Flush all currently pending batches; returns how many landed.
        Raises on the first failure (the loop wraps this with backoff)."""
        flushed = 0
        for path in self._pending_batches():
            batch = json.loads(path.read_text(encoding="utf-8"))
            await self._flush_fn(batch["key"], batch["entries"], batch["append_id"])
            path.unlink()
            flushed += 1
        return flushed

    async def _flush_loop(self) -> None:
        while True:
            self._wakeup.clear()
            try:
                flushed = await self.flush_once()
            except _PermanentFlushRejection as exc:
                # The platform will refuse this batch identically forever, so
                # retrying is not resilience — it is a loop that hides the
                # cause. The transcript cannot advance past this batch either
                # way, so this logs once at error level, naming the batch, and
                # stops the flusher.
                logger.error(
                    "spool flush REFUSED permanently by the platform; transcript "
                    "mirroring has stopped for this box: %s",
                    exc,
                )
                self._permanent_rejection = exc
                return
            except Exception as exc:  # noqa: BLE001 — flusher must survive anything
                logger.warning("spool flush failed (will retry): %s", exc)
                await asyncio.sleep(self._retry_delay_s)
                continue
            if (
                flushed > 0
                and self.pending_batch_count() == 0
                and self._drain_handler is not None
            ):
                try:
                    await self._drain_handler()
                except Exception:  # noqa: BLE001 — keep transcript flushing alive
                    logger.exception(
                        "spool drain handler failed; journal was not compacted"
                    )
            await self._wakeup.wait()


@dataclasses.dataclass
class InteractionAnswer:
    decision: str  # "allow" | "deny"
    updated_input: dict[str, Any] | None = None
    message: str | None = None


class InteractionBroker:
    """Bridge hook approvals and Claude's native user-input callback to the host."""

    #: Tools acceptEdits auto-approves — the CLI's own file-edit set. Every
    #: other tool still asks; every other permission mode is unchanged by it.
    _ACCEPT_EDITS_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

    #: Vendor-owned interactive tool whose control plane is ``can_use_tool``.
    #: This name belongs here, inside the Claude adapter; the platform carries
    #: the tool name and input verbatim and defines no parallel vocabulary.
    _ASK_USER_TOOL = "AskUserQuestion"

    def __init__(
        self,
        sender: EnvelopeSender,
        *,
        wait_budget_s: float = DEFAULT_INTERACTION_WAIT_S,
        hook_timeout_s: float = INTERACTION_HOOK_TIMEOUT_S,
        permission_mode: str = "default",
    ) -> None:
        self._sender = sender
        self._wait_budget_s = wait_budget_s
        self._hook_ceiling_s = max(hook_timeout_s - _INTERACTION_HOOK_MARGIN_S, 1.0)
        self._pending: dict[str, asyncio.Future[InteractionAnswer]] = {}
        self._permission_mode = str(permission_mode or "default")

    def set_permission_mode(self, mode: str) -> None:
        """Track the CLI's live permission mode (updated by the same op that
        updates the SDK, after the SDK accepted it — the two never disagree)."""
        self._permission_mode = str(mode or "default")

    def answer(self, interaction_id: str, answer: InteractionAnswer) -> bool:
        """Host answer for a pending interaction. False if unknown/expired —
        the caller reports that back instead of silently dropping."""
        future = self._pending.get(interaction_id)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def pending_ids(self) -> list[str]:
        """Ids of gates still awaiting an answer (diagnostics only)."""
        return [k for k, f in self._pending.items() if not f.done()]

    def cancel_all(self, *, message: str) -> int:
        """Resolve every pending gate as a deny (interrupt path).

        The SDK's interrupt aborts the turn, but the CLI cannot finish
        aborting while a PreToolUse hook or can_use_tool callback is still
        blocked on a pending future: without this release the next turn hangs.
        Deny (not exception) lets either control seam return a well-formed
        decision and the abort proceed.
        """
        released = 0
        for future in self._pending.values():
            if not future.done():
                future.set_result(
                    InteractionAnswer(decision="deny", message=message)
                )
                released += 1
        return released

    async def pre_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        """Gate ordinary tools; leave native user input to ``can_use_tool``.

        Interaction identity is runner-minted — deliberately not
        ``tool_use_id``, which does not survive defer/resume.

        For hook-gated calls the tool id is still reported alongside it,
        because it is the only authoritative name for the tool_use block the
        gate belongs to, and this hook is the only place it and the interaction
        are both in hand. Without it on the wire the host has to re-derive the
        binding by matching the gate's input against streamed blocks."""
        tool_name = str(input_data.get("tool_name") or "")
        if tool_name == self._ASK_USER_TOOL:
            # Claude Code only offers AskUserQuestion when a permission prompt
            # control channel exists, and always dispatches the call through
            # that channel. Do not emit the same question from this earlier
            # hook: can_use_tool below owns its one interaction and answer.
            return {}

        # The CLI runs with its ordinary permission engine disarmed by this
        # hook's decisions, so the permission mode's semantics live here too.
        # bypassPermissions means "do not gate tools": without this
        # short-circuit a bypass session parks on every tool call and waits
        # for an answer nobody is expected to give.
        # acceptEdits auto-approves exactly the CLI's file-edit set.
        mode = self._permission_mode
        if mode == "bypassPermissions":
            return self._decision("allow", reason="permission_mode=bypassPermissions")
        if mode == "acceptEdits" and tool_name in self._ACCEPT_EDITS_TOOLS:
            return self._decision("allow", reason="permission_mode=acceptEdits")
        gate_tool_use_id = str(
            (input_data.get("tool_use_id") if isinstance(input_data, dict) else None)
            or tool_use_id
            or ""
        )
        if not gate_tool_use_id:
            # The vendor declares it required on PreToolUseHookInput, so an
            # empty one means this lookup is in the wrong place or the CLI
            # names it something else. Report what actually arrived rather
            # than guessing at the right key.
            logger.warning(
                "gate has no tool_use_id: input keys=%s positional=%r",
                sorted(input_data.keys()) if isinstance(input_data, dict) else type(input_data).__name__,
                tool_use_id,
            )
        interaction_id = uuid_mod.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionAnswer] = loop.create_future()
        self._pending[interaction_id] = future
        try:
            await self._sender.send(
                "interaction",
                interaction_id=interaction_id,
                # From the input payload, where the vendor declares it required:
                # ``PreToolUseHookInput.tool_use_id: str``. The callback's
                # positional ``tool_use_id`` is a different, optional thing —
                # ``HookCallback``'s second parameter is ``str | None`` because
                # not every hook event has one — and reading that instead
                # yields an empty id on every gate, blinding every projection
                # that binds an approval to its tool block.
                tool_use_id=gate_tool_use_id,
                tool_name=tool_name,
                tool_input=input_data.get("tool_input") or {},
            )
            answer = await self._await_answer(future)
            if answer is None:
                return self._decision(
                    "defer", reason="host gone; approval wait budget exhausted"
                )
            if answer.decision == "allow":
                out = self._decision("allow", reason="user approved")
                if answer.updated_input is not None:
                    out["hookSpecificOutput"]["updatedInput"] = answer.updated_input
                return out
            return self._decision("deny", reason=answer.message or "user denied")
        except asyncio.CancelledError:
            # Cancellation means the CLI abandoned the hook. Propagate it
            # without inventing an approval decision.
            raise
        except Exception as exc:
            # A gate that errors must fail CLOSED. Under the CLI's bypass
            # base mode an errored hook falls through to "run it", so an
            # exception escaping here executes a tool nobody approved (an
            # uncaught timeout out of ``_await_answer`` is one way in).
            # Whatever the defect behind the exception, denying is the only
            # answer that cannot be the wrong one.
            logger.exception("permission gate errored; denying the tool")
            return self._decision("deny", reason=f"permission gate error: {exc}")
        finally:
            self._pending.pop(interaction_id, None)

    async def can_use_tool(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        context: Any,
    ) -> Any:
        """Answer the SDK permission control request without a second gate.

        The vendor requires this callback to expose and operate
        ``AskUserQuestion``. Ordinary tool decisions remain at PreToolUse,
        where ``defer`` can stop and resume a run; if the CLI subsequently
        consults this callback for one of them, passing the already-approved
        input through avoids asking the user twice.
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if str(tool_name or "") != self._ASK_USER_TOOL:
            return PermissionResultAllow(updated_input=input_payload)

        tool_use_id = str(getattr(context, "tool_use_id", None) or "")
        if not tool_use_id:
            # ToolPermissionContext declares this non-empty on every callback.
            # Keep the interaction answerable, but expose a vendor contract
            # violation instead of fabricating an engine tool id.
            logger.warning("AskUserQuestion callback has no tool_use_id")

        interaction_id = uuid_mod.uuid4().hex
        future: asyncio.Future[InteractionAnswer] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[interaction_id] = future
        try:
            await self._sender.send(
                "interaction",
                interaction_id=interaction_id,
                tool_use_id=tool_use_id,
                tool_name=self._ASK_USER_TOOL,
                tool_input=input_payload,
            )
            # The SDK contract deliberately permits an indefinite user wait.
            # Unlike a hook, can_use_tool has no CLI timeout and no ``defer``
            # result type; interrupt/stop resolves the future through
            # ``cancel_all`` and a host reattach can still answer it.
            answer = await future
            if answer.decision == "allow":
                return PermissionResultAllow(
                    updated_input=(
                        answer.updated_input
                        if answer.updated_input is not None
                        else input_payload
                    )
                )
            return PermissionResultDeny(message=answer.message or "user denied")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AskUserQuestion control callback errored; denying")
            return PermissionResultDeny(message=f"user-input callback error: {exc}")
        finally:
            self._pending.pop(interaction_id, None)

    async def _await_answer(
        self, future: "asyncio.Future[InteractionAnswer]"
    ) -> "InteractionAnswer | None":
        """Wait for the answer; give up only once the host is gone, not the human.

        Waiting forever pins a run to a host that may never return; waiting a
        flat two minutes throws away a decision a person was still making, and
        loses every approval across a platform restart. So the budget runs only
        while the link is down, and resets when it comes back — a reattach means
        someone is listening again.
        """
        disconnected_for = 0.0
        started = time.monotonic()
        while True:
            # asyncio.wait returns normally on each poll timeout. This avoids
            # the Python 3.10 distinction between asyncio.TimeoutError and the
            # built-in TimeoutError; an uncaught timeout would escape the hook,
            # and the CLI's bypass mode could then execute an unapproved tool.
            done, _ = await asyncio.wait({future}, timeout=_INTERACTION_LINK_POLL_S)
            if done:
                return future.result()
            if time.monotonic() - started >= self._hook_ceiling_s:
                # The CLI is about to abort this callback. Deferring first is
                # what makes the outcome a vendor-supported stop rather than a
                # dropped gate.
                logger.info(
                    "approval deferred: reached the hook ceiling (%.0fs)",
                    self._hook_ceiling_s,
                )
                return None
            if self._sender.host_connected:
                disconnected_for = 0.0
                continue
            disconnected_for += _INTERACTION_LINK_POLL_S
            if disconnected_for >= self._wait_budget_s:
                logger.info(
                    "approval deferred: host absent %.1fs (budget %.1fs)",
                    disconnected_for,
                    self._wait_budget_s,
                )
                return None

    @staticmethod
    def _decision(decision: str, *, reason: str) -> dict[str, Any]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }


class RunnerSession:
    """Feed one persistent ``ClaudeSDKClient`` without platform turn state."""

    def __init__(
        self,
        *,
        session_id: str,
        link: HostLink,
        client_factory: Callable[[InteractionBroker], SdkSession],
        resume_client_factory: Callable[[InteractionBroker, str], SdkSession] | None = None,
        store: SpoolSessionStore | None = None,
        activation_callback: (
            Callable[[str, dict[str, Any] | None], None] | None
        ) = None,
        interaction_wait_s: float = DEFAULT_INTERACTION_WAIT_S,
        permission_mode: str = "default",
    ) -> None:
        # Before activation this is a slot id, used only for local task names
        # and diagnostics.  No envelope is emitted under it.  Activation
        # replaces it once with the platform Session id before the pumps start.
        self.slot_id = session_id
        self.session_id = session_id
        self.sender = EnvelopeSender(link, session_id)
        self.broker = InteractionBroker(
            self.sender,
            wait_budget_s=interaction_wait_s,
            permission_mode=permission_mode,
        )
        self._client_factory = client_factory
        self._resume_client_factory = resume_client_factory
        self._store = store
        self._activation_callback = activation_callback
        self._permission_mode = str(permission_mode or "default")
        self._client: SdkSession | None = None
        self._store_bound_to: str | None = None
        self._prepared = False
        self._active = False
        self._deliveries: asyncio.Queue[_QueuedDelivery | object] = asyncio.Queue()
        self._delivery_task: asyncio.Task[None] | None = None
        self._message_task: asyncio.Task[None] | None = None
        self._delivery_receipts: dict[
            str, tuple[DeliveryCommand, asyncio.Future[None]]
        ] = {}
        self._delivered_commands: dict[str, DeliveryCommand] = {}
        # Claude accepts another query while a response is active, but that is
        # steering rather than an ordered next prompt: the later instruction
        # can replace the answer the earlier input was still producing. The
        # runner therefore owns the adapter buffer and submits one input per
        # SDK Result boundary.
        self._buffered_deliveries: deque[_BufferedDelivery] = deque()
        self._pending_prompt_consumptions: deque[_PendingPromptConsumption] = deque()
        self._consumed_prompt_ids: set[str] = set()
        # Consumption groups commands under the next SDK Result. Interrupt
        # needs that boundary to decide whether the cancelled command already
        # settled and whether a queued successor keeps the adapter FIFO active.
        self._command_ids_by_input_id: dict[str, str] = {}
        # Vendor message id -> platform input id, for inputs queried but not yet
        # echoed. See :meth:`_query_delivery` for why the two differ.
        self._platform_input_ids_by_vendor_uuid: dict[str, str] = {}
        self._consumed_commands_since_result: set[str] = set()
        self._produced_commands: set[str] = set()
        # An interrupt can overtake a prompt already written to the CLI's stdin.
        # Keep that command at the FIFO head until UserPromptSubmit proves the
        # CLI dequeued it; the hook then blocks it before model or tool work.
        self._interrupts_awaiting_prompt_boundary: set[str] = set()
        # The response Claude started on its own that is running now, by the
        # uuid of the root prompt it dequeued, and every such id this session
        # has seen: an interrupt naming one that already ended is a no-op,
        # not an unknown command.
        self._engine_response_id: str | None = None
        self._engine_response_ids: set[str] = set()
        self._last_delivery_sequence = 0
        self._busy = False
        if self._store is not None:
            self._store.set_drain_handler(self._compact_after_store_drain)

    async def _compact_after_store_drain(self) -> None:
        store = self._store
        assert store is not None
        if (
            not store.has_accepted_append
            or store.pending_batch_count() != 0
            or store.permanent_rejection is not None
        ):
            return
        await self.sender.compact_after_store_flush(
            HistoryStoreSequence(store.confirmed_store_sequence())
        )

    def _with_prompt_consumption_hook(
        self,
        hooks: dict[str, list[Any]] | None,
    ) -> dict[str, list[Any]]:
        from claude_agent_sdk import HookMatcher

        configured = {
            event: list(matchers)
            for event, matchers in (hooks or {}).items()
        }
        configured["UserPromptSubmit"] = [
            HookMatcher(hooks=[cast("Any", self.on_user_prompt_submit)]),
            *configured.get("UserPromptSubmit", []),
        ]
        return configured

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def _store_bound(self) -> bool:
        return self._store_bound_to is not None

    def bind_store(self, platform_session_id: str, store: dict[str, Any] | None) -> None:
        """Point the transcript store at a Session before the SDK connects.

        Only a slot that is claimed in the same breath does this. A prepared
        slot has no Session to bind to, which is what the deferred target is
        for — and a prepared slot carries no resume, so nothing needs the
        store until it is claimed.
        """

        if self._prepared or self._client is not None:
            raise RunnerProtocolError("bind_store after the slot is prepared")
        target = str(platform_session_id or "").strip()
        if not target:
            raise RunnerProtocolError("bind_store missing session_id")
        if self._activation_callback is None:
            raise RunnerProtocolError("this runner slot has no transcript store")
        self._activation_callback(target, store)
        self._store_bound_to = target
        if self._store is not None:
            self._store.start_flusher()

    async def prepare(self) -> None:
        """Spawn and initialize the SDK without exposing a Session or input."""

        if self._prepared or self._client is not None:
            raise RunnerProtocolError("runner slot is already prepared")
        self._client = self._client_factory(self.broker)
        await self._client.connect()
        self._prepared = True

    async def activate(
        self,
        session_id: str,
        link: HostLink,
        *,
        store: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        mcp_servers: list[str] | None = None,
        resume_session_key: str | None = None,
    ) -> None:
        """Bind a prepared SDK process to exactly one platform Session."""

        if not self._prepared or self._client is None:
            raise RunnerProtocolError("activate before prepare")
        if self._active:
            raise RunnerProtocolError("prepared runner slot is already active")
        target = str(session_id or "").strip()
        if not target:
            raise RunnerProtocolError("activate missing session_id")
        if self._activation_callback is not None and not self._store_bound:
            self._activation_callback(target, store)
        elif self._store_bound and target != self._store_bound_to:
            # The store is scoped to one platform Session. A slot bound at
            # prepare may only be claimed by the Session it was bound for.
            raise RunnerProtocolError(
                f"activate as {target!r} on a slot whose transcript store is "
                f"bound to {self._store_bound_to!r}"
            )
        requested_mode = str(permission_mode or self._permission_mode).strip()
        if not requested_mode:
            raise RunnerProtocolError("activate permission_mode is empty")
        resume = str(resume_session_key or "").strip()
        if resume:
            if self._resume_client_factory is None:
                raise RunnerProtocolError("this runner cannot resume a prepared Session")
            # Claude reads resume and SessionStore during connect, not query.
            # Only this unclaimed slot's process is replaced; its box and
            # prepared files stay in place. Bind the store above before connect.
            await self._client.disconnect()
            self._client = self._resume_client_factory(self.broker, resume)
            await self._client.connect()
        if requested_mode != self._permission_mode:
            await self._client.set_permission_mode(requested_mode)
            self.broker.set_permission_mode(requested_mode)
            self._permission_mode = requested_mode
        required_mcp = [
            str(name).strip() for name in (mcp_servers or []) if str(name).strip()
        ]
        if len(required_mcp) != len(set(required_mcp)):
            raise RunnerProtocolError("activate mcp_servers contains duplicates")
        for server_name in required_mcp:
            await self._client.reconnect_mcp_server(server_name)
        if required_mcp:
            status_response = await self._client.get_mcp_status()
            raw_statuses = (
                status_response.get("mcpServers")
                if isinstance(status_response, dict)
                else None
            )
            if not isinstance(raw_statuses, list):
                raise RunnerProtocolError(
                    "MCP activation status carries no mcpServers list"
                )
            statuses = {
                str(item.get("name") or "").strip(): str(
                    item.get("status") or ""
                ).strip()
                for item in raw_statuses
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
            unusable = {
                name: statuses.get(name, "missing")
                for name in required_mcp
                if statuses.get(name) != "connected"
            }
            if unusable:
                raise RunnerProtocolError(
                    "required MCP servers are not connected after activation: "
                    f"{unusable!r}"
                )
        self.sender.bind_prepared_session(link, target)
        self.session_id = target
        self._active = True
        if self._store is not None and not self._store_bound:
            self._store.start_flusher()
        await self.sender.send("status", state="idle")
        self._delivery_task = asyncio.create_task(
            self._pump_deliveries(),
            name=f"sdk-fifo-delivery:{target}",
        )
        self._message_task = asyncio.create_task(
            self._pump_events(),
            name=f"sdk-fifo-messages:{target}",
        )

    async def start(self, *, store: dict[str, Any] | None = None) -> None:
        """Start an already-bound session in transport-neutral unit tests.

        Delegates to the same ``prepare`` then ``activate`` barriers the wire
        path uses: a direct RunnerSession user constructed this session with
        its final Session id and link, so activation re-binds those same
        values. Duplicating the activation tail here instead would let the
        direct path drift from the wire one — skipping the sender's one-time
        identity adoption and the activation callback that binds the store.

        ``store`` is what an ``activate`` frame carries; a session built with
        a deferred transcript target needs it here for the same reason the
        wire path requires it, and one built without a target ignores it.
        """

        await self.prepare()
        await self.activate(self.session_id, self.sender.link, store=store)
        # Activation binds the link the way the wire path does, which holds
        # sends behind the hello handshake. A transport-neutral caller has no
        # hello to send, so it completes the same handshake by replaying an
        # empty window — the one step that reopens the sender.
        await self.sender.replay_after(0)

    @staticmethod
    async def _single_input(item: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield item

    def _validated_input(self, command: DeliveryCommand) -> dict[str, Any]:
        command_id = str(command.command_id or "").strip()
        if not command_id:
            raise RunnerProtocolError("command_id is required")
        if command.session_id != self.session_id:
            raise RunnerProtocolError(
                "delivery belongs to a different session "
                f"expected={self.session_id!r} actual={command.session_id!r}"
            )
        if command.sequence <= self._last_delivery_sequence:
            raise RunnerProtocolError(
                "delivery sequence is not increasing "
                f"previous={self._last_delivery_sequence} "
                f"actual={command.sequence}"
            )
        if not isinstance(command.sdk_input, dict):
            raise RunnerProtocolError("sdk_input must be a dict")
        item = deepcopy(command.sdk_input)
        input_uuid = item.get("uuid")
        if not isinstance(input_uuid, str) or not input_uuid.strip():
            raise RunnerProtocolError("SDK input uuid is required")
        try:
            normalized_input_uuid = str(uuid_mod.UUID(input_uuid.strip()))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RunnerProtocolError("SDK input uuid must be a valid UUID") from exc
        item["uuid"] = normalized_input_uuid
        input_session_id = item.get("session_id")
        if input_session_id not in {None, "", self.session_id}:
            raise RunnerProtocolError(
                "SDK input session does not match the runner "
                f"expected={self.session_id!r} actual={input_session_id!r}"
            )
        item["session_id"] = self.session_id
        return item

    @staticmethod
    def _pending_prompt_consumption(
        sdk_input: dict[str, Any],
    ) -> _PendingPromptConsumption:
        input_id = str(sdk_input.get("uuid") or "").strip()
        message = sdk_input.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return _PendingPromptConsumption(input_id=input_id, prompt=content)
        # Blocks reach Claude as blocks, but the prompt hook reports the text
        # they contain — the same projection the host stored as ``content``.
        if isinstance(content, list) and content:
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    raise RunnerProtocolError(
                        "SDK root input content block must be an object"
                    )
                if str(block.get("type") or "") != "text":
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    raise RunnerProtocolError(
                        "SDK root input text block must carry a string"
                    )
                if text.strip():
                    texts.append(text)
            return _PendingPromptConsumption(
                input_id=input_id,
                prompt="\n".join(texts),
            )
        raise RunnerProtocolError(
            "SDK root input content must be a string or content blocks"
        )

    async def on_user_prompt_submit(
        self,
        hook_input: Any,
        _tool_use_id: str | None,
        _context: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(hook_input, dict)
            or hook_input.get("hook_event_name") != "UserPromptSubmit"
            or not isinstance(hook_input.get("prompt"), str)
        ):
            raise RunnerProtocolError("SDK UserPromptSubmit hook payload is malformed")
        if not self._pending_prompt_consumptions:
            return {}
        pending = self._pending_prompt_consumptions[0]
        prompt = hook_input["prompt"]
        if prompt != pending.prompt:
            # Claude Code also routes its own task-notification prompts through
            # UserPromptSubmit. They are not platform FIFO inputs and must not
            # advance the external-input queue.
            return {}
        self._pending_prompt_consumptions.popleft()
        if pending.input_id in self._consumed_prompt_ids:
            raise RunnerProtocolError("SDK consumed the same FIFO input twice")
        self._consumed_prompt_ids.add(pending.input_id)
        command_id = self._command_ids_by_input_id.get(pending.input_id)
        if command_id is not None:
            self._consumed_commands_since_result.add(command_id)
        from claude_agent_sdk import UserMessage

        message = UserMessage(
            content=prompt,
            uuid=pending.input_id,
            parent_tool_use_id=None,
        )
        await self.sender.send(
            "event",
            message_type="UserMessage",
            message=_jsonable(message),
            requires_persistence=True,
        )
        if command_id in self._interrupts_awaiting_prompt_boundary:
            # interrupt() only acknowledges the CLI control request. It does not
            # retract a prompt already queued on stdin, so the prompt hook is the
            # first boundary that can prevent the cancelled input from running.
            return {
                "decision": "block",
                "reason": "user interrupted the session",
            }
        return {}

    @staticmethod
    def _is_root_assistant_activity(message: Any, message_type: str) -> bool:
        """Whether this SDK message starts or is a root assistant message.

        A child's activity carries ``parent_tool_use_id`` and never opens a
        root response. With partial messages on, the first root event of a
        response is the ``message_start`` StreamEvent; with them off, the
        complete AssistantMessage is the first root output there is.
        """

        if getattr(message, "parent_tool_use_id", None) is not None:
            return False
        if message_type == "AssistantMessage":
            return True
        if message_type != "StreamEvent":
            return False
        event = getattr(message, "event", None)
        return isinstance(event, dict) and str(event.get("type") or "") == "message_start"

    async def _interrupt_engine_response(self, response_id: str) -> None:
        """Stop the response Claude started on its own, named by its address.

        No platform command owns it, so there is no FIFO row to settle and no
        prompt boundary to wait for: the SDK interrupt is the whole stop. The
        same race as a command interrupt applies afterwards — Claude Code does
        not always follow the acknowledgement with a ResultMessage — so the
        correlated control event closes the observed interval on the host,
        and a Result that does arrive finds nothing open.
        """

        assert self._client is not None
        if response_id != self._engine_response_id:
            return
        released = self.broker.cancel_all(message="user interrupted the session")
        if released:
            logger.info("interrupt released %d pending interaction(s)", released)
        await self._client.interrupt()
        if response_id != self._engine_response_id:
            return
        self._engine_response_id = None
        continues_fifo = bool(
            self._pending_prompt_consumptions
            or self._consumed_commands_since_result
            or self._buffered_deliveries
        )
        await self.sender.send(
            "turn_interrupted",
            command_id=response_id,
            continues_fifo=continues_fifo,
        )
        prompted_successor = (
            await self._query_next_buffered()
            if self._buffered_deliveries
            else False
        )
        self._busy = bool(
            prompted_successor
            or self._buffered_deliveries
            or self._pending_prompt_consumptions
            or self._consumed_commands_since_result
        )
        if not self._busy:
            await self.sender.send("status", state="idle")

    async def _pump_deliveries(self) -> None:
        while True:
            queued = await self._deliveries.get()
            if queued is _STOP:
                return
            if not isinstance(queued, _QueuedDelivery):
                raise RunnerProtocolError("delivery queue contains an invalid item")
            try:
                if self._client is None:
                    raise RunnerProtocolError("input before session start")
                sdk_input = self._validated_input(queued.command)
                delivery = _BufferedDelivery(
                    command=queued.command,
                    sdk_input=sdk_input,
                    pending_consumption=self._pending_prompt_consumption(sdk_input),
                )
                if self._busy:
                    # Acceptance is the adapter's receipt, not a claim that the
                    # vendor consumed the input. Keep the command replayable by
                    # id and report consumption only after UserPromptSubmit.
                    self._buffered_deliveries.append(delivery)
                    logger.info(
                        "buffered behind the active response: command_id=%s "
                        "sequence=%d",
                        queued.command.command_id,
                        queued.command.sequence,
                    )
                else:
                    await self._query_delivery(delivery)
                    logger.info(
                        "queried the SDK: command_id=%s sequence=%d",
                        queued.command.command_id,
                        queued.command.sequence,
                    )
                self._delivered_commands[queued.command.command_id] = queued.command
                self._last_delivery_sequence = queued.command.sequence
            except Exception as exc:
                if not queued.accepted.done():
                    queued.accepted.set_exception(_detached_exception(exc))
            else:
                if not self._busy:
                    self._busy = True
                    await self.sender.send("status", state="busy")
                if not queued.accepted.done():
                    queued.accepted.set_result(None)

    async def _query_delivery(self, delivery: _BufferedDelivery) -> None:
        """Give one accepted FIFO input to Claude at a legal prompt boundary."""

        if self._client is None:
            raise RunnerProtocolError("input before session start")
        pending = delivery.pending_consumption
        self._pending_prompt_consumptions.append(pending)
        self._command_ids_by_input_id[pending.input_id] = delivery.command.command_id
        # ``uuid`` on a streaming input is the vendor's id for a transcript
        # message; the platform input id identifies the FIFO entry. Claude discards,
        # silently, an input whose uuid already names a message in the session
        # it resumed — which is every re-delivery into a rebuilt box, since the
        # input the box died on is in the transcript that box resumed from. So
        # each attempt gets its own vendor message id and the runner translates
        # the echo back, keeping the platform's id the one the host ever sees.
        vendor_uuid = str(uuid_mod.uuid4())
        self._platform_input_ids_by_vendor_uuid[vendor_uuid] = pending.input_id
        try:
            await self._client.query(
                self._single_input({**delivery.sdk_input, "uuid": vendor_uuid}),
                session_id=self.session_id,
            )
        except Exception:
            self._platform_input_ids_by_vendor_uuid.pop(vendor_uuid, None)
            if pending in self._pending_prompt_consumptions:
                self._pending_prompt_consumptions.remove(pending)
            self._command_ids_by_input_id.pop(pending.input_id, None)
            raise

    async def _query_next_buffered(self) -> bool:
        """Prompt the FIFO head after the preceding response has ended."""

        if not self._buffered_deliveries:
            return False
        delivery = self._buffered_deliveries[0]
        await self._query_delivery(delivery)
        self._buffered_deliveries.popleft()
        return True

    @staticmethod
    def _same_delivery(left: DeliveryCommand, right: DeliveryCommand) -> bool:
        return (
            left.command_id == right.command_id
            and left.session_id == right.session_id
            and left.sequence == right.sequence
            and left.sdk_input == right.sdk_input
        )

    async def submit(self, command: DeliveryCommand) -> str:
        if self._client is None or not self._active:
            raise RunnerProtocolError("runner is not accepting deliveries")
        existing = self._delivered_commands.get(command.command_id)
        if existing is not None:
            if not self._same_delivery(existing, command):
                raise RunnerProtocolError(
                    "delivery command id collision with different payload "
                    f"command_id={command.command_id!r}"
                )
            return "duplicate"
        receipt = self._delivery_receipts.get(command.command_id)
        if receipt is None:
            accepted: asyncio.Future[None] = (
                asyncio.get_running_loop().create_future()
            )
            self._delivery_receipts[command.command_id] = (command, accepted)
            await self._deliveries.put(
                _QueuedDelivery(command=command, accepted=accepted)
            )
        else:
            pending_command, accepted = receipt
            if not self._same_delivery(pending_command, command):
                raise RunnerProtocolError(
                    "delivery command id collision with different payload "
                    f"command_id={command.command_id!r}"
                )
        try:
            await accepted
        except BaseException:
            self._delivery_receipts.pop(command.command_id, None)
            raise
        return "accepted"

    def answer_interaction(
        self, interaction_id: str, answer: InteractionAnswer
    ) -> bool:
        return self.broker.answer(interaction_id, answer)

    def attach_link(self, link: HostLink) -> None:
        self.sender.set_link(link)

    async def interrupt(self, command_id: str) -> None:
        if self._client is None:
            raise RunnerProtocolError("interrupt before session start")
        command_id = str(command_id or "").strip()
        if not command_id:
            raise RunnerProtocolError("interrupt requires command_id")
        if command_id in self._engine_response_ids:
            await self._interrupt_engine_response(command_id)
            return
        command = self._delivered_commands.get(command_id)
        if command is None:
            raise RunnerProtocolError(
                f"interrupt names an unknown command {command_id!r}"
            )
        released = self.broker.cancel_all(message="user interrupted the session")
        if released:
            logger.info("interrupt released %d pending interaction(s)", released)
        input_id = str(command.sdk_input.get("uuid") or "").strip()
        pending = next(
            (
                item
                for item in self._pending_prompt_consumptions
                if item.input_id == input_id
            ),
            None,
        )
        awaits_prompt_boundary = pending is not None
        if awaits_prompt_boundary:
            self._interrupts_awaiting_prompt_boundary.add(command_id)
        try:
            await self._client.interrupt()
        except BaseException:
            self._interrupts_awaiting_prompt_boundary.discard(command_id)
            raise
        if command_id in self._produced_commands:
            self._interrupts_awaiting_prompt_boundary.discard(command_id)
            return

        if awaits_prompt_boundary:
            # The CLI may acknowledge interrupt before it dequeues the prompt.
            # Publishing a terminal here would make the runner look idle and let
            # a successor overtake an input the CLI can still execute. The prompt
            # hook above will publish consumption, block execution, and the SDK's
            # resulting ResultMessage will close this command in FIFO order.
            return

        # SDK interrupt returning is the engine's acknowledgement. Claude Code
        # does not always follow it with a ResultMessage when the interrupt
        # races local transcript finalization, so publish one correlated
        # platform control event instead of leaving the host stream blocked.
        self._consumed_commands_since_result.discard(command_id)
        self._produced_commands.add(command_id)
        continues_fifo = bool(
            self._pending_prompt_consumptions
            or self._consumed_commands_since_result
            or self._buffered_deliveries
        )
        await self.sender.send(
            "turn_interrupted",
            command_id=command_id,
            continues_fifo=continues_fifo,
        )
        prompted_successor = (
            await self._query_next_buffered()
            if self._buffered_deliveries
            else False
        )
        self._busy = bool(
            prompted_successor
            or self._buffered_deliveries
            or self._pending_prompt_consumptions
            or self._consumed_commands_since_result
        )
        if not self._busy:
            await self.sender.send("status", state="idle")

    async def stop_task(self, task_id: str) -> None:
        if self._client is None:
            raise RunnerProtocolError("stop_task before session start")
        await self._client.stop_task(task_id)

    async def set_permission_mode(self, mode: str) -> None:
        if self._client is None:
            raise RunnerProtocolError("set_permission_mode before session start")
        await self._client.set_permission_mode(mode)
        # Only after the SDK accepted it: the broker's gate short-circuits on
        # this mode, and gating on a mode the CLI refused would split the two.
        self.broker.set_permission_mode(mode)

    async def get_server_info(self) -> dict[str, Any] | None:
        if self._client is None:
            raise RunnerProtocolError("get_init_info before session start")
        info = await self._client.get_server_info()
        return info if isinstance(info, dict) else None

    async def stop(self) -> None:
        if self._delivery_task is not None:
            await self._deliveries.put(_STOP)
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
            self._delivery_task = None
        if self._message_task is not None:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass
            self._message_task = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._prepared = False
        self._active = False
        if self._store is not None:
            await self._store.stop_flusher()

    async def _pump_events(self) -> None:
        assert self._client is not None
        try:
            await self._pump_events_inner()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the pump's death is the message
            # A dying pump dies silently without this handler: the SDK raises
            # (ProcessError — the CLI died; CLIJSONDecodeError — unparseable
            # output), the task ends, and the host sees a turn go quiet with
            # nothing to act on. The SDK's own class name is the taxonomy —
            # "CLI not installed", "CLI died" and "output unparseable" are
            # three different operator actions, and a bare string would
            # collapse them into one.
            with contextlib.suppress(Exception):
                await self.sender.send(
                    "error",
                    error_class=type(exc).__name__,
                    detail=str(exc),
                )
            raise

    async def _pump_events_inner(self) -> None:
        assert self._client is not None
        async for message in self._client.receive_messages():
            platform_input_id: str | None = None
            message_type = type(message).__name__
            if message_type == "UserMessage":
                content = getattr(message, "content", None)
                parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
                input_id = str(getattr(message, "uuid", None) or "").strip()
                pending = None
                if parent_tool_use_id is None and input_id:
                    with contextlib.suppress(ValueError):
                        input_id = str(uuid_mod.UUID(input_id))
                    platform_input_id = self._platform_input_ids_by_vendor_uuid.pop(
                        input_id, None
                    )
                    if platform_input_id is not None:
                        input_id = platform_input_id
                    if input_id in self._consumed_prompt_ids:
                        self._consumed_prompt_ids.remove(input_id)
                        continue
                    pending = next(
                        (
                            item
                            for item in self._pending_prompt_consumptions
                            if item.input_id == input_id
                        ),
                        None,
                    )
                    if pending is not None:
                        self._pending_prompt_consumptions.remove(pending)
                        command_id = self._command_ids_by_input_id.get(input_id)
                        if command_id is not None:
                            self._consumed_commands_since_result.add(command_id)
                if isinstance(content, str) and pending is None:
                    # The SDK echoes its own root prompts as UserMessage. Only
                    # a UUID already in the platform delivery queue can cross
                    # the external-input boundary; list-valued tool results
                    # remain engine output.
                    continue
            payload = _jsonable(message)
            if platform_input_id is not None and isinstance(payload, dict):
                # The host reads a root UserMessage's uuid as the platform input
                # id it is the consumption receipt for.
                payload["uuid"] = platform_input_id
            extra: dict[str, Any] = {}
            # A root assistant response beginning while nothing platform-owned
            # is in flight is the engine's own: Claude answering a queued task
            # notification, which this SDK does not echo as a UserMessage. The
            # boundary is the real activity — the first root `message_start`,
            # or the complete AssistantMessage when partial events are off —
            # exactly as the internal active projector marks it active. It is
            # committed before compaction can drop it, and later platform
            # inputs queue behind the response like behind any active one.
            engine_boundary = (
                self._is_root_assistant_activity(message, message_type)
                and not self._busy
                and not self._pending_prompt_consumptions
                and not self._consumed_commands_since_result
            )
            if engine_boundary:
                extra["engine_boundary"] = True
                extra["requires_persistence"] = True
            is_result = message_type == "ResultMessage"
            store_sequence: HistoryStoreSequence | None = None
            if is_result:
                # The turn's store coordinate rides the Result and nothing
                # else. It has a value only where the store has actually been
                # written, and that is here; putting a coordinate on every
                # frame would publish numbers that never described durable
                # state. Live-only frames have no store position.
                store_sequence = HistoryStoreSequence(
                    self._store.confirmed_store_sequence()
                    if self._store is not None
                    else 0
                )
            if message_type in _PERSISTENT_SDK_MESSAGE_TYPES:
                extra["requires_persistence"] = True
            await self.sender.send(
                "event",
                result_store_sequence=store_sequence,
                message_type=message_type,
                message=payload,
                **extra,
            )
            if engine_boundary:
                self._busy = True
                response_id = str(getattr(message, "uuid", None) or "").strip()
                if not response_id:
                    raise RunnerProtocolError(
                        "engine-owned response boundary carries no SDK envelope uuid"
                    )
                # The host addresses this response by the same id: a stop
                # aimed at it arrives as an interrupt naming it.
                self._engine_response_id = response_id
                self._engine_response_ids.add(response_id)
                await self.sender.send("status", state="busy")
            if is_result:
                self._engine_response_id = None
                self._interrupts_awaiting_prompt_boundary.difference_update(
                    self._consumed_commands_since_result
                )
                self._produced_commands.update(
                    self._consumed_commands_since_result
                )
                self._consumed_commands_since_result.clear()
                if (
                    self._store is not None
                    and self._store.has_accepted_append
                    and self._store.pending_batch_count() == 0
                    and self._store.permanent_rejection is None
                ):
                    # Re-read after sending Result. A flush can complete
                    # between the checkpoint snapshot and this check; using
                    # the snapshot would miss both this path and the drain
                    # callback that already ran.
                    await self.sender.compact_after_store_flush(
                        HistoryStoreSequence(
                            self._store.confirmed_store_sequence()
                        )
                    )
                prompted_successor = await self._query_next_buffered()
                # deferred_tool_use rides on the ResultMessage event itself. A
                # separate op for it would be unreachable: the host's turn
                # iterator returns at the result frame, so anything sent after
                # it only meets the next turn's seq barrier.
                self._busy = bool(
                    prompted_successor
                    or self._buffered_deliveries
                    or self._pending_prompt_consumptions
                    or self._consumed_commands_since_result
                )
                if not self._busy:
                    await self.sender.send("status", state="idle")


# ── websocket transport ─────────────────────────────────────────────────────
#
# One isolated slot, one Claude process, one eventual platform Session.  A
# fresh runner accepts ``prepare`` and initializes Claude without input.  The
# claiming host reconnects with ``activate``; later hosts use ``attach``.  The
# active Session keeps running when the host link drops: retained frames replay
# by cursor; an expired cursor triggers a SessionStore rebuild before the
# retained suffix.

SessionFactory = Callable[[dict[str, Any], HostLink], "RunnerSession"]


class _WsHostLink:
    """HostLink over one websocket connection. A send failure marks the link
    dead (frames drop, seq advances) — never raises into the session."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._open = True

    def is_connected(self) -> bool:
        return self._open

    def mark_closed(self) -> None:
        self._open = False

    async def send(self, frame: dict[str, Any]) -> bool:
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — dead link is a state, not an error
            self._open = False
            return False
        return True


class RunnerWsServer:
    """Envelope endpoint for one prepared slot and its eventual Session."""

    def __init__(self, *, host: str = "0.0.0.0", port: int = 8000,
                 session_factory: SessionFactory) -> None:
        self._host = host
        self._port = port
        self._session_factory = session_factory
        self.session: RunnerSession | None = None
        self._activation_token: str | None = None
        self._server: Any = None

    @property
    def port(self) -> int:
        return self._port

    @staticmethod
    def _process_request(connection: Any, request: Any) -> Any:
        # Plain-HTTP GET /health answers 200 "OK" without a ws upgrade: the
        # host's readiness gate probes this — from inside the box and from the
        # host — before dialing the envelope websocket, and the exact body is
        # part of that probe's contract (a stranger answering the port fails
        # it). Anything else falls through to the ws handshake.
        if request.path == "/health":
            from http import HTTPStatus

            return connection.respond(HTTPStatus.OK, "OK")
        return None

    async def start(self) -> None:
        from websockets.asyncio.server import serve as websocket_serve

        self._server = await websocket_serve(
            self._handle, self._host, self._port,
            process_request=self._process_request,
        )
        if self._port == 0:  # tests bind ephemeral ports
            self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self._activation_token = None

    async def _handle(self, ws: Any) -> None:
        link = _WsHostLink(ws)
        try:
            opening = json.loads(await ws.recv())
        except Exception:
            return
        try:
            await self._open_session(opening, link, ws)
        except RunnerProtocolError as exc:
            await link.send({"op": "error", "detail": str(exc)})
            await ws.close(code=1002, reason=str(exc))
            return
        try:
            async for raw in ws:
                frame = json.loads(raw)
                await self._dispatch(frame, link)
        except Exception as exc:  # noqa: BLE001 — connection teardown path
            logger.info("host link closed: %s", exc)
        finally:
            link.mark_closed()

    async def _open_session(self, opening: dict[str, Any], link: _WsHostLink, ws: Any) -> None:
        op = opening.get("op")
        after_sequence = 0
        engine_contract = _runner_engine_contract()
        _validate_host_engine_requirements(
            opening.get("engine_requirements"),
            engine_contract,
        )
        if op == "prepare":
            if self.session is not None:
                raise RunnerProtocolError(
                    "prepare on a non-empty runner slot — attach instead"
                )
            slot_id = str(opening.get("slot_id") or "").strip()
            if not slot_id:
                raise RunnerProtocolError("prepare missing slot_id")
            activation_token = str(
                opening.get("activation_token") or ""
            ).strip()
            if not activation_token:
                raise RunnerProtocolError("prepare missing activation_token")
            session = self._session_factory(opening, link)
            self.session = session
            self._activation_token = activation_token
            # A slot claimed in the same breath binds its transcript store now,
            # not on the activate that follows: `options.resume` is read inside
            # the connect below, and the vendor materializes it from the store
            # when the box has no local transcript. Every rebuild after a
            # sandbox death is that case.
            claimed_by = str(opening.get("claimed_by") or "").strip()
            if claimed_by:
                session.bind_store(claimed_by, opening.get("store"))
            try:
                await session.prepare()
            except BaseException:
                self.session = None
                self._activation_token = None
                with contextlib.suppress(BaseException):
                    await session.stop()
                raise
            await link.send({
                "op": "prepared",
                "protocol": RUNNER_PROTOCOL,
                "slot_id": slot_id,
                "engine_contract": engine_contract,
            })
            logger.info("runner prepared slot %s", slot_id)
            return
        if op == "activate":
            session = self.session
            if session is None or not session.is_prepared:
                raise RunnerProtocolError("activate with no prepared slot")
            if session.is_active:
                raise RunnerProtocolError(
                    "activate on an active runner — attach instead"
                )
            requested_slot = str(opening.get("slot_id") or "").strip()
            if not requested_slot:
                raise RunnerProtocolError("activate missing slot_id")
            if requested_slot != session.slot_id:
                raise RunnerProtocolError(
                    "activate slot mismatch: runner holds "
                    f"{session.slot_id!r}, host asked for {requested_slot!r}"
                )
            activation_token = str(
                opening.get("activation_token") or ""
            ).strip()
            expected_token = str(self._activation_token or "")
            if not activation_token or not secrets.compare_digest(
                activation_token, expected_token
            ):
                raise RunnerProtocolError("activate token mismatch")
            session_id = str(opening.get("session_id") or "").strip()
            if not session_id:
                raise RunnerProtocolError("activate missing session_id")
            raw_mcp_servers = opening.get("mcp_servers", [])
            if not isinstance(raw_mcp_servers, list) or any(
                not isinstance(name, str) or not name.strip()
                for name in raw_mcp_servers
            ):
                raise RunnerProtocolError(
                    "activate mcp_servers must be a list of non-empty names"
                )
            try:
                await session.activate(
                    session_id,
                    link,
                    store=(
                        opening.get("store")
                        if isinstance(opening.get("store"), dict)
                        else None
                    ),
                    permission_mode=str(
                        opening.get("permission_mode") or ""
                    ).strip()
                    or None,
                    mcp_servers=list(raw_mcp_servers),
                    resume_session_key=str(opening.get("resume") or "").strip() or None,
                )
            except BaseException:
                # A partially activated Claude process is never returned to
                # prepared state.  Destroy it so a retry has a fresh identity.
                self.session = None
                self._activation_token = None
                with contextlib.suppress(BaseException):
                    await session.stop()
                raise
        elif op == "attach":
            if self.session is None or not self.session.is_active:
                raise RunnerProtocolError("attach with no active session")
            requested = str(opening.get("session_id") or "").strip()
            if not requested:
                # Absence is refused separately because an empty string is
                # falsy: a mismatch comparison guarded on the name being
                # present does not run for an unnamed attach, and the newest
                # connection wins the link unconditionally. Under shared
                # tenancy that path needs no secret — sibling conversations
                # share the box network namespace and the runner port is
                # derived from the uid. The name is not a credential; the
                # hello reply carries it back. Requiring it is the difference
                # between needing the session id and needing nothing.
                raise RunnerProtocolError("attach missing session_id")
            if requested != self.session.session_id:
                raise RunnerProtocolError(
                    f"attach session mismatch: runner holds "
                    f"{self.session.session_id!r}, host asked for {requested!r}"
                )
            after_sequence = opening.get("last_seen_seq")
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
            ):
                raise RunnerProtocolError(
                    "attach last_seen_seq must be a non-negative int"
                )
            # Validate before replacing the live link. A malformed attach must
            # not evict the host that is still serving this session.
            self.session.sender.cursor_window(after_sequence)
            self.session.attach_link(link)
            logger.info("attached to session %s", self.session.session_id)
        else:
            raise RunnerProtocolError(
                f"first frame must be prepare|activate|attach, got {op!r}"
            )
        logger.info("runner serving session %s", self.session.session_id)
        _write_death_notice(opening.get("death_notice"))
        window = self.session.sender.cursor_window(after_sequence)
        await link.send({
            "op": "hello",
            "protocol": RUNNER_PROTOCOL,
            "session_id": self.session.session_id,
            "engine_contract": engine_contract,
            "last_seq": self.session.sender.last_seq,
            "first_retained_sequence": window.first_retained_sequence,
        })
        # After the handshake, never before it: the host keys its stream state
        # off `hello`, and a frame that arrives ahead of it has nowhere to go.
        # Gap + retained suffix stay under the sender's lock so compaction
        # cannot move the advertised boundary between them.
        await self.session.sender.replay_after(after_sequence, emit_gap=True)

    async def _dispatch(self, frame: dict[str, Any], link: _WsHostLink) -> None:
        session = self.session
        if session is None:
            raise RunnerProtocolError("frame before activation")
        op = frame.get("op")
        if op == "input":
            sequence = frame.get("sequence")
            sdk_input = frame.get("sdk_input")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= 0
                or not isinstance(sdk_input, dict)
            ):
                raise RunnerProtocolError(
                    "input requires a positive sequence and sdk_input object"
                )
            command = DeliveryCommand(
                command_id=str(frame.get("command_id") or ""),
                session_id=str(frame.get("session_id") or ""),
                sequence=sequence,
                sdk_input=sdk_input,
            )
            logger.info(
                "input received: command_id=%s sequence=%d",
                command.command_id,
                command.sequence,
            )
            status = await session.submit(command)
            logger.info(
                "input accepted: command_id=%s status=%s",
                command.command_id,
                status,
            )
            await session.sender.send(
                "input_ack",
                command_id=command.command_id,
                duplicate=status == "duplicate",
            )
        elif op == "answer":
            interaction_id = str(frame.get("interaction_id") or "")
            answered = session.answer_interaction(
                interaction_id,
                InteractionAnswer(
                    decision=str(frame.get("decision") or "deny"),
                    updated_input=frame.get("updated_input"),
                    message=frame.get("message"),
                ),
            )
            if not answered:
                # Expired (defer already fired) or unknown. Report, don't drop:
                # the host resolves it against its own pending-interaction view.
                #
                # Logged with both sides because "rejected" has three causes
                # that need opposite fixes: an id this box never minted (the
                # answer reached the wrong box, or a fresh session replaced the
                # parked one), an id it minted and has since dropped (the gate
                # expired), and one still pending but already resolved. The
                # frame alone cannot tell them apart.
                logger.info(
                    "answer rejected: asked=%s pending=%s",
                    interaction_id,
                    sorted(session.broker.pending_ids()),
                )
            # A direct response to the answer command, not a turn event: it has
            # no sequence and cannot leak into a later stream after reattach.
            await link.send({
                "op": "answer_ack",
                "interaction_id": interaction_id,
                "accepted": answered,
            })
        elif op == "interrupt":
            await session.interrupt(str(frame.get("command_id") or ""))
        elif op == "stop_task":
            task_id = str(frame.get("task_id") or "").strip()
            if not task_id:
                raise RunnerProtocolError("stop_task requires task_id")
            await session.stop_task(task_id)
        elif op == "event_persisted":
            sequence = frame.get("seq")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise RunnerProtocolError("event_persisted requires a positive seq")
            await session.sender.acknowledge_event_persistence(sequence)
        elif op == "set_permission_mode":
            request_id = str(frame.get("request_id") or "").strip()
            mode = str(frame.get("mode") or "").strip()
            if not request_id or not mode:
                raise RunnerProtocolError(
                    "set_permission_mode requires request_id and mode"
                )
            await session.set_permission_mode(mode)
            await link.send({
                "op": "permission_mode_ack",
                "request_id": request_id,
                "mode": mode,
            })
        elif op == "get_init_info":
            info = await session.get_server_info()
            await link.send({
                "op": "init_info",
                "info": _jsonable(info) if info is not None else None,
            })
        else:
            raise RunnerProtocolError(f"unknown op {op!r}")


# ── real wiring (in-box main) ───────────────────────────────────────────────


#: 4xx codes that ask for the retry rather than refusing the request.
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})


def _classify_flush_error(error: Any, *, path: str = "") -> Exception:
    """Decide whether an HTTP failure is a verdict or an outage.

    A 4xx is the platform saying it understood the request and refuses it, so
    the identical request will be refused identically — for a spool that retries
    forever, that is a loop, not resilience. 5xx and transport failures are the
    platform being unwell, which is exactly what the spool exists to outlast.
    """
    code = int(getattr(error, "code", 0) or 0)
    if not (400 <= code < 500) or code in _RETRYABLE_CLIENT_STATUSES:
        return error
    detail = ""
    try:
        detail = error.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001 — the status is the signal, the body is a hint
        detail = ""
    return _PermanentFlushRejection(
        f"POST {path} answered {code} {getattr(error, 'reason', '')}: {detail}"
    )


def transcript_payload_digest(
    key: dict[str, Any], entries: list[dict[str, Any]]
) -> str:
    """The digest an append declares over what it is sending.

    Canonical (``sort_keys``) JSON, so the value depends on the key/entry
    content and not on the order a serializer happened to emit fields in. The
    platform recomputes it from the received body and refuses a mismatch;
    ``tests/transcript_store_sequence_test.py`` holds the two definitions to the
    same output, since this file may not import the platform's copy.
    """
    return hashlib.sha256(
        json.dumps(
            {"key": key, "entries": entries},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class _HttpStoreTarget:
    """Flush/load against the platform transcript API, stdlib urllib on the
    default executor.

    Tracks the ``store_sequence`` the platform reports per SessionStore key and
    refuses one that moves backwards. That number is the durable transcript's
    position: a store that answers with a lower one has lost entries this runner
    was told were committed, and continuing would mirror the rest of the session
    onto a transcript missing its middle.
    """

    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self._base = base_url.rstrip("/")
        self._headers = headers
        self._store_sequences: dict[str, int] = {}

    @staticmethod
    def _key_identity(key: dict[str, Any]) -> str:
        return json.dumps(
            [key.get("project_key"), key.get("session_id"), key.get("subpath")],
            sort_keys=True,
        )

    def _record_store_sequence(self, key: dict[str, Any], data: dict[str, Any]) -> None:
        sequence = data.get("store_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise RuntimeError(
                f"transcript store response carries no usable store_sequence: "
                f"{sequence!r}"
            )
        identity = self._key_identity(key)
        previous = self._store_sequences.get(identity)
        if previous is not None and sequence < previous:
            raise RuntimeError(
                f"transcript store_sequence moved backwards ({previous} -> "
                f"{sequence}) for key {identity}"
            )
        self._store_sequences[identity] = sequence

    def confirmed_store_sequence(self) -> int:
        """The main scope's last confirmed store position, or 0 before any.

        A lower bound, deliberately. ``SpoolSessionStore.append`` acks the SDK
        once the batch is fsync'd locally and returns; the platform has not
        answered yet, and the value below only moves when a flush completes. So
        at any instant the spool may still hold batches this number does not
        cover, and it lags real durability by whatever the flusher has not
        drained.

        The error must stay one-directional. Too low means a consumer trims
        less than it could — wasted memory. Too high would mean trimming a
        prefix the store does not actually hold, which loses transcript. Anyone
        tightening this must keep it a lower bound; an optimistic value is the
        only change here that can destroy data.

        Subpath scopes carry independent sequences and are never mixed: this
        answers for the main scope alone.
        """
        for identity, sequence in self._store_sequences.items():
            if json.loads(identity)[2] in (None, ""):
                return sequence
        return 0

    def _post_blocking(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        req = Request(
            self._base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **self._headers},
            method="POST",
        )
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read()
        except HTTPError as exc:
            raise _classify_flush_error(exc, path=path) from exc
        return json.loads(body) if body else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._post_blocking, path, payload
        )

    def bind(self, platform_session_id: str) -> tuple[
        Callable[[dict[str, Any], list[dict[str, Any]], str], Awaitable[None]],
        Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]] | None]],
        Callable[[dict[str, Any]], Awaitable[list[str]]],
        Callable[[], int],
    ]:
        async def flush(
            key: dict[str, Any], entries: list[dict[str, Any]], append_id: str
        ) -> None:
            resp = await self._post(
                f"/api/v1/transcript/{platform_session_id}/append",
                {
                    "key": key,
                    "entries": entries,
                    "append_id": append_id,
                    "payload_sha256": transcript_payload_digest(key, entries),
                },
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            if not isinstance(data, dict):
                raise RuntimeError("transcript append response carries no data object")
            self._record_store_sequence(key, data)

        async def load(key: dict[str, Any]) -> list[dict[str, Any]] | None:
            resp = await self._post(
                f"/api/v1/transcript/{platform_session_id}/load", {"key": key}
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            if not isinstance(data, dict):
                return None
            self._record_store_sequence(key, data)
            entries = data.get("entries")
            return entries if isinstance(entries, list) else None

        async def list_subkeys(key: dict[str, Any]) -> list[str]:
            resp = await self._post(
                f"/api/v1/transcript/{platform_session_id}/list-subkeys",
                {"key": key},
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            subkeys = data.get("subkeys") if isinstance(data, dict) else None
            if not isinstance(subkeys, list) or any(
                not isinstance(item, str) or not item for item in subkeys
            ):
                raise RuntimeError(
                    "transcript list-subkeys response carries no usable subkeys"
                )
            return list(subkeys)

        return flush, load, list_subkeys, self.confirmed_store_sequence


class _DeferredHttpStoreTarget:
    """Transcript target whose platform writes begin only after activation.

    A prepared runtime has no Session and therefore no HTTP target at all. A
    direct resume is different: Claude's SDK reads SessionStore during
    ``connect()``, inside the prepare barrier, so that process receives its
    platform read source before connect. The matching flush closure remains
    disabled until activation proves the same Session and store binding.
    """

    def __init__(self) -> None:
        self._prepared_platform_session_id: str | None = None
        self._prepared_store_cfg: dict[str, Any] | None = None
        self._prepared_flush: Callable[
            [dict[str, Any], list[dict[str, Any]], str], Awaitable[None]
        ] | None = None
        self._flush: Callable[
            [dict[str, Any], list[dict[str, Any]], str], Awaitable[None]
        ] | None = None
        self._load: Callable[
            [dict[str, Any]], Awaitable[list[dict[str, Any]] | None]
        ] | None = None
        self._list_subkeys: Callable[
            [dict[str, Any]], Awaitable[list[str]]
        ] | None = None
        self._confirmed_sequence: Callable[[], int] | None = None

    @staticmethod
    def _normalize_binding(
        platform_session_id: str,
        store_cfg: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        target_session = str(platform_session_id or "").strip()
        if not target_session:
            raise RunnerProtocolError("transcript binding requires session_id")
        if not isinstance(store_cfg, dict):
            raise RunnerProtocolError("transcript store.base_url is required")
        base_url = str(store_cfg.get("base_url") or "").strip()
        if not base_url:
            raise RunnerProtocolError("transcript store.base_url is required")
        headers = store_cfg.get("headers") or {}
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise RunnerProtocolError(
                "transcript store.headers must contain only string pairs"
            )
        return target_session, {
            "base_url": base_url,
            "headers": dict(headers),
        }

    @staticmethod
    def _target(
        store_cfg: dict[str, Any],
    ) -> _HttpStoreTarget:
        return _HttpStoreTarget(
            str(store_cfg["base_url"]),
            dict(store_cfg["headers"]),
        )

    def bind_resume_source(
        self,
        platform_session_id: str,
        store_cfg: dict[str, Any] | None,
    ) -> None:
        """Enable durable reads for one resume without enabling HTTP flush."""

        if self._prepared_platform_session_id is not None or self._flush is not None:
            raise RunnerProtocolError("resume transcript source is already bound")
        target_session, normalized_store = self._normalize_binding(
            platform_session_id,
            store_cfg,
        )
        target = self._target(normalized_store)
        (
            self._prepared_flush,
            self._load,
            self._list_subkeys,
            self._confirmed_sequence,
        ) = target.bind(target_session)
        self._prepared_platform_session_id = target_session
        self._prepared_store_cfg = normalized_store

    def activate(
        self,
        platform_session_id: str,
        store_cfg: dict[str, Any] | None,
    ) -> None:
        if self._flush is not None:
            raise RunnerProtocolError("transcript target is already bound")
        target_session, normalized_store = self._normalize_binding(
            platform_session_id,
            store_cfg,
        )
        if self._prepared_platform_session_id is not None:
            if (
                target_session != self._prepared_platform_session_id
                or normalized_store != self._prepared_store_cfg
            ):
                raise RunnerProtocolError(
                    "activation transcript binding does not match resume source"
                )
            if self._prepared_flush is None:
                raise RunnerProtocolError("resume transcript source is incomplete")
            self._flush = self._prepared_flush
            return
        target = self._target(normalized_store)
        (
            self._flush,
            self._load,
            self._list_subkeys,
            self._confirmed_sequence,
        ) = target.bind(target_session)

    async def flush(
        self,
        key: dict[str, Any],
        entries: list[dict[str, Any]],
        append_id: str,
    ) -> None:
        if self._flush is None:
            raise RunnerProtocolError(
                "prepared transcript cannot flush before Session activation"
            )
        await self._flush(key, entries, append_id)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        if self._load is None:
            return None
        return await self._load(key)

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        if self._list_subkeys is None:
            return []
        return await self._list_subkeys(key)

    def confirmed_store_sequence(self) -> int:
        return self._confirmed_sequence() if self._confirmed_sequence else 0


def _ensure_working_directory(options: Any) -> None:
    """Make sure ``options.cwd`` exists before the CLI is spawned into it.

    The SDK refuses to spawn at all when the working directory is missing
    ("Working directory does not exist"), and whether it exists depends on how
    this box was provisioned — which this process cannot see:

    * a box the host built for this session had its workspace directory created
      at create time, over the box's command channel;
    * a box that was pre-created before any session claimed it cannot have a
      per-session directory baked into its image.

    The process that owns the spawn is the only one that can guarantee its own
    working directory in both cases, so it does. Idempotent, and a no-op on the
    normal path: the image bakes the workload user's workspace, so the directory
    is already there and already belongs to that user.

    A directory this function creates belongs to whoever runs the runner (root),
    which the CLI's user could not then write — so it is handed over immediately.
    A directory that already exists is left exactly as it is, whoever owns it.
    That is a boundary, not caution: a pre-existing workspace is either
    the image's (already correct) or a mount, and a mount's contents and
    permissions belong to whoever mounted it. Chowning someone else's mounted
    tree to this box's uid would be this process reaching outside its box.
    """
    cwd = str(getattr(options, "cwd", "") or "").strip()
    if not cwd:
        return
    if os.path.isdir(cwd):
        return
    os.makedirs(cwd, exist_ok=True)
    _adopt_directory(cwd, options)


def _adopt_directory(path: str, options: Any) -> None:
    """Give the CLI's user ownership of a directory this process just created.

    Best effort by design: this runs on the spawn path, and a box whose user
    cannot be resolved, or whose runner is not privileged enough to chown,
    must still start. What it must not do is fail the session over a directory
    the CLI may well be able to use anyway — the loud failure belongs to the
    first write that actually cannot proceed, which names the path.
    """
    env = (getattr(options, "env", None) or {}) if options is not None else {}
    user = str(env.get("USER") or "").strip()
    if not user:
        return
    try:
        import pwd

        entry = pwd.getpwnam(user)
        os.chown(path, entry.pw_uid, entry.pw_gid)
    except Exception:
        logger.info(
            "runner: could not give %r ownership of %s; leaving it as it is",
            user,
            path,
            exc_info=True,
        )


def _wire_value_matches_annotation(value: Any, annotation: Any) -> bool:
    """Whether a JSON value can inhabit one branch of an SDK annotation."""

    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(
            member is not type(None)
            and _wire_value_matches_annotation(value, member)
            for member in get_args(annotation)
        )
    if origin is dict:
        return isinstance(value, dict)
    if origin in (list, tuple):
        return isinstance(value, list)
    if dataclasses.is_dataclass(annotation):
        return isinstance(value, dict)
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    return True


def _materialize_sdk_wire_value(value: Any, annotation: Any, *, path: str) -> Any:
    """Restore dataclass values using the installed SDK's own annotations.

    The host transports JSON, while some SDK options (currently ``agents``)
    require dataclass instances. Reading the annotation here keeps a new JSON
    field automatic and a new dataclass shape self-describing; core never
    transcribes the vendor's field list or nested constructor.
    """

    if value is None or annotation is Any:
        return value
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        candidates = [
            member
            for member in get_args(annotation)
            if member is not type(None)
            and _wire_value_matches_annotation(value, member)
        ]
        if not candidates:
            return value
        last_error: RunnerProtocolError | None = None
        for member in candidates:
            try:
                return _materialize_sdk_wire_value(value, member, path=path)
            except RunnerProtocolError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return value
    if origin is dict and isinstance(value, dict):
        args = get_args(annotation)
        value_annotation = args[1] if len(args) == 2 else Any
        return {
            str(key): _materialize_sdk_wire_value(
                item,
                value_annotation,
                path=f"{path}.{key}",
            )
            for key, item in value.items()
        }
    if origin is list and isinstance(value, list):
        args = get_args(annotation)
        item_annotation = args[0] if args else Any
        return [
            _materialize_sdk_wire_value(
                item,
                item_annotation,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if origin is tuple and isinstance(value, list):
        args = get_args(annotation)
        item_annotation = args[0] if args else Any
        return tuple(
            _materialize_sdk_wire_value(
                item,
                item_annotation,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        )
    if dataclasses.is_dataclass(annotation):
        if not isinstance(value, dict):
            raise RunnerProtocolError(f"{path} must be an object")
        declared_fields = {field.name for field in dataclasses.fields(annotation)}
        unknown = sorted(set(value) - declared_fields)
        if unknown:
            raise RunnerProtocolError(
                f"{path} has fields absent from the installed SDK: {unknown!r}"
            )
        annotations = get_type_hints(annotation)
        materialized = {
            key: _materialize_sdk_wire_value(
                item,
                annotations.get(key, Any),
                path=f"{path}.{key}",
            )
            for key, item in value.items()
        }
        try:
            return annotation(**materialized)
        except (TypeError, ValueError) as exc:
            raise RunnerProtocolError(
                f"{path} does not match the installed SDK: {exc}"
            ) from exc
    return value


def _real_session_factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
    """Build one unclaimed Claude process and its local transcript spool."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

    slot_id = str(opening["slot_id"])
    options_in = opening.get("options") or {}
    store_target = _DeferredHttpStoreTarget()
    resume_session_key = str(options_in.get("resume") or "").strip()
    resume_transcript = options_in.get("resume_transcript")
    if resume_session_key:
        if not isinstance(resume_transcript, dict):
            raise RunnerProtocolError(
                "prepare.options.resume requires resume_transcript"
            )
        unknown_resume_fields = set(resume_transcript) - {
            "platform_session_id",
            "store",
        }
        if unknown_resume_fields:
            raise RunnerProtocolError(
                "prepare.options.resume_transcript carries unknown keys "
                f"{sorted(unknown_resume_fields)!r}"
            )
        store_target.bind_resume_source(
            str(resume_transcript.get("platform_session_id") or ""),
            (
                resume_transcript.get("store")
                if isinstance(resume_transcript.get("store"), dict)
                else None
            ),
        )
    elif resume_transcript is not None:
        raise RunnerProtocolError(
            "prepare.options.resume_transcript requires resume"
        )
    store = SpoolSessionStore(
        os.environ.get("ASTRABOX_RUNNER_SPOOL_DIR", "/tmp/astrabox-runner-spool"),
        flush_fn=store_target.flush,
        load_fn=store_target.load,
        list_subkeys_fn=store_target.list_subkeys,
        sequence_fn=store_target.confirmed_store_sequence,
    )

    sdk_option_keys = frozenset(
        field.name for field in dataclasses.fields(ClaudeAgentOptions)
    )
    unknown = set(options_in) - sdk_option_keys - RUNNER_OPTION_KEYS
    if unknown:
        raise RunnerProtocolError(
            f"prepare.options carries unknown keys {sorted(unknown)!r} — "
            f"the runner refuses options it would silently drop"
        )
    controlled = set(options_in) & _RUNNER_CONTROLLED_SDK_OPTION_KEYS
    if controlled:
        raise RunnerProtocolError(
            "prepare.options carries runner-controlled keys "
            f"{sorted(controlled)!r}"
        )

    session: RunnerSession | None = None

    def client_factory(broker: InteractionBroker, *, resume: str | None = None) -> SdkSession:
        if session is None:
            raise RunnerProtocolError("runner session is unavailable during SDK setup")
        option_annotations = get_type_hints(ClaudeAgentOptions)
        sdk_kwargs: dict[str, Any] = {
            key: _materialize_sdk_wire_value(
                options_in[key],
                option_annotations.get(key, Any),
                path=f"prepare.options.{key}",
            )
            for key in sdk_option_keys - _RUNNER_CONTROLLED_SDK_OPTION_KEYS
            if options_in.get(key) is not None
        }
        sdk_kwargs.setdefault("permission_mode", "default")
        if resume:
            sdk_kwargs["resume"] = resume
        configured_hooks = {
            "PreToolUse": [
                # The broker returns a plain dict shaped exactly like the
                # SDK's SyncHookJSONOutput TypedDict (spike-verified);
                # cast bridges dict[str, Any] to the TypedDict union.
                HookMatcher(
                    matcher=None,
                    hooks=[cast("Any", broker.pre_tool_use)],
                    # Without this the matcher runs on the vendor's 60s
                    # default, which expires a gate while a person is still
                    # reading it. The broker defers before this elapses.
                    timeout=INTERACTION_HOOK_TIMEOUT_S,
                )
            ]
        }
        options = ClaudeAgentOptions(
            **sdk_kwargs,
            cli_path=CLAUDE_CLI_PATH,
            session_store=store,
            # A background task can settle after the launching turn's Result.
            # Batched mirroring withholds that terminal transcript entry until
            # the automatic follow-up turn produces another Result, leaving the
            # platform blind if that model turn stalls. Each vendor mirror frame
            # must reach the fsync spool as it arrives.
            session_store_flush="eager",
            hooks=session._with_prompt_consumption_hook(configured_hooks),
            # The SDK owns the transport detail: this callback makes it add
            # ``--permission-prompt-tool stdio`` and routes control requests
            # here. Setting permission_prompt_tool_name in the runner as well is
            # explicitly invalid in the vendor API.
            can_use_tool=broker.can_use_tool,
        )
        _ensure_working_directory(options)
        return ClaudeSDKClient(options=options)

    wait_s = float(options_in.get("interaction_wait_s") or DEFAULT_INTERACTION_WAIT_S)
    session = RunnerSession(
        session_id=slot_id,
        link=link,
        client_factory=client_factory,
        resume_client_factory=lambda broker, key: client_factory(broker, resume=key),
        store=store,
        activation_callback=store_target.activate,
        interaction_wait_s=wait_s,
        permission_mode=str(options_in.get("permission_mode") or "default"),
    )
    return session


async def main() -> None:
    import signal

    logging.basicConfig(level=logging.INFO)
    server = RunnerWsServer(
        port=int(os.environ.get("ASTRABOX_RUNNER_PORT", "8000")),
        session_factory=_real_session_factory,
    )
    await server.start()
    logger.info("sandbox runner listening on :%d (%s)", server.port, RUNNER_PROTOCOL)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
