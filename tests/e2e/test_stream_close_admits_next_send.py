"""A turn's response body must not end before the next send is admissible.

The client has no signal between "the body ended" and "I may send again": the
UI sends the next turn the moment the stream closes. So the close is a promise
that the turn slot is free, and anything that ends the body earlier — a bridge
that finished before the terminal, a live drain that beat the worker's
mirror-fed settle — turns that promise into a ``409 SESSION_BUSY`` on a turn
that is already over.

That gap was measured at roughly 100ms and closed by moving the settle wait to
the one chokepoint every tail exit passes. Nothing pinned it, and the contract
outlives the implementation that currently keeps it: whatever ends a stream in
future has the same promise to keep.

The assertion here is deliberately about ORDER, not about internal state. It
sends with no delay at all — no sleep, no poll, no readiness check — because
any wait would hide exactly the window under test.

    .venv/bin/python -m pytest tests/e2e/test_stream_close_admits_next_send.py -m e2e -s
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    data,
    delete_session,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    stream_turn,
    tool_name,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _send_with_no_gap(client: httpx.Client, sid: str, content: str) -> tuple[int, str]:
    """POST the next turn and read its status before touching the body.

    Returns ``(status_code, error_code)``; ``error_code`` is empty on success.
    The body is drained so the turn does not outlive the assertion.
    """
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/ai-stream",
        json={"content": content, "client_message_id": str(uuid.uuid4())},
        headers={"Accept": "text/event-stream"},
    ) as resp:
        status = resp.status_code
        if status >= 400:
            resp.read()
            try:
                code = str(resp.json().get("code") or "")
            except Exception:
                code = resp.text[:200]
            return status, code
        for _ in resp.iter_lines():
            pass
    return status, ""


def test_next_send_is_admissible_the_instant_the_body_ends(
    e2e_client: httpx.Client,
) -> None:
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    poll_until_agent_ready(e2e_client, sid)

    first = stream_turn(e2e_client, sid, content="Say READY and nothing else.")
    assert first.error is None, f"first turn errored: {first.error}"
    assert first.finish_reason == "stop", (
        f"first turn did not complete (finish={first.finish_reason!r}); this test "
        "needs a turn that ends on its own, not one paused on an interaction"
    )

    # No wait of any kind between the body ending above and the send below.
    status, code = _send_with_no_gap(e2e_client, sid, "Say AGAIN and nothing else.")

    assert status != 409, (
        f"the next send was refused with HTTP {status} ({code or 'no code'}) "
        "immediately after the previous turn's body ended — the stream closed "
        "before its turn slot was admissible"
    )
    assert status == 200, f"next send failed with HTTP {status}: {code}"


def _messages(client: httpx.Client, sid: str) -> list:
    body = data(client.get(f"/api/v1/sessions/{sid}/messages"))
    msgs = body.get("messages")
    assert isinstance(msgs, list), f"messages payload is not a list: {body}"
    return msgs


def _visible_text(message: object) -> str:
    """The reader-visible message text, excluding thinking and tool metadata."""

    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def test_a_send_during_a_running_turn_queues_behind_it(
    e2e_client: httpx.Client,
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
) -> None:
    """A send while a turn is running joins the conversation's durable FIFO.

    The engine seam makes strict FIFO input an admission requirement: a busy
    conversation accepts the next input and orders it behind the running
    turn. Ordering, not refusal, is the mechanism that prevents corruption —
    what must never happen is a second concurrent turn interleaving one
    session.

    So this pins the whole FIFO contract: the send is accepted while the
    first turn is demonstrably live, its input is consumed exactly once,
    its answer lands strictly AFTER the running turn's answer, and the
    session settles clean.

    The first stream is held open rather than drained: reading a frame proves
    the turn is live at the moment the second send is made, which no amount of
    waiting can prove.
    """
    # Unattended, because the long turn this needs is a COMMAND, and a mode
    # that gates commands parks it on an approval instead — the contract here
    # is the order two sends settle in, not the permission matrix.
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    poll_until_agent_ready(e2e_client, sid)
    first_marker = f"FIRST_DONE_{uuid.uuid4().hex[:8]}"
    second_marker = f"SECOND_DONE_{uuid.uuid4().hex[:8]}"
    try:
        with e2e_client.stream(
            "POST",
            f"/api/v1/sessions/{sid}/ai-stream",
            json={
                "content": (
                    f"Run `sleep 20` with the {tool_name('command')} tool, "
                    "then reply with only "
                    f"{first_marker}."
                ),
                "client_message_id": str(uuid.uuid4()),
            },
            headers={"Accept": "text/event-stream"},
        ) as running:
            assert running.status_code == 200, f"first turn refused: {running.status_code}"
            saw_frame = False
            # One iterator for the whole stream: httpx refuses a second
            # iter_lines() on a partially consumed response.
            running_lines = running.iter_lines()
            for line in running_lines:
                if line.strip():
                    saw_frame = True
                    break
            assert saw_frame, "the first turn produced no frame, so it was never running"

            with httpx.Client(
                base_url=e2e_base_url,
                headers=e2e_auth_headers,
                timeout=30.0,
            ) as second:
                # Status only; the body is closed immediately. The queued
                # input's execution belongs to the platform's worker, not to
                # this response object, so abandoning the stream must not
                # abandon the input — that is part of what settles below.
                with second.stream(
                    "POST",
                    f"/api/v1/sessions/{sid}/ai-stream",
                    json={
                        "content": f"Reply with only {second_marker}.",
                        "client_message_id": str(uuid.uuid4()),
                    },
                    headers={"Accept": "text/event-stream"},
                ) as queued:
                    assert queued.status_code == 200, (
                        f"a send during a running turn must join the FIFO, got "
                        f"HTTP {queued.status_code}"
                    )
            for _ in running_lines:
                pass

        # Both inputs belong to one FIFO now; the queued one's answer must
        # arrive on its own, ordered behind the first turn's.
        deadline = time.monotonic() + 120.0
        ordered = False
        while time.monotonic() < deadline:
            transcript = "\n".join(
                _visible_text(m)
                for m in _messages(e2e_client, sid)
                if isinstance(m, dict) and str(m.get("role")) == "assistant"
            )
            first_at = transcript.find(first_marker)
            second_at = transcript.find(second_marker)
            if first_at >= 0 and second_at >= 0:
                assert second_at > first_at, (
                    "the queued input's answer overtook the running turn's — "
                    f"FIFO order broke ({first_marker} at {first_at}, "
                    f"{second_marker} at {second_at})"
                )
                ordered = True
                break
            time.sleep(1.0)
        assert ordered, (
            "the queued input was accepted but its answer never arrived — "
            "admitted-then-stranded is the failure the old refusal never allowed"
        )

        final = wait_until_settled(e2e_client, sid)
        assert str(final.get("state")) == "READY"
        assert not final.get("pending_interaction")
        user_texts = [
            _visible_text(m)
            for m in _messages(e2e_client, sid)
            if isinstance(m, dict) and str(m.get("role")) == "user"
        ]
        assert sum(second_marker in text for text in user_texts) == 1, (
            "the queued input must be consumed exactly once"
        )
    finally:
        # The outcome-aware reaper deletes this on pass and keeps the exact
        # session/sandbox on failure. Direct deletion here erased the FIFO
        # scene and let a slow DELETE mask the admitted-then-stranded assert.
        release_session(sid)


def test_the_session_is_usable_after_the_no_gap_pair(
    e2e_client: httpx.Client,
) -> None:
    """The close contract must not be met by leaving the second turn wedged.

    A close that admits the next send and then strands it would satisfy the
    test above while breaking the session, so the pair is driven once more and
    the session is required to answer.
    """
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    poll_until_agent_ready(e2e_client, sid)
    try:
        first = stream_turn(e2e_client, sid, content="Say ONE and nothing else.")
        assert first.error is None, f"first turn errored: {first.error}"

        status, code = _send_with_no_gap(e2e_client, sid, "Say TWO and nothing else.")
        assert status == 200, f"no-gap send refused: HTTP {status} {code}"

        third = stream_turn(e2e_client, sid, content="Say THREE and nothing else.")
        assert third.error is None, f"turn after the no-gap pair errored: {third.error}"
        assert third.finish_reason == "stop", (
            f"session did not recover after the no-gap pair "
            f"(finish={third.finish_reason!r})"
        )
        detail = data(e2e_client.get(f"/api/v1/sessions/{sid}"))
        assert not detail.get("pending_interaction"), (
            "a pending interaction was left behind by the no-gap pair"
        )
    finally:
        delete_session(e2e_client, sid)
