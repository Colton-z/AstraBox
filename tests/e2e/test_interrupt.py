"""Interrupt e2e — interrupting a turn settles it, and the session stays usable.

This is the end-to-end proof that the ``POST /api/v1/sessions/{id}/interrupt``
control path settles an in-flight turn without wedging the session, across the two
ways a turn can be in-flight:

* **streaming turn** — a real command writes its started marker and waits on a
  fixture-controlled release file. The test interrupts before releasing it, so a
  natural finish cannot substitute for cancellation. The turn must settle back
  to ``READY`` without recording ``last_turn_status=FAILED`` or a turn error.
* **held tool permission** — a turn paused on a native tool-permission interaction
  is interrupted. The interrupt denies the held tool (so the uniquely-named file is
  never written — a denied tool has no side effect), clears the pending interaction,
  and settles the turn back to ``READY``.

In both cases the load-bearing proof that the session survived is that a second,
short turn immediately streams a text-delta and settles ``READY`` again — the
session is fully sendable after the interrupt.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_interrupt.py -m e2e -s
"""

from __future__ import annotations

import json
import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    SETTLE_TIMEOUT_S,
    TERMINAL_STATES,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    data,
    file_names,
    get_session,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    stream_turn,
    tool_name,
    wait_for_pending_interaction,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e

# Surfaced ``session.state`` values that mean a turn is in-flight (streaming,
# interrupting, or paused waiting on an interaction) rather than settled at READY.
_IN_FLIGHT_STATES = {
    "PROCESSING",
    "STREAMING",
    "BUSY",
    "INTERRUPTING",
    "WAITING_INPUT",
    "BACKGROUND_RUNNING",
}


def _assert_sendable(e2e_client: httpx.Client, sid: str) -> None:
    """Prove the session is usable again: a short deterministic turn streams ``2``.

    Fails loud if the follow-up turn errors, streams no text, omits the pinned answer,
    or does not settle back to READY.
    """
    reply = stream_turn(
        e2e_client,
        sid,
        content="Reply with only the number 2 and nothing else. Do not use any tool.",
    )
    assert reply.error is None, f"follow-up turn surfaced an error frame: {reply.error}"
    assert reply.n_text_delta > 0, "follow-up turn streamed no text-delta (session not sendable)"
    assert "2" in reply.text, f"follow-up reply did not contain '2': {reply.text!r}"
    final = wait_until_settled(e2e_client, sid)
    assert str(final.get("state")) == "READY", (
        f"session not READY after follow-up turn: {final.get('state')!r}"
    )


def _post_turn_until_streaming(
    e2e_client: httpx.Client, sid: str, content: str, *, read_budget_s: float = 30.0
) -> tuple[bool, bool]:
    """POST a long streaming turn and read its SSE only until it is observably streaming.

    Reads until a model text/reasoning or tool-input frame arrives, or a hard
    wall-clock ``read_budget_s`` elapses, then closes the stream. ``start`` and
    ``data-turn-accepted`` are platform control frames, not proof that the engine
    started producing output. The bound is load-bearing: the server keeps the
    stream open with 15s keepalive comments, so an unbounded read would never
    return. The turn keeps running server-side after the stream is closed; the
    caller interrupts it via the control endpoint.

    Returns ``(saw_ui_header, saw_output_frame)``. Fails loud if the endpoint does not
    return an event stream.
    """
    start = time.monotonic()
    saw_ui = False
    saw_output_frame = False
    output_frame_types = {
        "reasoning-start",
        "reasoning-delta",
        "text-start",
        "text-delta",
        "tool-input-start",
        "tool-input-delta",
        "tool-input-available",
    }
    with e2e_client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/ai-stream",
        json={"content": content, "client_message_id": str(uuid.uuid4())},
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(read_budget_s + 15.0, connect=30.0),
    ) as resp:
        content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in content_type, (
            f"ai-stream did not return SSE (content-type={content_type!r})"
        )
        saw_ui = resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"
        for raw in resp.iter_lines():
            if time.monotonic() - start > read_budget_s:
                break
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") in output_frame_types:
                saw_output_frame = True
                break
    return saw_ui, saw_output_frame


def test_interrupt_streaming_turn_settles_and_session_stays_usable(e2e_client: httpx.Client) -> None:
    """Interrupt an executing command, without a failure record, then run another turn."""
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        marker = f"interrupt_{uuid.uuid4().hex}"
        started_path = workspace_path(e2e_client, sid, f"{marker}.started")
        release_path = workspace_path(e2e_client, sid, f"{marker}.release")
        exited_path = workspace_path(e2e_client, sid, f"{marker}.exited")
        on_exit = f"printf exited > {shlex.quote(exited_path)}"
        command = "sh -c " + shlex.quote(
            f"trap {shlex.quote(on_exit)} EXIT; "
            f"printf started > {shlex.quote(started_path)}; "
            f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done"
        )
        long_prompt = (
            f"Use the {tool_name('command')} tool to run exactly this one command in the "
            "foreground and wait for it to finish. Do not create the release file or "
            f"run any other command. I will stop the running command: {command}"
        )
        # Start the long turn and read its stream only until it is observably streaming,
        # then close it (the turn keeps running server-side). This proves the turn actually
        # streamed before the read closes it, and the read cannot hang: it is hard
        # wall-clock bounded.
        saw_ui, saw_output_frame = _post_turn_until_streaming(e2e_client, sid, long_prompt)
        assert saw_ui, "first turn was not a valid AI-SDK message stream"
        assert saw_output_frame, (
            "the long turn produced no model or tool frame before it could be interrupted"
        )

        # Model output alone does not prove the tool started. The command owns
        # both markers; the fixture never releases its loop before interrupting.
        started = wait_for_file(e2e_client, sid, started_path, timeout=30.0)
        assert started == b"started", (
            f"the real command never confirmed execution: {started!r}"
        )
        names = file_names(e2e_client, sid)
        assert f"{marker}.release" not in names, "the command gate was released before interrupt"
        assert f"{marker}.exited" not in names, "the command exited before interrupt"
        in_flight = get_session(e2e_client, sid)
        assert str(in_flight.get("state") or "") in _IN_FLIGHT_STATES, (
            f"the tool was not in flight at the interrupt boundary: {in_flight}"
        )
        assert not in_flight.get("pending_interaction"), (
            "the command must be executing, not waiting for permission"
        )

        ack = data(e2e_client.post(f"/api/v1/sessions/{sid}/interrupt", timeout=30.0))
        assert str(ack.get("session_id") or "") == sid, (
            f"interrupt did not acknowledge the session: {ack}"
        )

        # READY describes session availability, not whether the last turn failed.
        # Wait for this first turn's durable outcome as well as an idle session.
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        settled = get_session(e2e_client, sid)
        while time.monotonic() < deadline:
            settled = get_session(e2e_client, sid)
            state = str(settled.get("state") or "")
            if state in TERMINAL_STATES:
                pytest.fail(f"session went terminal ({state}) after interrupt: {settled.get('last_error')!r}")
            if (
                state == "READY"
                and not settled.get("pending_interaction")
                and settled.get("last_turn_id")
                and settled.get("last_turn_status")
            ):
                break
            time.sleep(1.0)
        else:
            state = str(get_session(e2e_client, sid).get("state") or "")
            pytest.fail(
                f"session did not settle READY after interrupt within "
                f"{SETTLE_TIMEOUT_S:.0f}s; state={state!r}"
            )

        assert settled.get("last_turn_status") == "COMPLETED", (
            f"user interrupt was recorded as a failed turn: {settled}"
        )
        assert not settled.get("last_turn_error"), (
            f"user interrupt left a turn error: {settled.get('last_turn_error')!r}"
        )
        interrupted_turn_id = settled["last_turn_id"]

        # The gate remains closed: a normal tool finish cannot unblock this turn.
        _assert_sendable(e2e_client, sid)
        follow_up = get_session(e2e_client, sid)
        assert (
            follow_up.get("last_turn_id")
            and follow_up.get("last_turn_id") != interrupted_turn_id
        ), (
            "follow-up did not record a distinct completed turn"
        )
        assert follow_up.get("last_turn_status") == "COMPLETED", follow_up
        assert not follow_up.get("last_turn_error"), follow_up
    finally:
        release_session(sid)


def test_interrupt_clears_pending_interaction_and_settles(e2e_client: httpx.Client) -> None:
    """Interrupt a turn paused on a tool permission; it denies the tool and settles READY.

    default mode holds the Write as a native ``tool_approval`` interaction (the turn
    pauses). Interrupting while it waits denies the held tool — so the uniquely-named
    file is never created — clears the pending interaction, and settles the turn back to
    READY, after which the session still accepts a fresh turn.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))  # the mode under test; agent_chat defaults to bypassPermissions
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    write_path = workspace_path(e2e_client, sid, f"interrupt_{uuid.uuid4().hex[:8]}.txt")
    write_name = write_path.rsplit("/", 1)[-1]
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            turn = stream_turn(
                e2e_client,
                sid,
                content=(
                    f"Use the {tool_name('write')} tool to create a file at {write_path} "
                    "containing the single word MANGO. Do not run any other tool or command."
                ),
            )
            assert turn.error is None, turn.error
            assert not turn.interactions
            assert pending_interaction(e2e_client, sid) is None
            assert wait_for_file(e2e_client, sid, write_path, timeout=60.0) is not None
            _assert_sendable(e2e_client, sid)
            return

        # The turn pauses on the Write permission; stream_turn returns when it pauses.
        turn = stream_turn(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('write')} tool to create a file at {write_path} containing the "
                f"single word MANGO. Do not run any other tool or command."
            ),
        )
        assert turn.saw_ui_header, "turn was not a valid AI-SDK message stream"

        pi = pending_interaction(e2e_client, sid)
        if pi is None:
            pi = wait_for_pending_interaction(e2e_client, sid, timeout=20.0)
        assert pi is not None, (
            f"no pending interaction surfaced for the Write (finish={turn.finish_reason})"
        )
        assert str(pi.get("presentation")) == approval_presentation(), (
            f"expected a tool approval interaction: {pi}"
        )

        # Interrupt while WAITING_INPUT: the held tool is denied and the turn settles.
        ack = data(e2e_client.post(f"/api/v1/sessions/{sid}/interrupt", timeout=30.0))
        assert str(ack.get("session_id") or "") == sid, (
            f"interrupt did not acknowledge the session: {ack}"
        )

        settled = wait_until_settled(e2e_client, sid)
        assert str(settled.get("state")) == "READY", (
            f"session not READY after interrupt: {settled.get('state')!r}"
        )
        assert not settled.get("pending_interaction"), (
            "interrupt left the pending interaction uncleared"
        )

        # Load-bearing: the denied Write had no side effect. The turn is fully settled,
        # so the uniquely-named file must never have been created.
        assert write_name not in file_names(e2e_client, sid), (
            f"interrupted (denied) Write still created {write_path}"
        )

        # The session is immediately usable again.
        _assert_sendable(e2e_client, sid)
    finally:
        release_session(sid)
