"""Runtime-eviction context-preservation e2e — the in-memory runtime is a cache.

Proves that dropping a session's cached in-memory runtime is transparent to the
conversation. The runtime held in the control-plane process is only a cache over
durable state: the sandbox container, its workspace, and the engine's native
conversation transcript persist independently. So after the cache is dropped the
next turn transparently rebuilds the runtime and *resumes the same conversation*
rather than starting a fresh one.

Flow:

* create a session and drive one turn that plants a unique, memorable token
  (``ZEBRA-<hex>``); assert that turn settles back to READY.
* call the operator ``evict-runtime`` endpoint, which drops the runtime held in
  process memory without touching the sandbox container, its workspace, or the
  persisted transcript. The session projection must stay READY (eviction is an
  in-process cache drop, not a state transition).
* drive a second turn that asks the agent to recall the token. With the cached
  runtime gone, the turn must rebuild it from the durable sandbox and resume the
  prior conversation, so the streamed reply contains the *same* token.

The load-bearing assertion is that the token survives the eviction. The
``open_sandbox`` path re-dials the resident in-box server, then the second turn
must resume the engine's native conversation rather than silently starting over.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_evict_runtime.py -m e2e -s
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    release_session,
    create_session,
    data,
    get_session,
    poll_until_agent_ready,
    stream_turn,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _evict_runtime(client: httpx.Client, sid: str) -> dict:
    """Drop the session's cached in-memory runtime via the operator endpoint.

    Returns the unwrapped envelope ``data`` (``{"evicted": <session_id>}``). This
    clears only the process-memory runtime handle; the sandbox container and its
    durable workspace/transcript are left intact, so the next turn must rebuild
    the runtime and resume the persisted conversation.
    """
    return data(client.post(f"/api/v1/admin/sessions/{sid}/evict-runtime"))


def test_evict_runtime_preserves_context(e2e_client: httpx.Client) -> None:
    """plant a token -> evict the in-memory runtime -> the post-evict recall turn.

    The planting turn, eviction confirmation, transparent READY projection, and
    post-eviction recall are all asserted. A reconnect that wedges or silently
    starts a fresh conversation is a product failure.
    """
    token = f"ZEBRA-{uuid.uuid4().hex[:8]}"
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # Turn 1 — plant the memorable token. Deterministic prompt: the exact
        # token goes in, a fixed one-word acknowledgement comes out.
        res1 = stream_turn(
            e2e_client,
            sid,
            content=(
                f"Remember this exact token for later: {token}. "
                f"Reply with only the word OK and nothing else."
            ),
        )
        assert res1.error is None, f"planting turn errored before settling: {res1.error}"
        settled1 = wait_until_settled(e2e_client, sid)
        assert str(settled1.get("state")) == "READY", (
            f"session not READY after the planting turn: {settled1.get('state')!r}"
        )

        # Evict the cached runtime. This is an in-process cache drop only, so the
        # session projection must stay READY — eviction is not a state transition.
        evicted = _evict_runtime(e2e_client, sid)
        assert evicted.get("evicted") == sid, (
            f"evict-runtime did not confirm the evicted session: {evicted}"
        )
        after_evict = get_session(e2e_client, sid)
        assert str(after_evict.get("state")) == "READY", (
            f"runtime eviction changed session state (must be transparent): "
            f"{after_evict.get('state')!r}"
        )

        # The next turn rebuilds the evicted runtime from the still-running box
        # and resumes the same native conversation.
        res2 = stream_turn(
            e2e_client,
            sid,
            content=(
                "What exact token did I ask you to remember? Reply with only that "
                "token and nothing else."
            ),
        )
        assert res2.error is None, f"post-eviction recall turn errored: {res2.error}"
        assert res2.n_text_delta > 0, "post-eviction recall turn streamed no text"
        assert token in res2.text, (
            f"runtime eviction lost the conversation token {token!r}: {res2.text!r}"
        )
        settled2 = wait_until_settled(e2e_client, sid)
        assert str(settled2.get("state")) == "READY", (
            f"session not READY after post-eviction recall: {settled2.get('state')!r}"
        )
    finally:
        release_session(sid)
