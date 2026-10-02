"""One reader per engine process; a turn is attribution, not read scope.

An engine runtime keeps producing after the platform's turn has ended: a
sub-agent finishes and wakes the parent, an extension injects a message that
triggers a model run, a queued follow-up drains. Every one of those arrives
on the same wire as a platform turn's output. What decides whether a reader
sees them is not the engine but who is reading — and a reader that only runs
while a platform turn is open leaves everything else in the inbox.

This relay is that reader, for the life of the process. It takes each record
once and routes it by the engine's own boundaries:

* a run the engine starts because the platform delivered an input belongs to
  that platform turn, and is handed to the turn's own iterator through
  ``turn_inbox``;
* any other run is the engine's own response, published through the
  platform's :class:`ResidentOutputSink` as it streams — opened at the run's
  start, closed at the engine's own terminal, heartbeated in between;
* records outside any run that carry child-run facts (a sub-agent status
  push, a reply to a child read) are persisted as native engine messages, the
  session-scoped path the child-run view already folds.

Which record starts a run, which ends it, how a record translates, and what
its sequence is are the engine's answers, given through :class:`RelaySeam`.
The internal runtime has the same shape: one collector task iterates the SDK
for the whole session, every message is numbered, and a response id is chosen
at publish time from whether a platform input was consumed. Nothing here
decides whether a run may continue or has finished.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineEventSink,
    ResidentOutputCheckpoint,
    ResidentOutputSink,
    ResidentResponseHandle,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    EngineTurnEmission,
    InteractionRequested,
    TurnTerminal,
    emission_from_translated_frame,
)

logger = get_logger(__name__)

#: How often an open engine-owned response stamps the conversation slot it
#: holds. The same cadence the Claude adapter keeps.
RESIDENT_HEARTBEAT_INTERVAL_S = 5.0


@dataclass(frozen=True)
class CountedRecord:
    """One record with the position the reading process gave it.

    For a link that numbers nothing and replays nothing on a reconnect, the
    position is this process's own count, which is all a journal row needs.
    """

    sequence: int
    record: dict[str, Any]


class RelaySeam(Protocol):
    """What one engine tells the relay about its own wire."""

    engine_kind: str

    def sequence(self, wire: Any) -> int:
        """The engine's monotonic position for this record."""
        ...

    def record(self, wire: Any) -> dict[str, Any]:
        """The record itself, as the engine wrote it."""
        ...

    def starts_run(self, record: dict[str, Any]) -> bool:
        """Whether this record opens a model run."""
        ...

    def platform_run(
        self, record: dict[str, Any], *, pending: bool, active: bool,
    ) -> bool | None:
        """Whether the opening run belongs to a platform input.

        None defers attribution until a later native record proves it. The
        relay retains the opening records in order until the engine answers.
        """
        ...

    def settles_run(self, record: dict[str, Any]) -> bool:
        """Whether this record is the engine's own end of a run."""
        ...

    def handoff_to_platform(
        self, translator: Any, record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """The preceding response's terminal at a native queued-input boundary.

        Called only while an external input awaits output and an engine-owned
        response is open. None keeps the record with that response.
        """
        ...

    def response_id(self, record: dict[str, Any], sequence: int) -> str:
        """The identity an engine-owned response is published under."""
        ...

    def new_translator(self) -> Any: ...

    def translate(self, translator: Any, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Platform frames for one record, in order; a ``result`` frame ends the run."""
        ...

    async def child_facts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Session-scoped child-run frames this record changes, if any.

        Asynchronous because some engines learn a child's state by asking
        (Codex reads the child's thread); others fold what the record itself
        carries and answer at once.
        """
        ...

    def native_records(self) -> list[dict[str, Any]]:
        """The engine's own records behind the facts just folded, drained.

        What the relay persists when no run is open, and what the adapter's
        ``durable_child_resource_facts`` folds back: for pi the status push or
        inspect reply itself, for Codex the thread document it read.
        """
        ...

    def owed_child_reads(self) -> list[dict[str, Any]]:
        """Commands to send the engine for child reads the facts made owed."""
        ...

    def carries_child_facts(self, record: dict[str, Any]) -> bool:
        """Whether this record is one the child-run view can fold durably."""
        ...

    def interaction(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """The ``interaction.request`` frame this record opens, if any."""
        ...


@dataclass
class _Resident:
    response_id: str
    handle: ResidentResponseHandle | None
    translator: Any
    #: Sequences at or before this were journaled already: translated for
    #: state, never published again.
    after_sequence: int | None = None
    heartbeat: asyncio.Task[None] | None = field(default=None, repr=False)


class ResidentRelay:
    """Route one engine's records for the life of its process."""

    def __init__(
        self,
        *,
        seam: RelaySeam,
        session_id: str,
        engine_session_key: str | None,
        next_record: Callable[[], Awaitable[Any]],
        send_command: Callable[[dict[str, Any]], Awaitable[Any]],
        current_sequence: Callable[[], Awaitable[int]],
        resident_output_sink: ResidentOutputSink | None,
        event_sink: EngineEventSink | None,
        checkpoint: ResidentOutputCheckpoint | None = None,
    ) -> None:
        self._seam = seam
        self._session_id = str(session_id)
        self._engine_session_key = engine_session_key
        self._next_record = next_record
        self._send_command = send_command
        self._current_sequence = current_sequence
        self._sink = resident_output_sink
        self._checkpoint = checkpoint
        self._event_sink = event_sink
        #: Records that belong to the platform's own turn, in wire order. The
        #: turn iterator drains this instead of the process.
        self.turn_inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._platform_pending = False
        self._platform_active = False
        #: Platform inputs written so far, and the queued run starts each
        #: pending input was given, with that input's number.
        self._submissions = 0
        self._attributed_starts: list[tuple[Any, int]] = []
        self._run_prelude: list[Any] = []
        self._resident: _Resident | None = None
        #: Sequences below this are wire replay from before this relay read
        #: the process. They may still belong to a platform turn being
        #: re-attached, but never open an engine-owned response.
        self._floor = 0
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name=f"resident-relay-{self._seam.engine_kind}-{self._session_id}"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        await self._stop_heartbeat()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    # ── attribution ──────────────────────────────────────────────────────
    def platform_input_submitted(self) -> int:
        """A platform input is on its way to the engine.

        Marked before the input is written, not after the engine answers it:
        the engine's acknowledgement and the run's first record are two
        records on the same wire, and this relay may take the second before
        the sender's continuation has taken the first. An input queued behind
        a run in progress starts its run only after that run settles, which
        the routing already honours.

        Returns the input's number, which :meth:`platform_turn_begins` takes.
        """

        self._platform_pending = True
        self._submissions += 1
        return self._submissions

    def platform_turn_begins(self, submission: int | None) -> None:
        """A platform turn opens for the input with this number.

        The platform opens a turn only after its previous one settled, and
        nothing reads ``turn_inbox`` before the new turn's stream does. What
        is queued ahead of this turn's own run therefore has no reader: a
        turn the platform settled while it was parked on an interaction (a
        stop) leaves the rest of its run here, and a stream that read it
        would take that run's end for its own. It is dropped. If this turn's
        run has not started yet, the remaining records of a run still open
        are routed as if no platform turn were active. A wire failure is
        kept: it is the process's, and the next reader must learn it in
        order.

        ``submission`` is the number :meth:`platform_input_submitted` gave the
        input, or None when the input is written after this call.
        """

        first = self._submissions + 1 if submission is None else submission
        queued: list[Any] = []
        while not self.turn_inbox.empty():
            queued.append(self.turn_inbox.get_nowait())
        numbers = {id(wire): number for wire, number in self._attributed_starts}
        keep_from = next(
            (
                index
                for index, item in enumerate(queued)
                if numbers.get(id(item), 0) >= first
            ),
            None,
        )
        kept = [
            item
            for index, item in enumerate(queued)
            if isinstance(item, BaseException)
            or (keep_from is not None and index >= keep_from)
        ]
        for item in kept:
            self.turn_inbox.put_nowait(item)
        self._attributed_starts = [
            (wire, number)
            for wire, number in self._attributed_starts
            if any(wire is item for item in kept)
        ]
        if keep_from is None:
            self._platform_active = False

    def platform_input_rejected(self) -> None:
        """The engine refused the input before acceptance; no run will follow it."""

        self._platform_pending = False

    def platform_turn_reattached(self) -> None:
        """A consumer owns the active run, including a command with no start event."""

        self._platform_active = True

    def platform_turn_finished(self) -> None:
        """The adapter observed its command's native completion boundary."""

        self._platform_active = False
        self._platform_pending = False

    # ── the loop ─────────────────────────────────────────────────────────
    async def _run(self) -> None:
        try:
            await self._restore()
            while True:
                wire = await self._next_record()
                await self._route(wire)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — the loop's death is the fact to carry
            self._failure = exc
            # A platform turn waiting on the inbox learns the same way it
            # would have from the process: the exception arrives in order.
            await self.turn_inbox.put(exc)
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "resident relay stopped: session=%s engine=%s error=%s",
                self._session_id,
                self._seam.engine_kind,
                exc,
            )
        finally:
            await self._stop_heartbeat()

    async def _restore(self) -> None:
        self._floor = int(await self._current_sequence())
        sink = self._sink
        if sink is None:
            return
        checkpoint = self._checkpoint or await sink.restore_resident_output(
            engine_kind=self._seam.engine_kind
        )
        if checkpoint.replay_after_sequence is not None:
            self._floor = checkpoint.replay_after_sequence + 1
        if checkpoint.external_turn_active:
            self._platform_active = True
        response_id = str(checkpoint.open_response_id or "").strip()
        if not response_id:
            return
        if checkpoint.after_sequence is None or checkpoint.boundary_sequence is None:
            raise RuntimeError(
                "platform reports an open engine-owned response with no journaled "
                f"output: session={self._session_id} response={response_id}"
            )
        self._resident = _Resident(
            response_id=response_id,
            handle=ResidentResponseHandle(
                response_id=response_id, owns_slot=checkpoint.open_response_owns_slot,
            ),
            translator=self._seam.new_translator(),
            after_sequence=int(checkpoint.after_sequence),
        )
        # Replay reaches back to the response's boundary, so the translator
        # rebuilds its state from the same records it saw the first time.
        self._floor = min(self._floor, int(checkpoint.boundary_sequence))
        self._start_heartbeat()
        logger.info(
            "resident relay resumed an open response: session=%s response=%s after=%s",
            self._session_id,
            response_id,
            checkpoint.after_sequence,
        )

    async def _route(self, wire: Any) -> None:
        seam = self._seam
        record = seam.record(wire)
        sequence = seam.sequence(wire)

        # A replayed start belongs to the response already in custody. The
        # next input cannot claim it merely because that input is pending.
        if self._resident is not None:
            if self._platform_pending or self._platform_active:
                frame = seam.handoff_to_platform(self._resident.translator, record)
                if frame is not None:
                    terminal = emission_from_translated_frame(frame)
                    if not isinstance(terminal, TurnTerminal):
                        raise RuntimeError("engine input handoff did not end the preceding response")
                    await self._close(self._resident, terminal, sequence)
                    self._platform_active = True
                    if self._platform_pending:
                        self._platform_pending = False
                        self._attributed_starts.append((wire, self._submissions))
                    await self.turn_inbox.put(wire)
                    return
            await self._observe(self._resident, record, sequence)
            return

        if self._run_prelude or (
            seam.starts_run(record)
            and (sequence >= self._floor or self._platform_active or self._platform_pending)
        ):
            self._run_prelude.append(wire)
            attribution = seam.platform_run(
                record, pending=self._platform_pending, active=self._platform_active,
            )
            if attribution is None:
                return
            prelude, self._run_prelude = self._run_prelude, []
            start = prelude[0]
            self._platform_active = attribution
            if attribution:
                if self._platform_pending:
                    self._platform_pending = False
                    self._attributed_starts.append((start, self._submissions))
                for item in prelude:
                    await self.turn_inbox.put(item)
                if seam.settles_run(record):
                    self._platform_active = False
            else:
                await self._open(seam.record(start), seam.sequence(start))
                resident = self._resident
                assert resident is not None
                for item in prelude:
                    await self._observe(resident, seam.record(item), seam.sequence(item))
            return

        if self._platform_active:
            await self.turn_inbox.put(wire)
            if seam.settles_run(record):
                self._platform_active = False
            return

        if sequence < self._floor:
            return
        await self._observe_idle(record, sequence)

    # ── engine-owned responses ───────────────────────────────────────────
    async def _open(self, record: dict[str, Any], sequence: int) -> None:
        sink = self._sink
        response_id = self._seam.response_id(record, sequence)
        handle: ResidentResponseHandle | None = None
        if sink is not None:
            handle = await sink.open_resident_response(
                engine_kind=self._seam.engine_kind,
                response_id=response_id,
                engine_session_key=self._engine_session_key,
                causation_id=f"{self._seam.engine_kind}:run:{response_id}",
                native_message=dict(record),
                runner_sequence=sequence,
            )
        self._resident = _Resident(
            response_id=response_id,
            handle=handle,
            translator=self._seam.new_translator(),
        )
        if handle is not None:
            self._start_heartbeat()
            logger.info(
                "resident relay opened a response: session=%s response=%s seq=%s owns_slot=%s",
                self._session_id,
                response_id,
                sequence,
                handle.owns_slot,
            )

    async def _observe(self, resident: _Resident, record: dict[str, Any], sequence: int) -> None:
        seam = self._seam
        publishable = resident.handle is not None and (
            resident.after_sequence is None or sequence > resident.after_sequence
        )
        emissions: list[EngineTurnEmission] = []
        terminal: TurnTerminal | None = None
        interaction: InteractionRequested | None = None
        for frame in await seam.child_facts(record):
            emissions.append(emission_from_translated_frame(frame))
        seam.native_records()
        request = seam.interaction(record)
        if request is not None:
            emission = emission_from_translated_frame(request)
            if isinstance(emission, InteractionRequested):
                interaction = emission
        for frame in seam.translate(resident.translator, record):
            emission = emission_from_translated_frame(frame)
            if isinstance(emission, TurnTerminal):
                terminal = emission
                continue
            emissions.append(emission)
        sink = self._sink
        if publishable and emissions and sink is not None and resident.handle is not None:
            await sink.publish_resident_output(
                resident.handle, emissions, engine_sequence_number=sequence
            )
        for payload in seam.owed_child_reads():
            await self._send_command(payload)
        if interaction is not None and publishable and sink is not None and resident.handle is not None:
            await sink.open_resident_interaction(
                resident.handle,
                interaction_id=interaction.interaction_id,
                contract=dict(interaction.contract),
                engine_session_key=self._engine_session_key,
                engine_sequence_number=sequence,
            )
        if terminal is not None:
            await self._close(resident, terminal, sequence)

    async def _close(self, resident: _Resident, terminal: TurnTerminal, sequence: int) -> None:
        self._resident = None
        await self._stop_heartbeat(resident)
        sink = self._sink
        if sink is None or resident.handle is None:
            return
        await sink.close_resident_response(
            resident.handle, terminal=terminal, engine_sequence_number=sequence
        )
        logger.info(
            "resident relay closed a response: session=%s response=%s outcome=%s seq=%s",
            self._session_id,
            resident.response_id,
            terminal.outcome,
            sequence,
        )

    # ── between runs ─────────────────────────────────────────────────────
    async def _observe_idle(self, record: dict[str, Any], sequence: int) -> None:
        """A record outside any run: only child-run facts have somewhere to go.

        They are persisted as the engine's native message, which is the
        session-scoped source the child-run view folds through the adapter's
        ``durable_child_resource_facts``; a status that changed while nothing
        was running is therefore visible on the next read without a response
        to hang it on. The facts themselves are folded here too so the
        projector's own state — which child it has seen, which read it owes —
        stays current, and any read owed goes out now.
        """

        seam = self._seam
        if not seam.carries_child_facts(record):
            return
        # What the seam observed before this record, while a platform turn
        # folded its own children, reached the platform as that turn's facts
        # but not the journal. The durable fold replays the journal alone, and
        # Codex learns a child only from the thread document it read, so a
        # child found during the turn needs that document ahead of its later
        # notifications, whether or not this record changes anything.
        carried = seam.native_records()
        facts = await seam.child_facts(record)
        natives = seam.native_records()
        # A status push the package repeats about once a second says nothing
        # new most of the time. Only a record that changed a child is worth a
        # journal row; the fold re-derives every state from the changes alone.
        journal = [*carried, *(natives if facts else [])]
        if journal and self._event_sink is not None:
            for offset, native in enumerate(journal):
                await self._event_sink.persist_event(
                    engine_kind=seam.engine_kind,
                    causation_id=f"{seam.engine_kind}:idle:{sequence}:{offset}",
                    payload={"runner_sequence": int(sequence), "message": dict(native)},
                )
        for payload in seam.owed_child_reads():
            await self._send_command(payload)

    # ── heartbeat ────────────────────────────────────────────────────────
    def _start_heartbeat(self) -> None:
        resident = self._resident
        if resident is None or resident.handle is None or not resident.handle.owns_slot:
            return
        if resident.heartbeat is not None and not resident.heartbeat.done():
            return
        resident.heartbeat = asyncio.create_task(
            self._heartbeat_loop(resident.handle),
            name=f"resident-relay-heartbeat-{self._session_id}",
        )

    async def _stop_heartbeat(self, resident: _Resident | None = None) -> None:
        target = resident if resident is not None else self._resident
        if target is None:
            return
        task = target.heartbeat
        target.heartbeat = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _heartbeat_loop(self, handle: ResidentResponseHandle) -> None:
        sink = self._sink
        assert sink is not None
        while True:
            await asyncio.sleep(RESIDENT_HEARTBEAT_INTERVAL_S)
            try:
                alive = await sink.heartbeat_resident_response(handle)
            except Exception as exc:  # noqa: BLE001 — a missed stamp is retried next tick
                logger.warning(
                    "resident relay heartbeat failed session=%s response=%s: %s",
                    self._session_id,
                    handle.response_id,
                    exc,
                )
                continue
            if not alive:
                return


__all__ = ["RESIDENT_HEARTBEAT_INTERVAL_S", "RelaySeam", "ResidentRelay"]
