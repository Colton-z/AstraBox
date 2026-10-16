"""``astrabox run`` — give an Agent a task and watch it answer.

The task is sent as one message (``POST …/inputs``), and the answer is read off
the session's stream (``GET …/stream?after_seq=N``), the same two calls the
console makes. The platform holds the message until the conversation's
runtime takes it, so a conversation that is still starting, or still answering
an earlier message, needs no waiting first.

The stream carries every response in the conversation, one message per HTTP
response. The answer to this task is the response whose ``start`` part lists
the message's ``input_id``; text from any other response is not this task's
answer and is skipped. The command ends when that response's message
finishes: either the response ended, or it paused on a question
(``data-interaction``), which is reported as pending. A message the platform
could not hand over arrives as ``data-input-failed`` and fails the run.
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
#: comment while a session is quiet, so a gap longer than this means the
#: transport is gone rather than the Agent thinking.
_STREAM_READ_TIMEOUT_SECONDS = 120.0

#: The AI SDK stream's terminal sentinel (``astrabox/api/sse.py``).
_DONE_SENTINEL = "[DONE]"


def register(subparsers: Any) -> None:
    """Add ``astrabox run`` to the top-level parser."""
    run = subparsers.add_parser(
        "run",
        parents=[connection_flags()],
        help="Send a task to an Agent and print the answer.",
        description=(
            "Start a conversation with the named Agent (or continue one with "
            "--session), send the task, and stream the reply until it ends or "
            "asks a question."
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
        result = send_task(client, session_id, args.task, deadline=deadline, live=live)

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


def send_task(
    client: ApiClient,
    session_id: str,
    task: str,
    *,
    deadline: float,
    live: bool,
) -> dict[str, Any]:
    """Send one message and read the session's stream until its answer ends or pauses.

    Text is printed as it arrives in the table format, and accumulated for the
    JSON one, where writing it to stdout while building a JSON object would
    corrupt the object a caller is parsing.
    """
    # Where the conversation's output stands before the message exists: its
    # answer can only come after this.
    position = int((client.get(f"/api/v1/sessions/{session_id}/messages", params={"limit": 1}) or {})[
        "output_seq"
    ])
    receipt = client.post(
        f"/api/v1/sessions/{session_id}/inputs",
        # A fresh id per invocation is what `run` means: the same id again
        # would be the same message, not a second one.
        json={"content": task, "client_message_id": str(uuid.uuid4())},
    )
    reading = _Answer(str((receipt or {})["input_id"]), live=live)
    while not reading.over:
        position = _read_stream(client, session_id, position, reading, deadline=deadline)
    if live and reading.chunks:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return {
        "text": "".join(reading.chunks),
        "parts": reading.parts,
        "errors": reading.errors,
        "pending_interaction": reading.pending,
    }


class _Answer:
    """What the stream has shown of the response to one input."""

    def __init__(self, input_id: str, *, live: bool) -> None:
        self.input_id = input_id
        self.live = live
        self.response_id: str | None = None
        self.reading = False
        self.over = False
        self.parts = 0
        self.chunks: list[str] = []
        self.errors: list[str] = []
        self.pending: dict[str, Any] | None = None

    def take(self, part: dict[str, Any]) -> None:
        kind = part.get("type")
        if kind == "start":
            metadata = part.get("messageMetadata")
            if isinstance(metadata, dict) and self.input_id in (metadata.get("input_ids") or []):
                self.response_id = str(part.get("messageId") or "")
            # A response's message reopens with a bare start after a question.
            self.reading = self.response_id is not None and part.get("messageId") == self.response_id
            return
        if kind == "data-input-failed":
            data = part.get("data") if isinstance(part.get("data"), dict) else {}
            if data.get("input_id") == self.input_id:
                self.errors.append(str(data.get("message") or data.get("code") or "message refused"))
                self.over = True
            return
        if not self.reading:
            return
        self.parts += 1
        if kind == "text-delta":
            text = str(part.get("delta") or "")
            self.chunks.append(text)
            if self.live and text:
                sys.stdout.write(text)
                sys.stdout.flush()
        elif kind == "error":
            self.errors.append(str(part.get("errorText") or "error"))
        elif kind == "data-interaction" and isinstance(part.get("data"), dict):
            self.pending = dict(part["data"])
        elif kind == "finish":
            self.reading = False
            self.over = True


def _read_stream(
    client: ApiClient, session_id: str, position: int, answer: _Answer, *, deadline: float
) -> int:
    """Read one message's worth of the session's stream; where to read from next."""
    import httpx

    try:
        with client.stream(
            "GET",
            f"/api/v1/sessions/{session_id}/stream?after_seq={position}",
            read_timeout=_STREAM_READ_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise CliError(
                    f"the deployment refused the session stream with HTTP {response.status_code}",
                    exit_code=EXIT_FAILED,
                    details={"session_id": session_id, "body": response.text[:300]},
                )
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise CliError(
                        "the answer did not finish in time",
                        exit_code=EXIT_UNREACHABLE,
                        details={"session_id": session_id, "parts": answer.parts},
                    )
                part = _decode(line)
                if part is _DONE_SENTINEL:
                    break
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "data-resume-cursor":
                    position = int((part.get("data") or {}).get("seq") or position)
                    continue
                answer.take(part)
                if answer.over:
                    break
    except httpx.RequestError as exc:
        raise CliError(
            f"the session stream failed: {exc}",
            exit_code=EXIT_UNREACHABLE,
            details={"session_id": session_id},
        ) from exc
    if time.monotonic() > deadline and not answer.over:
        raise CliError(
            "the answer did not finish in time",
            exit_code=EXIT_UNREACHABLE,
            details={"session_id": session_id, "parts": answer.parts},
        )
    return position


def _decode(line: str) -> Any:
    """One SSE line as a part, the terminal sentinel, or ``None`` to skip.

    Keepalive comments and the blank lines between records carry nothing, and
    a part this CLI cannot decode is skipped rather than failing the run.
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
    "send_task",
    "start_conversation",
]
