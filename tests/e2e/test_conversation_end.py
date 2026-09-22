"""End-conversation contract e2e — what ``POST /conversation/end`` does to a session.

``POST /api/v1/sessions/{id}/conversation/end`` is scoped to *assistant*
conversations, where it ends the active conversation on a shared, long-lived
workspace sandbox without destroying that sandbox. The public session API
(``POST /api/v1/sessions``) mints a plain *chat* conversation, so on this backend
the end-conversation call is **rejected** — and, importantly, the rejection is
non-destructive: the live session and its sandbox are left exactly as they were.

Two deployment-specific contracts are proven here:

* ``test_conversation_end_rejects_chat_session_without_teardown`` — the always-on
  reality of the public surface. A chat conversation cannot be ended: the call
  returns ``409 INVALID_REQUEST`` and the session stays ``READY`` bound to the SAME
  sandbox. Nothing is torn down. This is the load-bearing "keeps the session and
  sandbox alive" guarantee, realized here as a no-op rejection rather than a reset.

* ``test_assistant_conversation_end_starts_fresh_context_on_same_workspace`` is
  marked ``assistant_live``. It creates a real Assistant/Hermes workspace,
  terminates one conversation without killing the shared box, then proves a new
  conversation on that same box owns a different engine session.

Run its dedicated lane against the pinned Assistant Environment (deselected in
the default unit run):

    E2E_LANE=assistant ./scripts/e2e_live.sh
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_kubernetes_runtime_image,
    create_session,
    data,
    get_admin_session_detail,
    get_session,
    poll_until_agent_ready,
    poll_until_ready,
    release_assistant,
    release_session,
    stream_turn,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def test_conversation_end_rejects_chat_session_without_teardown(e2e_client: httpx.Client) -> None:
    """End on a plain chat conversation is rejected, and the reject tears nothing down.

    create -> READY -> one real turn (a live conversation on a running sandbox) ->
    ``POST /conversation/end`` -> ``409 INVALID_REQUEST`` (end is only defined for
    assistant conversations) -> the session is still ``READY`` on the SAME sandbox.
    The rejected call must be a no-op on session and sandbox lifecycle.
    """
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # A live conversation with real context, running on a provisioned sandbox.
        res = stream_turn(e2e_client, sid, content="Reply with exactly: OK")
        assert res.error is None, f"turn errored before the end call: {res.error}"
        before = wait_until_settled(e2e_client, sid)
        sandbox_before = str(before.get("sandbox_id") or "")
        assert sandbox_before, f"session has no sandbox_id after a settled turn: {before}"

        # No helper wraps this route — call it directly. conversation/end is defined
        # only for assistant conversations; this is a plain chat conversation, so the
        # backend rejects it.
        resp = e2e_client.post(f"/api/v1/sessions/{sid}/conversation/end")
        assert resp.status_code == 409, (
            f"expected 409 rejecting end on a chat conversation, got "
            f"{resp.status_code}: {resp.text[:400]}"
        )
        body = resp.json()
        assert body.get("code") == "INVALID_REQUEST", f"unexpected error envelope: {body}"
        assert "assistant" in str(body.get("message") or "").lower(), (
            f"reject reason should name the assistant-conversation restriction: {body}"
        )

        # The load-bearing assertion: the rejected end is NON-DESTRUCTIVE. The session
        # is still alive (READY, not terminal — wait_until_settled fails loud on a
        # terminal state) and bound to the SAME sandbox. conversation/end did not tear
        # the session or its sandbox down.
        after = wait_until_settled(e2e_client, sid)
        assert str(after.get("state")) == "READY", (
            f"session not READY after a rejected end: {after.get('state')!r}"
        )
        assert str(after.get("sandbox_id") or "") == sandbox_before, (
            f"sandbox changed after a rejected end: "
            f"{after.get('sandbox_id')!r} != {sandbox_before!r}"
        )
    finally:
        release_session(sid)


def _assistant_runtime(
    e2e_client: httpx.Client,
) -> tuple[str, str, str, str, str, str]:
    expected_name = os.getenv("ASTRABOX_E2E_ASSISTANT_ENVIRONMENT", "").strip()
    expected_image = os.getenv("ASTRABOX_E2E_ASSISTANT_IMAGE", "").strip()
    expected_digest = os.getenv(
        "ASTRABOX_E2E_ASSISTANT_IMAGE_DIGEST", ""
    ).strip()
    expected_model = os.getenv("ASTRABOX_E2E_ASSISTANT_MODEL", "").strip()
    kubeconfig = os.getenv("ASTRABOX_E2E_KUBECONFIG", "").strip()
    namespace = os.getenv("ASTRABOX_E2E_KUBE_NAMESPACE", "").strip()
    assert expected_name and expected_image and expected_digest and expected_model, (
        "assistant_live requires the exact Assistant Environment, Hermes image digest, "
        "and smoke-proven model"
    )
    assert kubeconfig and namespace, "assistant_live requires Kubernetes proof inputs"
    environments = data(e2e_client.get("/api/v1/admin/environments"))
    matches = [item for item in environments if item.get("name") == expected_name]
    assert len(matches) == 1, (
        f"expected one Assistant Environment {expected_name!r}, found {len(matches)}"
    )
    actual = matches[0]
    assert actual.get("engine_kind") == "assistant", actual
    assert actual.get("enabled") is True, actual
    assert actual.get("runtime_template_name") == expected_image, actual
    model_catalog = data(
        e2e_client.get(
            f"/api/v1/admin/environments/{expected_name}/models"
        )
    )
    assert expected_model in model_catalog.get("models", []), model_catalog
    return (
        expected_name,
        expected_image,
        expected_digest,
        expected_model,
        kubeconfig,
        namespace,
    )


def _wake_assistant(e2e_client: httpx.Client, assistant_id: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = data(
            e2e_client.post(
                f"/api/v1/assistants/{assistant_id}/workspace/wake",
                timeout=120.0,
            )
        )
        if str(last.get("state") or "") == "READY" and last.get("current_sandbox_id"):
            return last
        time.sleep(3.0)
    pytest.fail(
        f"assistant {assistant_id} workspace did not become READY within {timeout:.0f}s: {last}"
    )


@pytest.mark.assistant_live
def test_assistant_answers_a_second_message_in_the_same_conversation(
    e2e_client: httpx.Client,
) -> None:
    """Two turns in ONE conversation — the most basic thing a chat does.

    Nothing covered it. Every Assistant e2e here opens a fresh conversation per
    turn, and so did every real-box probe, so a defect that only a SECOND
    message can reach shipped: the engine client released its turn slot after
    yielding the terminal frame, in code the consumer's break makes
    unreachable, and the second message came back "already has an active
    turn". The unit tests that drained the stream to exhaustion stayed green
    throughout, because draining resumes the generator past that yield.

    So this sends twice down one conversation and reads the answer, not just
    the frame count: the second turn must answer, and it must answer FROM the
    first — a reply that lost the context would pass an error-free check while
    the conversation was, in the way that matters, broken.
    """

    assistant_id = ""
    session_id = ""
    token = f"ASTRABOX-{uuid.uuid4().hex[:6].upper()}"
    try:
        (
            assistant_environment,
            assistant_image,
            _assistant_image_digest,
            assistant_model,
            _kubeconfig,
            _kube_namespace,
        ) = _assistant_runtime(e2e_client)
        assistant = data(
            e2e_client.post(
                "/api/v1/assistants",
                json={
                    "display_name": f"__e2e_second_message_{uuid.uuid4().hex[:8]}",
                    "environment_name": assistant_environment,
                    "model_config_override": {"model_name": assistant_model},
                },
            )
        )
        assistant_id = str(assistant.get("assistant_id") or "")
        assert assistant_id, f"assistant create returned no id: {assistant}"
        _wake_assistant(e2e_client, assistant_id)

        conversation = data(
            e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
        )
        session_id = str(conversation.get("session_id") or "")
        assert session_id, f"Assistant conversation returned no session: {conversation}"
        poll_until_ready(e2e_client, session_id, expected_image=assistant_image)

        first = stream_turn(
            e2e_client,
            session_id,
            content=(
                f"Remember this code: {token}. Reply with just the word OK. "
                "Do not use any tool."
            ),
        )
        assert first.error is None, f"first turn errored: {first.error}"
        assert first.n_text_delta > 0, "first turn streamed no text"
        wait_until_settled(e2e_client, session_id)

        second = stream_turn(
            e2e_client,
            session_id,
            content="What was the code I asked you to remember? Reply with just the code.",
        )
        assert second.error is None, (
            f"the second message in the same conversation errored: {second.error}"
        )
        assert second.n_text_delta > 0, "second turn streamed no text"
        assert token in second.text, (
            "the second turn answered without the first turn's context: "
            f"{second.text[:200]!r}"
        )
        wait_until_settled(e2e_client, session_id)
    finally:
        release_session(session_id)
        release_assistant(assistant_id)


@pytest.mark.assistant_live
def test_assistant_conversation_end_starts_fresh_context_on_same_workspace(
    e2e_client: httpx.Client,
) -> None:
    """End one Assistant conversation, then prove the next gets a new engine session."""
    assistant_id = ""
    first_id = ""
    second_id = ""
    try:
        (
            assistant_environment,
            assistant_image,
            assistant_image_digest,
            assistant_model,
            kubeconfig,
            kube_namespace,
        ) = _assistant_runtime(e2e_client)
        assistant = data(
            e2e_client.post(
                "/api/v1/assistants",
                json={
                    "display_name": f"__e2e_conversation_end_{uuid.uuid4().hex[:8]}",
                    "environment_name": assistant_environment,
                    "model_config_override": {"model_name": assistant_model},
                },
            )
        )
        assistant_id = str(assistant.get("assistant_id") or "")
        assert assistant_id, f"assistant create returned no id: {assistant}"
        workspace = _wake_assistant(e2e_client, assistant_id)
        workspace_sandbox = str(workspace.get("current_sandbox_id") or "")
        assert_kubernetes_runtime_image(
            sandbox_id=workspace_sandbox,
            expected_image=assistant_image,
            expected_digest=assistant_image_digest,
            kubeconfig=kubeconfig,
            namespace=kube_namespace,
        )

        first = data(
            e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
        )
        first_id = str(first.get("session_id") or "")
        assert first_id, f"first Assistant conversation returned no session: {first}"
        poll_until_ready(e2e_client, first_id, expected_image=assistant_image)
        planted = stream_turn(
            e2e_client,
            first_id,
            content="Start this conversation with a brief greeting. Do not use any tool.",
        )
        assert planted.error is None, f"first Assistant turn errored: {planted.error}"
        assert planted.n_text_delta > 0, "first Assistant turn streamed no text"
        first_detail = wait_until_settled(e2e_client, first_id)
        first_admin_detail = get_admin_session_detail(e2e_client, first_id)
        first_engine_session = str(first_admin_detail.get("engine_session_key") or "").strip()
        assert first_engine_session, f"first conversation persisted no engine session: {first_detail}"

        ended = data(e2e_client.post(f"/api/v1/sessions/{first_id}/conversation/end"))
        assert ended.get("status") == "conversation-ended", ended
        assert ended.get("killed") is False, ended
        assert str(ended.get("sandbox_id") or "") == workspace_sandbox, ended
        assert str(get_session(e2e_client, first_id).get("state") or "") == "TERMINATED"

        second = data(
            e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
        )
        second_id = str(second.get("session_id") or "")
        assert second_id, f"second Assistant conversation returned no session: {second}"
        poll_until_ready(e2e_client, second_id, expected_image=assistant_image)
        second_before = get_session(e2e_client, second_id)
        assert str(second_before.get("sandbox_id") or "") == workspace_sandbox
        second_before_admin = get_admin_session_detail(e2e_client, second_id)
        assert str(second_before_admin.get("engine_session_key") or "").strip() != first_engine_session, (
            f"new conversation inherited the ended engine session: {second_before_admin}"
        )
        second_turn = stream_turn(
            e2e_client,
            second_id,
            content="Start this conversation with a brief greeting. Do not use any tool.",
        )
        assert second_turn.error is None, f"second Assistant turn errored: {second_turn.error}"
        assert second_turn.n_text_delta > 0, "second Assistant turn streamed no text"
        second_detail = wait_until_settled(e2e_client, second_id)
        second_admin_detail = get_admin_session_detail(e2e_client, second_id)
        second_engine_session = str(second_admin_detail.get("engine_session_key") or "").strip()
        assert second_engine_session, (
            f"second conversation persisted no engine session: {second_detail}"
        )
        assert second_engine_session != first_engine_session, (
            "conversation/end reused the ended engine context: "
            f"{first_engine_session!r}"
        )
    finally:
        release_session(first_id)
        release_session(second_id)
        release_assistant(assistant_id)
