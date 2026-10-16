"""Runtime-eviction context-preservation e2e — the in-memory runtime is a cache.

Proves that dropping a session's cached in-memory runtime is transparent to the
conversation. The runtime held in the control-plane process is only a cache over
durable state: the sandbox container, its workspace, and the engine's native
conversation transcript persist independently. So after the cache is dropped the
session's reader connects to the same box again and the next message *resumes
the same conversation* rather than starting a fresh one.

Flow:

* create a session and send one message that plants a unique, memorable token
  (``ZEBRA-<hex>``); its response ends and the session settles back to READY.
* call the operator ``evict-runtime`` endpoint, which drops the runtime held in
  process memory without touching the sandbox container, its workspace, or the
  persisted transcript. The session stays READY (eviction is an in-process cache
  drop, not a state transition).
* send a second message that asks the agent to recall the token. With the
  cached runtime gone, the reader reconnects to the still-running box and the
  engine answers from the prior conversation, in the same box, with the *same*
  token.

The load-bearing assertion is that the token survives the eviction: a reconnect
that silently starts a fresh engine conversation answers without it.

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
    send_and_follow,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _evict_runtime(client: httpx.Client, sid: str) -> dict:
    """Drop the session's cached in-memory runtime via the operator endpoint.

    Returns the unwrapped envelope ``data`` (``{"evicted": <session_id>}``). This
    clears only the process-memory runtime handle; the sandbox container and its
    durable workspace/transcript are left intact, so the next message must reach
    the runtime again and resume the persisted conversation.
    """
    return data(client.post(f"/api/v1/admin/sessions/{sid}/evict-runtime"))


def test_evict_runtime_preserves_context(e2e_client: httpx.Client) -> None:
    """plant a token -> evict the in-memory runtime -> the recall after eviction.

    The planting response, eviction confirmation, transparent READY state and
    the recall after eviction are all asserted. A reconnect that wedges or
    silently starts a fresh conversation is a product failure.
    """
    token = f"ZEBRA-{uuid.uuid4().hex[:8]}"
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # Plant the memorable token. Deterministic prompt: the exact token goes
        # in, a fixed one-word acknowledgement comes out.
        planted = send_and_follow(
            e2e_client,
            sid,
            f"Remember this exact token for later: {token}. "
            f"Reply with only the word OK and nothing else.",
            until="end",
        )
        assert planted.error is None, f"the planting response failed: {planted.error}"
        assert planted.finish_reason == "stop", (
            f"the planting response did not end normally: {planted.finish_reason!r}"
        )
        settled = wait_until_settled(e2e_client, sid)
        box = str(settled.get("sandbox_id") or "")
        assert box, f"the settled conversation holds no sandbox: {settled}"

        # Evict the cached runtime. This is an in-process cache drop only, so the
        # session must stay READY — eviction is not a state transition.
        evicted = _evict_runtime(e2e_client, sid)
        assert evicted.get("evicted") == sid, (
            f"evict-runtime did not confirm the evicted session: {evicted}"
        )
        after_evict = get_session(e2e_client, sid)
        assert str(after_evict.get("state")) == "READY", (
            f"runtime eviction changed session state (must be transparent): "
            f"{after_evict.get('state')!r}"
        )

        # The next message reaches the evicted runtime's still-running box and
        # resumes the same native conversation.
        recalled = send_and_follow(
            e2e_client,
            sid,
            "What exact token did I ask you to remember? Reply with only that "
            "token and nothing else.",
            until="end",
        )
        assert recalled.error is None, f"the recall after eviction failed: {recalled.error}"
        assert recalled.n_text_delta > 0, "the recall after eviction streamed no text"
        assert token in recalled.text, (
            f"runtime eviction lost the conversation token {token!r}: {recalled.text!r}"
        )
        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["response_id"] == recalled.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        assert settled.get("sandbox_id") == box, (
            f"eviction moved the conversation off its live box: {box!r} -> "
            f"{settled.get('sandbox_id')!r}"
        )
    finally:
        release_session(sid)
