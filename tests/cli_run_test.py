"""``astrabox run`` — what ends a turn, and what a closed stream actually means.

The command reads AI SDK frames for their text and takes no position on what
any other frame type means: a segment boundary looks like a turn boundary from
a client, and the stream's own terminal sentinel is the signal that does not.
A stream can also close because the Agent stopped to ask something, which is
not the same as finishing.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from astrabox.cli import run
from astrabox.cli.client import ApiClient, Endpoint
from astrabox.cli.output import EXIT_UNREACHABLE, CliError

_SESSION = "session-1"


@pytest.fixture(autouse=True)
def _isolated_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ASTRABOX_ENDPOINT",
        "ASTRABOX_SERVER_HOST_PORT",
        "ASTRABOX_TOKEN",
        "ASTRABOX_CLIENT_ID",
        "ASTRABOX_CLIENT_SECRET",
        "ASTRABOX_TOKEN_URL",
        "ASTRABOX_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)


def _sse(*frames: str) -> bytes:
    return "".join(f"{frame}\n\n" for frame in frames).encode("utf-8")


def _client(stream_body: bytes, *, session: dict[str, Any] | None = None) -> ApiClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ai-stream"):
            return httpx.Response(200, content=stream_body)
        if request.url.path == f"/api/v1/sessions/{_SESSION}":
            session_data = {"state": "READY", **(session or {})}
            return httpx.Response(
                200, json={"code": "OK", "message": "ok", "data": session_data}
            )
        if request.url.path == "/api/v1/agents":
            return httpx.Response(
                200,
                json={
                    "code": "OK",
                    "message": "ok",
                    "data": [{"agent_id": "agent-1", "name": "researcher"}],
                },
            )
        if request.url.path.endswith("/conversations"):
            return httpx.Response(
                200, json={"code": "OK", "message": "ok", "data": {"session_id": _SESSION}}
            )
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "x", "data": None})

    return ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )


def test_text_deltas_are_joined_in_order() -> None:
    body = _sse(
        'data: {"type":"text-start","id":"1"}',
        'data: {"type":"text-delta","id":"1","delta":"Hello, "}',
        'data: {"type":"text-delta","id":"1","delta":"world"}',
        "data: [DONE]",
    )
    with _client(body) as client:
        result = run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert result["text"] == "Hello, world"


def test_the_terminal_sentinel_ends_the_read_not_a_finish_frame() -> None:
    """A per-segment finish frame is not a turn boundary. Treating one as the
    end would drop everything the engine sends after a pause."""
    body = _sse(
        'data: {"type":"text-delta","id":"1","delta":"first"}',
        'data: {"type":"finish"}',
        'data: {"type":"text-delta","id":"2","delta":" second"}',
        "data: [DONE]",
    )
    with _client(body) as client:
        result = run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert result["text"] == "first second"


def test_frames_this_client_cannot_read_are_skipped_not_fatal() -> None:
    """The stream's vocabulary belongs to the engine seam; a frame type this
    CLI has never heard of must not fail somebody's run."""
    body = _sse(
        ": keepalive",
        'data: {"type":"tool-input-start","toolName":"Bash"}',
        "data: not json at all",
        'data: {"type":"text-delta","id":"1","delta":"done"}',
        "data: [DONE]",
    )
    with _client(body) as client:
        result = run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert result["text"] == "done"


def test_an_error_frame_is_collected(capsys) -> None:
    body = _sse(
        'data: {"type":"error","errorText":"model refused"}',
        "data: [DONE]",
    )
    with _client(body) as client:
        result = run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert result["errors"] == ["model refused"]


def test_a_run_that_errored_exits_non_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    body = _sse('data: {"type":"error","errorText":"model refused"}', "data: [DONE]")
    monkeypatch.setattr("astrabox.cli.run.ApiClient", lambda *_a, **_k: _client(body))

    assert run._cmd_run(_args()) != 0

    assert json.loads(capsys.readouterr().out)["errors"] == ["model refused"]


def test_a_closed_stream_with_a_pending_question_is_reported_as_pending(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The engine pausing to ask is not the work finishing, and a caller told
    'done' would wait forever for an answer it should have supplied."""
    body = _sse('data: {"type":"text-delta","id":"1","delta":"May I?"}', "data: [DONE]")
    pending = {"interaction_id": "int-1", "kind": "permission"}
    monkeypatch.setattr(
        "astrabox.cli.run.ApiClient",
        lambda *_a, **_k: _client(body, session={"pending_interaction": pending}),
    )

    assert run._cmd_run(_args()) == 0

    assert json.loads(capsys.readouterr().out)["pending_interaction"] == pending


def test_a_refused_turn_names_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "FORBIDDEN", "message": "no", "data": None})

    client = ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )
    with client:
        with pytest.raises(CliError) as caught:
            run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert "403" in caught.value.message


def test_the_deadline_fails_loud_rather_than_returning_half_an_answer() -> None:
    """Returning what arrived so far, as if it were the whole reply, is the
    failure this prevents."""
    body = _sse('data: {"type":"text-delta","id":"1","delta":"partial"}', "data: [DONE]")
    with _client(body) as client:
        with pytest.raises(CliError) as caught:
            run.stream_turn(client, _SESSION, "hi", deadline_seconds=0, live=False)

    assert caught.value.exit_code == EXIT_UNREACHABLE


def test_an_agent_name_is_resolved_to_its_id_before_starting() -> None:
    with _client(b"") as client:
        assert run.start_conversation(client, "researcher") == _SESSION


def _args(**overrides: Any) -> Any:
    from types import SimpleNamespace

    args = {
        "agent": "researcher",
        "task": "do the thing",
        "session": None,
        "timeout": 30,
        "output": "json",
        "endpoint": "http://deployment.test",
        "token": None,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


# ── waiting for the sandbox ───────────────────────────────────────────────


def _states_client(states: list[str]) -> ApiClient:
    """A deployment that reports each state in turn, then repeats the last."""
    remaining = list(states)

    def handle(request: httpx.Request) -> httpx.Response:
        state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(
            200, json={"code": "OK", "message": "ok", "data": {"state": state}}
        )

    return ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )


def test_the_turn_waits_for_the_sandbox_instead_of_racing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation exists before its sandbox does, and a turn sent into that
    window is refused with SESSION_BUSY. The wait is a precondition read from
    the state the deployment publishes, not a retry around the failure."""
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    with _states_client(["CREATING", "CREATING", "READY"]) as client:
        run.wait_until_ready(client, _SESSION, deadline=run.time.monotonic() + 30)


def test_a_session_that_died_is_not_waited_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """TERMINATED is not a state a session leaves, so polling it to the deadline
    would spend the caller's whole timeout learning nothing."""
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    with _states_client(["CREATING", "TERMINATED"]) as client:
        with pytest.raises(CliError) as caught:
            run.wait_until_ready(client, _SESSION, deadline=run.time.monotonic() + 30)

    assert caught.value.details["state"] == "TERMINATED"


def test_a_sandbox_that_never_arrives_fails_loud_with_the_last_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)

    with _states_client(["CREATING"]) as client:
        with pytest.raises(CliError) as caught:
            run.wait_until_ready(client, _SESSION, deadline=run.time.monotonic() + 0.05)

    assert caught.value.exit_code == EXIT_UNREACHABLE
    assert caught.value.details["state"] in ("CREATING", "")


def test_the_turn_carries_a_client_message_id() -> None:
    """The deployment derives this turn's command and input ids from it and
    refuses a turn without one. It is also the idempotency key, so a fresh
    value per invocation is what makes `run` a new turn rather than a replay."""
    sent: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=_sse("data: [DONE]"))

    client = ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )
    with client:
        run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert sent["content"] == "hi"
    import uuid as _uuid

    _uuid.UUID(sent["client_message_id"])  # raises if it is not a UUID


def test_two_runs_do_not_share_an_idempotency_key() -> None:
    """Reusing one id across invocations would make the second turn a replay of
    the first, and the deployment would answer with the first turn's outcome."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["client_message_id"])
        return httpx.Response(200, content=_sse("data: [DONE]"))

    for _ in range(2):
        client = ApiClient(
            Endpoint(base_url="http://deployment.test", token=None),
            transport=httpx.MockTransport(handle),
        )
        with client:
            run.stream_turn(client, _SESSION, "hi", deadline_seconds=30, live=False)

    assert len(set(seen)) == 2, f"the same key was reused: {seen}"
