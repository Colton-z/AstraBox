"""``astrabox run`` — give an Agent a task and watch it answer.

The turn is sent to the session's Data Stream Protocol endpoint and the reply
is read off that stream. The stream's own terminal sentinel is what ends the
command: the CLI reads AI SDK frames for their text, and takes no position on
what any other frame type means. Deciding a turn had ended by interpreting a
frame would put engine vocabulary in a client that has no business holding it —
and a segment boundary looks very much like a turn boundary from here.

A stream can also end because the engine is waiting for an answer rather than
because the work is done. The session is therefore re-read once the stream
closes, and a pending interaction is reported as exactly that.

A freshly started conversation is not ready to receive one: its sandbox is
still being created, and the deployment answers a turn sent into that window
with ``SESSION_BUSY``. The command waits for the session to reach ``READY``
first, which is the same predicate the live suite polls
(``tests/e2e/_sandbox_helpers.py``).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any

from astrabox.cli.client import ApiClient, resolve_endpoint
from astrabox.cli.flags import connection_flags
from astrabox.cli.output import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    FORMAT_TABLE,
    CliError,
    emit,
)
from astrabox.cli.resources import match_agent

#: Whole-run deadline. An Agent doing real work runs for minutes, so this is
#: generous; `--timeout` moves it and the command fails loud when it expires
#: rather than returning a partial answer as if it were the whole one.
DEFAULT_TIMEOUT_SECONDS = 900

#: Read deadline for one stream chunk. The server sends an SSE keepalive
#: comment while a turn is quiet, so a gap longer than this means the transport
#: is gone rather than the Agent thinking.
_STREAM_READ_TIMEOUT_SECONDS = 120.0

#: The AI SDK stream's terminal sentinel (``astrabox/api/sse.py``).
_DONE_SENTINEL = "[DONE]"

#: The session state that can accept a turn
#: (``astrabox/core/model/astrabox_models.py``).
_READY = "READY"

#: States a session never leaves, so waiting on one is waiting forever.
_TERMINAL_STATES = frozenset({"TERMINATED", "DELETED", "RECOVERY_REQUIRED"})

#: Gap between session-state reads while the sandbox is being created.
_READY_POLL_SECONDS = 1.0


def register(subparsers: Any) -> None:
    """Add ``astrabox run`` to the top-level parser."""
    run = subparsers.add_parser(
        "run",
        parents=[connection_flags()],
        help="Send a task to an Agent and print the answer.",
        description=(
            "Start a conversation with the named Agent (or continue one with "
            "--session), send the task, and stream the reply until the "
            "session's stream closes."
        ),
    )
    run.add_argument("agent", help="Agent name, or agent id.")
    run.add_argument("task", help="What to ask the Agent to do.")
    run.add_argument(
        "--session",
        help="Continue this session instead of starting a conversation.",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Whole-run deadline in seconds (default: %(default)s).",
    )
    run.set_defaults(func=_cmd_run)


def _cmd_run(args: Any) -> int:
    """Handle ``astrabox run``."""
    endpoint = resolve_endpoint(endpoint=args.endpoint, token=args.token)
    live = args.output == FORMAT_TABLE

    with ApiClient(endpoint) as client:
        deadline = time.monotonic() + max(args.timeout, 1)
        session_id = args.session or start_conversation(client, args.agent)
        wait_until_ready(client, session_id, deadline=deadline)
        result = stream_turn(
            client,
            session_id,
            args.task,
            deadline_seconds=max(int(deadline - time.monotonic()), 1),
            live=live,
        )
        result["pending_interaction"] = _pending_interaction(client, session_id)

    result["session_id"] = session_id
    if live:
        # The text was printed as it arrived; repeating it in the result would
        # double every answer.
        result.pop("text", None)
    emit(result, output=args.output, note=_note(result) if live else None)
    return EXIT_FAILED if result.get("errors") else EXIT_OK


def start_conversation(client: ApiClient, agent: str) -> str:
    """Resolve the Agent and open a conversation on it."""
    found = match_agent(client, agent)
    agent_id = str(found.get("agent_id") or "") if found else agent
    started = client.post(f"/api/v1/agents/{agent_id}/conversations")
    session_id = str((started or {}).get("session_id") or "")
    if not session_id:
        raise CliError(
            f"the deployment started no session for agent {agent!r}",
            details={"agent_id": agent_id},
        )
    return session_id


def wait_until_ready(client: ApiClient, session_id: str, *, deadline: float) -> None:
    """Wait for a session's sandbox before sending it a turn.

    A conversation is created before its sandbox exists. Sending the turn into
    that window is refused with ``SESSION_BUSY`` — "session is still creating,
    please retry" — so this wait is a precondition, not a retry wrapped around a
    failure: the deployment publishes the state, and this reads it.

    Shares the run's deadline rather than holding one of its own, so
    ``--timeout`` bounds the whole command rather than each half of it.
    """
    seen = ""
    while time.monotonic() < deadline:
        session = client.get(f"/api/v1/sessions/{session_id}")
        seen = str((session or {}).get("state") or "")
        if seen == _READY:
            return
        if seen in _TERMINAL_STATES:
            raise CliError(
                f"session {session_id} reached {seen} before it could take a turn",
                exit_code=EXIT_FAILED,
                details={"session_id": session_id, "state": seen},
            )
        time.sleep(_READY_POLL_SECONDS)
    raise CliError(
        f"session {session_id} was still {seen or 'unreported'} when time ran out",
        exit_code=EXIT_UNREACHABLE,
        details={"session_id": session_id, "state": seen},
    )


def stream_turn(
    client: ApiClient,
    session_id: str,
    task: str,
    *,
    deadline_seconds: int,
    live: bool,
) -> dict[str, Any]:
    """Send one turn and read the session's stream until it closes.

    Text is printed as it arrives in the table format, and accumulated for the
    JSON one — where writing it to stdout while building a JSON object would
    corrupt the object a caller is parsing.
    """
    import httpx

    # The deployment derives this turn's command id and input id from this
    # value (`{session_id}:{client_message_id}`), so it is required and it is
    # the idempotency key: re-sending a turn under the same id is the same
    # turn, not a second one. A fresh id per invocation is what `run` means.
    client_message_id = str(uuid.uuid4())
    chunks: list[str] = []
    errors: list[str] = []
    frames = 0
    deadline = time.monotonic() + max(deadline_seconds, 0)

    try:
        with client.stream(
            "POST",
            f"/api/v1/sessions/{session_id}/ai-stream",
            json={"content": task, "client_message_id": client_message_id},
            read_timeout=_STREAM_READ_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise CliError(
                    f"the deployment refused the turn with HTTP {response.status_code}",
                    exit_code=EXIT_FAILED,
                    details={"session_id": session_id, "body": response.text[:300]},
                )
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise CliError(
                        f"turn did not finish within {deadline_seconds}s",
                        exit_code=EXIT_UNREACHABLE,
                        details={"session_id": session_id, "frames": frames},
                    )
                frame = _decode(line)
                if frame is _DONE_SENTINEL:
                    break
                if frame is None:
                    continue
                frames += 1
                text = _text_of(frame)
                if text:
                    chunks.append(text)
                    if live:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                error = _error_of(frame)
                if error:
                    errors.append(error)
    except httpx.RequestError as exc:
        raise CliError(
            f"the session stream failed: {exc}",
            exit_code=EXIT_UNREACHABLE,
            details={"session_id": session_id},
        ) from exc

    if live and chunks:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return {"text": "".join(chunks), "frames": frames, "errors": errors}


def _decode(line: str) -> Any:
    """One SSE line as a frame, the terminal sentinel, or ``None`` to skip.

    Keepalive comments and the blank lines between records carry nothing, and
    a frame this CLI cannot decode is skipped rather than failing the run — the
    stream's vocabulary belongs to the engine seam, not here.
    """
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    body = line[len("data:") :].strip()
    if body == _DONE_SENTINEL:
        return _DONE_SENTINEL
    try:
        return json.loads(body)
    except ValueError:
        return None


def _text_of(frame: Any) -> str:
    """The assistant text an AI SDK ``text-delta`` frame carries."""
    if not isinstance(frame, dict) or frame.get("type") != "text-delta":
        return ""
    return str(frame.get("delta") or "")


def _error_of(frame: Any) -> str:
    """The message an error frame carries, if this frame is one."""
    if not isinstance(frame, dict) or frame.get("type") != "error":
        return ""
    return str(frame.get("errorText") or frame.get("message") or "error")


def _pending_interaction(client: ApiClient, session_id: str) -> dict[str, Any] | None:
    """What the session is waiting for, if the stream closed on a question.

    A closed stream does not by itself mean the work finished: the engine may
    be waiting for an answer. Reading it from the session is the difference
    between reporting a completed turn and a paused one.
    """
    session = client.get(f"/api/v1/sessions/{session_id}")
    pending = (session or {}).get("pending_interaction")
    return pending if isinstance(pending, dict) else None


def _note(result: dict[str, Any]) -> str:
    """The one-line human summary printed to stderr after a run."""
    lines = [f"session {result['session_id']}"]
    if result.get("pending_interaction"):
        lines.append("the Agent is waiting for an answer: reply in the console or with the API")
    for error in result.get("errors", []):
        lines.append(f"error: {error}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "register",
    "start_conversation",
    "stream_turn",
    "wait_until_ready",
]
