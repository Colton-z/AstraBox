"""Pi sub-agent vocabulary at the engine-neutral child-run seam.

Pi ships no sub-agents. Its own guide says so — "It intentionally does not
include built-in MCP, sub-agents, permission popups, plan mode, to-dos, or
background bash. You can build or install those workflows as extensions or
packages" — so a Pi child run is whatever the installed package emits, and
this module reads exactly one: ``pi-subagents``, pinned into the image.

That package publishes a contract addressed to an RPC host. Its
``Host inspection protocol (RPC)`` states that "RPC hosts receive live async
status through the bounded ``subagent-async`` widget (``PI_SUBAGENT_ASYNC_JSON:``
payload)", and that consumers "should read these JSON files instead of scraping
terminal output". The snapshot is therefore the lifecycle source here: it names
each run and child, carries the state in the package's own words, and keeps
reporting a run after it settles, because the projection is built from every
retained async job of the session rather than from the live ones.

Nothing else on the wire can close a background child. ``async`` is default-on,
so the delegating tool call answers as soon as the run exists and the children
outlive it; the package's completion events travel on ``pi.events``, which it
documents as "in-process only", never reaching a separate host process.

A foreground launch (``async: false``) is the other half, and it is the tool
call's own business: it blocks the parent, and its ``details.results`` rows are
settled by the time the call ends.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from astrabox.core.service.orchestrator.engine.child_runs import (
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.pi_events import raw_event_frame

PI_ENGINE_KIND = "pi"

#: The tool the pinned package registers. A different sub-agent package would
#: name its tool differently, which is why the image pins this one.
SUBAGENT_TOOL_NAME = "subagent"

#: The widget the package addresses RPC hosts through, and the marker its one
#: line carries. Both are the package's own constants
#: (``ASYNC_STATUS_SNAPSHOT_WIDGET_PREFIX`` and the widget key it is set on).
ASYNC_WIDGET_KEY = "subagent-async"
ASYNC_SNAPSHOT_PREFIX = "PI_SUBAGENT_ASYNC_JSON:"

#: ``ASYNC_STATUS_SNAPSHOT_KIND`` / ``ASYNC_STATUS_SNAPSHOT_VERSION``. A payload
#: announcing anything else is not this contract and is left alone.
ASYNC_SNAPSHOT_KIND = "pi-subagents.async-status-snapshot"
ASYNC_SNAPSHOT_VERSION = 1

#: ``AsyncStatusSnapshotState``, verbatim. The seam carries the package's own
#: word for a child's state; a state outside this set is a contract this
#: version does not know, not something to guess at.
_SNAPSHOT_STATES = frozenset(
    {
        "queued",
        "running",
        "complete",
        "failed",
        "partial",
        "paused",
        "stopped",
        "rejected",
    }
)

#: The states a run has left work in. Everything else has finished, and the
#: package retains it in the snapshot rather than dropping it, so a terminal
#: state is what closes a child run.
_OPEN_STATES = frozenset({"queued", "running", "paused"})

#: The states the package will act on a stop request for: it "stops only
#: pending or running children" and "the request is rejected for anything else
#: instead of widening to a run-level stop".
_STOPPABLE_STATES = frozenset({"queued", "running"})

#: The command that stops a run, or one child of it. The vendor states the
#: difference: bare, it "ends a current-session top-level async run"; with a
#: child id it stops that child "while the rest continue".
STOP_COMMAND = "/subagents-stop"

#: The seam carries one opaque string; the vendor addresses a run, optionally
#: plus a child. This separator joins them and never appears in a run id.
_REFERENCE_SEPARATOR = "/"

#: How a child id is spelled for a row that the vendor reports by position.
#: Its own resolver accepts `[step.childId, step.workflowKey, step.runId,
#: "step:<index>"]`, and the snapshot spells its step nodes the same way.
_STEP_ID_PREFIX = "step:"

#: The package's on-demand read of one child: "a child's delegated task,
#: transcript window, or final output". It runs inline over RPC without a
#: model turn, and answers on its own widget, correlated by the request id the
#: host chose. Constants are the package's (`INSPECT_*` in its inspect module).
INSPECT_COMMAND = "/subagents-inspect-rpc"
INSPECT_WIDGET_KEY = "subagent-inspect"
INSPECT_REPLY_PREFIX = "PI_SUBAGENT_INSPECT_JSON:"
INSPECT_REPLY_KIND = "pi-subagents.inspect-reply"
INSPECT_REPLY_VERSION = 1

#: The package caps a reply's message window at 200; asking for the cap is
#: asking for everything it will give.
_INSPECT_MESSAGE_LINES = 200

#: Reply errors the package documents as answers to a well-formed request:
#: the run is unknown, cleaned up, another session's, or there is no session.
#: Each is a statement about the run, not about the request, so none of them
#: is a protocol violation here.
_INSPECT_ERRORS_DROPPED = frozenset(
    {"not_found", "stale", "foreign_session", "no_active_session"}
)

#: The roles of what a child said. Pi's reply also carries `toolResult` and
#: `toolCall` rows; those are working detail and stay out.
_CHILD_MESSAGE_ROLES = frozenset({"assistant", "user"})


class PiChildRunError(RuntimeError):
    """The sub-agent package reported a child outside its declared shape."""


@dataclass
class _ChildState:
    """What the console has already been told about one child."""

    reference: str
    parent_reference: str | None = None
    label: str | None = None
    task_type: str | None = None
    opened: bool = False
    closed: bool = False
    status: str | None = None
    #: Whether the console is owed a read of this child's own words: set
    #: whenever the snapshot said something new about it, cleared when the
    #: request goes out.
    inspect_pending: bool = False
    #: Native activity fields, excluding the widget's publication timestamp.
    inspect_activity: str | None = None
    #: A hidden single step is read by its native id, not by the run's id.
    inspect_reference: str | None = None
    reported_messages: set[str] = field(default_factory=set)
    final_output_reported: bool = False


def step_child_id(index: int) -> str:
    """The child id for a row the vendor reports by position."""

    return f"{_STEP_ID_PREFIX}{int(index)}"


def child_reference(run_id: str, child_id: str | None = None) -> str:
    """One opaque seam reference naming a run, and a child within it.

    A launch the vendor reports without children is one child run: the run.
    Children are addressed by the node id the snapshot gave the host, which is
    exactly what the package's stop takes back.
    """

    clean_run = str(run_id or "").strip()
    if not clean_run:
        raise PiChildRunError("pi child run reference needs a run id")
    if _REFERENCE_SEPARATOR in clean_run:
        raise PiChildRunError(
            f"pi run id {clean_run!r} contains the reference separator"
        )
    if child_id is None:
        return clean_run
    clean_child = str(child_id).strip()
    if not clean_child or _REFERENCE_SEPARATOR in clean_child:
        raise PiChildRunError(f"pi child id {child_id!r} is not addressable")
    return f"{clean_run}{_REFERENCE_SEPARATOR}{clean_child}"


def workflow_result_run_id(details: dict[str, Any], result: dict[str, Any]) -> str:
    """Join the vendor's workflow summary and result by their explicit child key."""
    key = result.get("workflowKey")
    summary = details.get("workflowChildren")
    children = summary.get("children") if isinstance(summary, dict) else None
    if not isinstance(key, str) or not key or not isinstance(children, list):
        raise PiChildRunError("Pi workflow result lacks its native child linkage")
    matches = [child for child in children if isinstance(child, dict) and child.get("childId") == key]
    if len(matches) != 1 or not isinstance(matches[0].get("runId"), str) or not matches[0]["runId"]:
        raise PiChildRunError("Pi workflow result has no unique native child run")
    return child_reference(matches[0]["runId"])


def split_child_reference(reference: str) -> tuple[str, str | None]:
    """Resolve the adapter's shared control and transcript address."""
    clean = str(reference or "").strip()
    if not clean:
        raise PiChildRunError("pi child-run control reference is empty")
    run_id, separator, child_id = clean.partition(_REFERENCE_SEPARATOR)
    if not run_id.strip():
        raise PiChildRunError(
            f"pi child-run control reference is malformed: {reference!r}"
        )
    if not separator:
        return run_id.strip(), None
    if not child_id.strip():
        raise PiChildRunError(
            f"pi child-run control reference is malformed: {reference!r}"
        )
    child_reference(run_id.strip(), child_id.strip())
    return run_id.strip(), child_id.strip()


def stop_command_line(reference: str) -> str:
    """The command line that stops what this reference names."""

    run_id, child_id = split_child_reference(reference)
    return " ".join([STOP_COMMAND, run_id, *([child_id] if child_id else [])])


def inspect_command_line(reference: str, request_id: str) -> str:
    """The command line that reads what this reference has said so far.

    The package resolves the child by "exactly the node id the host received
    in the status snapshot", which is the same id the reference carries for
    its stop, so one spelling serves both.
    """

    clean_request = str(request_id or "").strip()
    if not clean_request:
        raise PiChildRunError("pi child-run inspect needs a request id")
    run_id, child_id = split_child_reference(reference)
    parts = [INSPECT_COMMAND, clean_request, run_id]
    if child_id:
        parts.append(child_id)
    parts.extend(["--lines", str(_INSPECT_MESSAGE_LINES)])
    return " ".join(parts)


def is_inspect_widget(record: dict[str, Any]) -> bool:
    """Whether one ``extension_ui_request`` is an inspect reply's carrier.

    The package answers on this widget with one line and then retracts it.
    Hosts "must not render this widget", so a caller needs to recognise it
    before deciding what the request was.
    """

    return (
        str(record.get("method") or "").strip() == "setWidget"
        and str(record.get("widgetKey") or "").strip() == INSPECT_WIDGET_KEY
    )


def inspect_reply(record: dict[str, Any]) -> dict[str, Any] | None:
    """The inspect reply one ``extension_ui_request`` carries, if any.

    The retracting update carries no lines and is not a reply.
    """

    if not is_inspect_widget(record):
        return None
    lines = record.get("widgetLines")
    if not isinstance(lines, list):
        return None
    for line in lines:
        if not isinstance(line, str) or not line.startswith(INSPECT_REPLY_PREFIX):
            continue
        try:
            payload = json.loads(line[len(INSPECT_REPLY_PREFIX) :])
        except ValueError as error:
            raise PiChildRunError(f"pi inspect reply is not JSON: {error}") from error
        if not isinstance(payload, dict):
            raise PiChildRunError("pi inspect reply is not an object")
        if str(payload.get("kind") or "") != INSPECT_REPLY_KIND:
            return None
        if payload.get("version") != INSPECT_REPLY_VERSION:
            raise PiChildRunError(
                f"pi inspect reply version {payload.get('version')!r} "
                f"is not {INSPECT_REPLY_VERSION}"
            )
        return payload
    return None


def is_async_status_widget(record: dict[str, Any]) -> bool:
    """Whether one ``extension_ui_request`` is the package's host status widget.

    The package repushes it about once a second for as long as a run lives, so
    a caller needs to know the widget is the sub-agent panel's even on a tick
    that changed nothing.
    """

    return (
        str(record.get("method") or "").strip() == "setWidget"
        and str(record.get("widgetKey") or "").strip() == ASYNC_WIDGET_KEY
    )


def async_status_snapshot(record: dict[str, Any]) -> dict[str, Any] | None:
    """The status snapshot one ``extension_ui_request`` carries, if any.

    Hosts are told to buffer these payloads by marker rather than render them,
    so every widget that is not this one, and every line that does not carry
    the package's marker, is left alone.
    """

    if not is_async_status_widget(record):
        return None
    lines = record.get("widgetLines")
    if not isinstance(lines, list):
        return None
    for line in lines:
        if not isinstance(line, str) or not line.startswith(ASYNC_SNAPSHOT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(ASYNC_SNAPSHOT_PREFIX) :])
        except ValueError as error:
            raise PiChildRunError(
                f"pi async status snapshot is not JSON: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise PiChildRunError("pi async status snapshot is not an object")
        if str(payload.get("kind") or "") != ASYNC_SNAPSHOT_KIND:
            return None
        if payload.get("version") != ASYNC_SNAPSHOT_VERSION:
            raise PiChildRunError(
                f"pi async status snapshot version {payload.get('version')!r} "
                f"is not {ASYNC_SNAPSHOT_VERSION}"
            )
        return payload
    return None


def _snapshot_state(node: dict[str, Any]) -> str:
    state = str(node.get("state") or "").strip()
    if state not in _SNAPSHOT_STATES:
        raise PiChildRunError(f"pi async status snapshot has unknown state {state!r}")
    return state


def _row_status(row: dict[str, Any]) -> tuple[str, bool]:
    """A foreground row's state in the package's words, and whether it settled.

    Every flag read here is one the package declares on ``SingleResult``. A row
    that carries none of them and no exit code has not finished, which is the
    state a running child is in while the tool streams partial progress.
    """

    for flag, name in (
        ("stopped", "stopped"),
        ("interrupted", "interrupted"),
        ("timedOut", "timed-out"),
        ("detached", "detached"),
    ):
        if bool(row.get(flag)):
            return name, True
    exit_code = row.get("exitCode")
    if exit_code is None:
        return "running", False
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise PiChildRunError(f"pi child run has a non-integer exitCode: {exit_code!r}")
    return ("complete" if exit_code == 0 else "failed"), True


def _label(node: dict[str, Any]) -> str | None:
    for key in ("label", "sessionName", "agent", "workflowKey"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return None


def _lifecycle_fact(
    state: _ChildState, *, event: str, engine_event: str, engine_status: str
) -> dict[str, Any]:
    can_stop = event != "closed" and engine_status in _STOPPABLE_STATES
    data: dict[str, Any] = {
        "kind": "lifecycle",
        "engineRef": state.reference,
        "event": event,
        "engineEvent": engine_event,
        "engineStatus": engine_status,
        "operations": ["stop"] if can_stop else [],
    }
    if state.parent_reference:
        data["parentEngineRef"] = state.parent_reference
    if state.label:
        data["description"] = state.label
    if state.task_type:
        data["taskType"] = state.task_type
    if can_stop:
        data["controlRef"] = state.reference
    canonical = canonical_child_run_data(data, engine_kind=PI_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": f"pi-child:lifecycle:{state.reference}:{engine_event}:{event}",
            "data": canonical,
        }
    )


def _message_fact(
    state: _ChildState, *, role: str, text: str, message_ref: str
) -> dict[str, Any]:
    """One thing the child said, in the words the package's reply carried."""

    data: dict[str, Any] = {
        "kind": "message",
        "engineRef": state.reference,
        "role": role,
        "content": [{"type": "text", "text": text}],
        "messageId": message_ref,
    }
    if state.parent_reference:
        data["parentEngineRef"] = state.parent_reference
    canonical = canonical_child_run_data(data, engine_kind=PI_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": f"pi-child:message:{state.reference}:{message_ref}",
            "data": canonical,
        }
    )


def _message_ref(role: str, text: str) -> str:
    """A stable identity for one reply message.

    The reply is a tail window re-read from the package's own artifacts, and
    its rows carry no ids of their own, so the row IS its identity: the same
    words in the same role are the same message on the next read.
    """

    digest = hashlib.sha256(f"{role}\n{text}".encode()).hexdigest()
    return digest[:16]


class PiChildResources:
    """Fold Pi's async status snapshots and tool results into child facts."""

    def __init__(self) -> None:
        self._children: dict[str, _ChildState] = {}
        #: Inspect requests in flight, by the request id the reply will quote.
        self._inspects: dict[str, str] = {}
        self._inspect_sequence = 0

    def contains(self, reference: str) -> bool:
        return str(reference or "").strip() in self._children

    def observe_ui_request(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Child facts owed after one extension UI request.

        This is the only place a background child run opens, changes or
        closes. Returns an empty list for every other widget, so a session
        whose model never delegates costs nothing.
        """

        snapshot = async_status_snapshot(record)
        if snapshot is None:
            return []
        runs = snapshot.get("runs")
        if not isinstance(runs, list):
            return []
        frames: list[dict[str, Any]] = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("id") or "").strip()
            if not run_id:
                raise PiChildRunError("pi async status snapshot run carries no id")
            frames.extend(
                self._fold_node(run, run_id=run_id, child_id=None, parent=None)
            )
        return frames

    def _fold_node(
        self,
        node: dict[str, Any],
        *,
        run_id: str,
        child_id: str | None,
        parent: str | None,
    ) -> list[dict[str, Any]]:
        """Fold one snapshot node and everything below it.

        Descendants are addressed by their own node id under the same run,
        which is what the package takes back: a child id is "exactly the node
        id the host received in the status snapshot".

        The run is the row. It is the identity the package hands out at
        launch and keeps through every state — a queued run has no steps yet,
        and they appear once it starts — so the run is what a reader follows
        from beginning to end. One delegation makes one step, and the package
        states that stopping such a run and stopping its child are the same
        act, so the step shares that row and its stop handle. Inspection still
        addresses the step's session file rather than the run summary.
        Steps are shown only when a launch fanned out into several,
        nested under the run that holds them, because then each is an agent
        of its own with its own stop.
        """

        reference = child_reference(run_id, child_id)
        state = _snapshot_state(node)
        frames = self._publish(
            reference,
            parent=parent,
            label=_label(node),
            status=state,
            settled=state not in _OPEN_STATES,
            engine_event="setWidget",
            task_type=str(node.get("kind") or "").strip() or None,
        )
        child_state = self._children[reference]
        activity = json.dumps(
            {"updatedAt": node.get("updatedAt"), "activity": node.get("activity")},
            sort_keys=True,
            separators=(",", ":"),
        )
        if not child_state.closed and activity != child_state.inspect_activity:
            # Work can produce messages without leaving `running`. The native
            # activity watermark owes a read even when no lifecycle fact changed.
            child_state.inspect_pending = True
        child_state.inspect_activity = activity
        children = node.get("children")
        child_nodes = (
            [child for child in children if isinstance(child, dict)]
            if isinstance(children, list)
            else []
        )
        inspect_reference = reference
        if len(child_nodes) == 1:
            node_id = str(child_nodes[0].get("id") or "").strip()
            if not node_id:
                raise PiChildRunError("pi async status snapshot child carries no id")
            inspect_reference = child_reference(run_id, node_id)
        if not child_state.closed and inspect_reference != child_state.inspect_reference:
            child_state.inspect_pending = True
        child_state.inspect_reference = inspect_reference
        if len(child_nodes) < 2:
            return frames
        for child in child_nodes:
            node_id = str(child.get("id") or "").strip()
            if not node_id:
                raise PiChildRunError("pi async status snapshot child carries no id")
            frames.extend(
                self._fold_node(
                    child, run_id=run_id, child_id=node_id, parent=reference
                )
            )
        return frames

    def inspect_requests(self) -> list[tuple[str, str]]:
        """The reads owed as ``(request_id, native_inspect_reference)`` pairs.

        One per child the snapshot changed since the last drain. The request
        id is what the package quotes back, and it is minted here so the reply
        can be matched to the child it was asked about; the package tells
        hosts to "drop unmatched replies", and a reply is only matchable by an
        id this projector issued.
        """

        owed: list[tuple[str, str]] = []
        for state in self._children.values():
            if not state.inspect_pending:
                continue
            state.inspect_pending = False
            self._inspect_sequence += 1
            request_id = f"astrabox-{self._inspect_sequence:x}"
            self._inspects[request_id] = state.reference
            owed.append((request_id, state.inspect_reference or state.reference))
        return owed

    def observe_persisted_inspect_reply(
        self, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Fold an inspect reply the platform persisted, matched by the run it names.

        A reply read back from the journal was asked for by this platform —
        that is why it was persisted — but the request id it quotes is one
        this projector never issued. The reply itself names the run and child
        it describes, and that is what it is matched on here.
        """

        payload = inspect_reply(record)
        if payload is None:
            return []
        run_id = str(payload.get("asyncId") or "").strip()
        if not run_id:
            return []
        child_id = str(payload.get("childId") or "").strip() or None
        reference = child_reference(run_id, child_id)
        if reference not in self._children:
            owner = next(
                (
                    state
                    for state in self._children.values()
                    if state.inspect_reference == reference
                ),
                None,
            )
            if owner is None:
                return []
            reference = owner.reference
        request_id = str(payload.get("requestId") or "").strip() or f"persisted:{reference}"
        self._inspects[request_id] = reference
        return self.observe_inspect_reply(record)

    def observe_inspect_reply(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Child facts owed after one inspect reply.

        The reply's ``messages`` are the child's own transcript window, and
        ``finalOutput`` is what it handed back. Both are re-read from the
        package's artifacts on every request, so what was already published
        comes again and is not published twice. Tool rows are the child's
        working detail and stay out, which is the line the codex projection
        draws too: a reader of the parent conversation is owed what the child
        said.
        """

        payload = inspect_reply(record)
        if payload is None:
            return []
        request_id = str(payload.get("requestId") or "").strip()
        reference = self._inspects.pop(request_id, None)
        if reference is None:
            return []
        state = self._children.get(reference)
        if state is None:
            return []
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            if code in _INSPECT_ERRORS_DROPPED:
                return []
            if code == "internal":
                # The supplier reports a failed artifact read on this request's
                # widget, not a stopped engine. Retain that diagnostic while
                # its independent status pushes and parent output keep flowing.
                return [raw_event_frame("subagent-inspect", payload)]
            raise PiChildRunError(
                f"pi inspect of {reference!r} failed: {code or 'unknown'}: "
                f"{error.get('message') or ''}"
            )

        frames: list[dict[str, Any]] = []
        rows = payload.get("messages")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("kind") or "") != "text":
                    continue
                text = row.get("text")
                role = str(row.get("role") or "").strip()
                # A tool result reaches the reply as a text row too, under
                # pi's own `toolResult` role. It is the child's working detail,
                # not what the child said, and the platform carries only the
                # two roles a conversation has — the same line codex draws.
                if role not in _CHILD_MESSAGE_ROLES:
                    continue
                if not isinstance(text, str) or not text:
                    continue
                message_ref = _message_ref(role, text)
                if message_ref in state.reported_messages:
                    continue
                state.reported_messages.add(message_ref)
                frames.append(
                    _message_fact(state, role=role, text=text, message_ref=message_ref)
                )
        final_output = payload.get("finalOutput")
        if (
            isinstance(final_output, str)
            and final_output
            and not state.final_output_reported
        ):
            state.final_output_reported = True
            frames.append(
                _message_fact(
                    state,
                    role="assistant",
                    text=final_output,
                    message_ref=f"final:{_message_ref('assistant', final_output)}",
                )
            )
        status = str(payload.get("status") or "").strip()
        inspected_reference = child_reference(
            str(payload.get("asyncId") or ""),
            str(payload.get("childId") or "").strip() or None,
        )
        if status and inspected_reference == reference:
            if status not in _SNAPSHOT_STATES:
                raise PiChildRunError(
                    f"pi inspect reply has unknown status {status!r}"
                )
            # The snapshot names the child; the reply's `label` is measured
            # to carry the run id instead, so it is not allowed to rename it.
            frames.extend(
                self._publish(
                    reference,
                    parent=state.parent_reference,
                    label=None,
                    status=status,
                    settled=status not in _OPEN_STATES,
                    engine_event="inspect",
                    mark_inspect=False,
                )
            )
        return frames

    def observe_tool_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Child facts owed after one foreground sub-agent tool call.

        A launch that left work running carries the package's ``asyncId``, and
        its children belong to the status snapshot instead: reading them here
        as well would close them the instant the call answered, which is the
        moment a background launch answers.
        """

        if str(event.get("toolName") or "").strip() != SUBAGENT_TOOL_NAME:
            return []
        engine_event = str(event.get("type") or "").strip()
        if engine_event == "tool_execution_update":
            payload = event.get("partialResult")
        elif engine_event == "tool_execution_end":
            payload = event.get("result")
        else:
            return []
        if not isinstance(payload, dict):
            return []
        details = payload.get("details")
        if not isinstance(details, dict):
            return []
        if details.get("asyncId") is not None:
            return []
        run_id = str(details.get("runId") or "").strip()
        if not run_id:
            return []
        rows = details.get("results")
        if not isinstance(rows, list):
            return []
        frames: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise PiChildRunError(
                    f"pi child run result carries no integer index: {row.get('index')!r}"
                )
            status, settled = _row_status(row)
            reference = child_reference(run_id, step_child_id(index))
            if details.get("mode") == "workflow":
                worker_run = workflow_result_run_id(details, row)
                siblings = [result for result in rows if isinstance(result, dict)
                            and result.get("workflowKey") == row.get("workflowKey")]
                reference = child_reference(worker_run, step_child_id(index) if len(siblings) > 1 else None)
            frames.extend(
                self._publish(
                    reference,
                    # A blocking launch reports only its rows, never the run
                    # they belong to, so each row is its own root here.
                    parent=None,
                    label=_label(row),
                    status=status,
                    # A foreground launch blocks the parent, so the call ending
                    # IS its children ending, whatever the last row said.
                    settled=settled or engine_event == "tool_execution_end",
                    engine_event=engine_event,
                )
            )
        return frames

    def _publish(
        self,
        reference: str,
        *,
        parent: str | None,
        label: str | None,
        status: str,
        settled: bool,
        engine_event: str,
        task_type: str | None = None,
        mark_inspect: bool = True,
    ) -> list[dict[str, Any]]:
        state = self._children.get(reference)
        if state is None:
            state = _ChildState(reference=reference, parent_reference=parent)
            self._children[reference] = state
        if state.closed:
            return []
        if label:
            state.label = label
        if task_type:
            state.task_type = task_type

        frames: list[dict[str, Any]] = []
        if not state.opened:
            state.opened = True
            state.status = status
            frames.append(
                _lifecycle_fact(
                    state,
                    event="opened",
                    engine_event=engine_event,
                    engine_status=status,
                )
            )
        elif status != state.status:
            state.status = status
            if not settled:
                frames.append(
                    _lifecycle_fact(
                        state,
                        event="updated",
                        engine_event=engine_event,
                        engine_status=status,
                    )
                )
        if settled:
            state.closed = True
            frames.append(
                _lifecycle_fact(
                    state,
                    event="closed",
                    engine_event=engine_event,
                    engine_status=status,
                )
            )
        if frames and mark_inspect:
            # Something changed about this child, so what it has said may have
            # too. A closed child is read once more: that read is where its
            # final output comes from.
            state.inspect_pending = True
        return frames
