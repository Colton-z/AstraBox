#!/usr/bin/env python3
"""Ask an Agent a question over the HTTP API and print the answer as it arrives.

Sending and receiving are two calls. ``POST /turn-inputs`` accepts the message
and answers with a receipt; the reply arrives on the Session's own event
stream, which replays from a cursor, so opening it after the POST loses
nothing.

    pip install httpx
    python examples/ask_agent.py "What is 1 + 1?"

Set ``ASTRABOX_BASE_URL`` for a deployment that is not on
``http://127.0.0.1:8088``, and ``ASTRABOX_AGENT_ID`` to choose an Agent other
than the first one the deployment lists. The Session keeps its sandbox after
this exits; delete it from the console, or with
``DELETE /api/v1/sessions/<session_id>``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

BASE_URL = os.environ.get("ASTRABOX_BASE_URL", "http://127.0.0.1:8088")
PROMPT = " ".join(sys.argv[1:]) or "What is 1 + 1? Reply with just the number."


def data(response: httpx.Response) -> Any:
    """Read one JSON envelope: every route answers ``{code, message, data}``."""
    response.raise_for_status()
    return response.json()["data"]


with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
    agent_id = os.environ.get("ASTRABOX_AGENT_ID") or data(client.get("/api/v1/agents"))[0]["agent_id"]
    session_id = data(client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))["session_id"]
    print(f"agent {agent_id} -> session {session_id}", file=sys.stderr)

    # A new Session builds its sandbox in the background. A turn sent while the
    # Session is still CREATING is refused with 409 SESSION_BUSY.
    while data(client.get(f"/api/v1/sessions/{session_id}"))["state"] == "CREATING":
        time.sleep(2)

    receipt = data(client.post(f"/api/v1/sessions/{session_id}/turn-inputs", json={"content": PROMPT}))
    print(f"turn {receipt['turn_id']} accepted", file=sys.stderr)

    with client.stream(
        "GET",
        f"/api/v1/sessions/{session_id}/ai-stream",
        params={"follow": "session"},
        timeout=httpx.Timeout(600.0, connect=30.0),
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            frame = json.loads(line[len("data: ") :])
            if frame.get("type") == "text-delta":
                print(frame["delta"], end="", flush=True)
            elif frame.get("type") == "error":
                sys.exit(f"\nturn failed: {frame.get('errorText')}")
            elif frame.get("type") == "data-result":
                # This Session has run exactly one turn, so the first result
                # frame ends it. A client that sends more than one message
                # matches the receipt's turn_id instead.
                print()
                break
