"""Live-turn e2e — the headless API integration smoke, in pytest + httpx.

This is the Python-layer twin of ``scripts/e2e_smoke.sh``: it drives the exact
open path the curl smoke does, but as an assertable test so it can gate CI (the
``e2e`` mark; needs a live deployment, skipped by default in unit CI).

What it proves:

* ``POST /api/v1/sessions`` cold-starts an agent sandbox and the
  session **state machine transitions** ``CREATING -> READY`` (the create
  response is ``CREATING``; polling converges to ``READY``; a terminal
  ``TERMINATED`` / ``RECOVERY_REQUIRED`` before READY fails the test with the
  surfaced ``last_error`` — no fake success).
* a READY session is bound to a concrete sandbox id.
* ``POST /api/v1/sessions/{id}/ai-stream`` streams AI-SDK Data-Stream-Protocol
  SSE (header ``x-vercel-ai-ui-message-stream: v1``), and at least one
  ``{"type":"text-delta","delta":"...2..."}`` frame arrives — the load-bearing
  proof that the selected engine and sandbox runtime surfaced a real model reply
  through the platform SSE chain.

A stream that opens and finishes with zero ``text-delta`` frames is an invalid
``(empty reply, exit=0)`` result. The test reports it through ``pytest.fail``
rather than accepting an empty answer.

Server under test
-----------------
Set ``ASTRABOX_E2E_BASE_URL`` to the already-running deployment under test. The
deployment owns its model credentials and sandbox topology; this test neither
boots a second server nor assumes its container runtime.

Run it explicitly (it is deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_live_turn.py -m e2e -s
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    data,
    get_session,
    poll_until_agent_ready,
    release_session,
)

pytestmark = pytest.mark.e2e

# Generous bounds: a cold sandbox + cold gateway is slow on the first turn.
READY_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_READY_TIMEOUT", "120"))
STREAM_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_STREAM_TIMEOUT", "180"))
PROMPT = os.getenv(
    "ASTRABOX_E2E_PROMPT", "What is 1 + 1? Reply with just the number."
)

# UI states (data_stream contract §1) that are terminal-before-ready failures.
_TERMINAL_STATES = {"TERMINATED", "RECOVERY_REQUIRED", "DELETED"}


def _stream_text_deltas(client: httpx.Client, sid: str) -> tuple[int, str, str | None, bool]:
    """POST the prompt, read the SSE, return (n_text_delta, text, error, saw_ui_hdr).

    Parses AI-SDK Data-Stream-Protocol frames: concatenates ``text-delta`` deltas,
    captures any in-band ``error`` frame, and confirms the
    ``x-vercel-ai-ui-message-stream`` response header.
    """
    n_delta = 0
    parts: list[str] = []
    saw_error: str | None = None
    saw_ui_header = False
    body = {"content": PROMPT, "client_message_id": str(uuid.uuid4())}
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/ai-stream",
        json=body,
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(STREAM_TIMEOUT_S, connect=30.0),
    ) as resp:
        ctype = resp.headers.get("content-type", "")
        # A pre-stream failure returns a JSON error envelope, not SSE.
        assert "text/event-stream" in ctype, (
            f"ai-stream did not return SSE (content-type={ctype!r}); "
            f"body={resp.read()[:400]!r}"
        )
        saw_ui_header = resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"
        for raw in resp.iter_lines():
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "text-delta":
                n_delta += 1
                delta = ev.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
            elif etype == "error":
                saw_error = str(ev.get("errorText") or "unknown error")
    return n_delta, "".join(parts), saw_error, saw_ui_header


def test_live_turn_streams_text_delta(e2e_client: httpx.Client) -> None:
    """create -> READY -> ai-stream -> a real text-delta containing "2"."""
    # ── liveness + seeded template ───────────────────────────────────────────
    health = e2e_client.get("/healthz")
    assert health.status_code == 200 and health.json().get("status") == "ok", health.text
    agents = data(e2e_client.get("/api/v1/agents"))
    assert agents, "no seeded agent to start a conversation against"

    # ── create (state machine starts at CREATING) ────────────────────────────
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    # The create call returns the detail object; per the contract it is typically
    # CREATING initially (sandbox provisioning is async).
    assert created.get("state") in {"CREATING", "READY"}, (
        f"unexpected initial state {created.get('state')!r}"
    )

    try:
        # ── poll to READY; assert the transition actually happened ───────────
        states = poll_until_agent_ready(e2e_client, sid)
        assert "READY" in states, states
        # CREATING must have been observed (or been the create-time state) — i.e.
        # the machine genuinely moved into READY rather than starting there with
        # no provisioning. (If the very first poll already caught READY, the
        # create response state CREATING still proves the transition.)
        assert created.get("state") == "CREATING" or "CREATING" in states, (
            f"never observed CREATING; create_state={created.get('state')!r} states={states}"
        )

        # A READY session is bound to concrete sandbox compute.
        detail = get_session(e2e_client, sid)
        assert str(detail.get("sandbox_id") or ""), (
            f"READY session has no sandbox_id: {detail}"
        )
        assert detail.get("runtime_unavailable") in (False, None), (
            f"READY session reports runtime_unavailable: {detail.get('runtime_unavailable')!r}"
        )

        # ── the live turn: stream + assert a real text-delta('2') ────────────
        n_delta, text, err, ui_header = _stream_text_deltas(e2e_client, sid)
        assert ui_header, "missing x-vercel-ai-ui-message-stream: v1 response header"
        assert err is None, f"ai-stream emitted an in-band error frame: {err}"
        # The fake-success signature this gate exists to catch.
        assert n_delta > 0, (
            "(empty reply, exit=0): the turn produced ZERO text-delta frames "
            f"(assembled text={text!r})"
        )
        assert "2" in text, (
            f"got {n_delta} text-delta frame(s) but assembled text has no '2': {text!r}"
        )
    finally:
        # Hand the session to the reaper rather than deleting it here: this runs
        # before pytest knows the outcome, and a failing live turn's sandbox is
        # the only place its cause is still visible.
        release_session(sid)
