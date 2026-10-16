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

* ``test_assistant_answers_a_second_message_in_the_same_conversation`` is
  marked ``assistant_live``: two messages down one Assistant conversation, the
  second answered from the first.

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
    answer_in_history,
    assert_kubernetes_runtime_image,
    create_session,
    data,
    deadline_after,
    get_admin_session_detail,
    get_session,
    history,
    poll_until_agent_ready,
    poll_until_ready,
    psql,
    release_assistant,
    release_session,
    send_and_follow,
    sql_literal,
    user_messages,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.runtime_preparation_test import _server_log
from tests.e2e.test_interactions import _waiting_interaction
from tests.e2e.test_prepared_runtime_activation import _run_json, _sandbox_exec

pytestmark = pytest.mark.e2e


def test_conversation_end_rejects_chat_session_without_teardown(e2e_client: httpx.Client) -> None:
    """End on a plain chat conversation is rejected, and the reject tears nothing down.

    create -> READY -> one answered message (a live conversation on a running
    sandbox) -> ``POST /conversation/end`` -> ``409 INVALID_REQUEST`` (end is only
    defined for assistant conversations) -> the session is still ``READY`` on the
    SAME sandbox. The rejected call must be a no-op on session and sandbox lifecycle.
    """
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # A live conversation with real context, running on a provisioned sandbox.
        res = send_and_follow(e2e_client, sid, "Reply with exactly: OK", until="end")
        assert res.error is None and res.finish_reason == "stop", (
            f"the response failed before the end call: {res.response}"
        )
        before = wait_until_settled(e2e_client, sid)
        sandbox_before = str(before.get("sandbox_id") or "")
        assert sandbox_before, f"session has no sandbox_id after an answered message: {before}"

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
    deadline = deadline_after(timeout)
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
    pytest.fail(f"assistant {assistant_id} workspace did not become READY in time: {last}")


@pytest.mark.assistant_live
def test_assistant_answers_a_second_message_in_the_same_conversation(
    e2e_client: httpx.Client,
) -> None:
    """Two messages in ONE conversation — the most basic thing a chat does.

    Every other Assistant case opens a fresh conversation per message, so a
    defect that only a SECOND message reaches — the runtime refusing or
    dropping it once it has answered the first — is caught only here.

    So this sends twice down one conversation and reads the answer, not just
    the part count: the second message must be answered, and answered FROM
    the first — a reply that lost the context would pass an error-free check
    while the conversation was, in the way that matters, broken.
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

        first = send_and_follow(
            e2e_client,
            session_id,
            f"Remember this code: {token}. Reply with just the word OK. Do not use any tool.",
            until="end",
        )
        assert first.error is None and first.finish_reason == "stop", (
            f"the first message's response did not end normally: {first.response}"
        )
        assert first.n_text_delta > 0, "the first message's response streamed no text"
        wait_until_settled(e2e_client, session_id)

        second = send_and_follow(
            e2e_client,
            session_id,
            "What was the code I asked you to remember? Reply with just the code.",
            until="end",
        )
        assert second.error is None and second.finish_reason == "stop", (
            f"the second message in the same conversation did not end normally: {second.response}"
        )
        assert second.n_text_delta > 0, "the second message's response streamed no text"
        assert token in second.text, (
            "the second message was answered without the first one's context: "
            f"{second.text[:200]!r}"
        )
        settled = wait_until_settled(e2e_client, session_id)
        assert settled["last_response"]["response_id"] == second.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        messages = history(e2e_client, session_id)
        assert [m["id"] for m in user_messages(messages)] == [
            first.client_message_id,
            second.client_message_id,
        ], [m["id"] for m in user_messages(messages)]
        for sent in (first, second):
            assert answer_in_history(messages, sent.input_id)["id"] == sent.response_id
    finally:
        release_session(session_id)
        release_assistant(assistant_id)


def _assistant_native_sessions(sandbox_id: str, assistant_id: str) -> list[dict]:
    """Read the supplier's live-session registry, without a platform runtime client."""
    config = psql(
        "SELECT runtime_identity->>'config_dir' FROM assistant_workspaces "
        f"WHERE assistant_id={sql_literal(assistant_id)}"
    )
    assert len(config) == 1 and config[0], "The workspace has no native profile path"
    program = """
import asyncio, json, os
import aiohttp
async def main():
    async with aiohttp.ClientSession() as client:
        async with client.ws_connect('http://127.0.0.1:9118/api/ws', headers={
            'X-AstraBox-Hermes-Token': os.environ['HERMES_DASHBOARD_SESSION_TOKEN'],
        }) as socket:
            await socket.send_json({'jsonrpc': '2.0', 'id': 'live-sessions', 'method': 'session.active_list', 'params': {}})
            while True:
                reply = await socket.receive_json(timeout=15)
                if reply.get('id') == 'live-sessions':
                    assert 'error' not in reply, 'Native active-list request failed'
                    print(json.dumps([{'id': row['id'], 'session_key': row['session_key']}
                                      for row in reply['result']['sessions']]))
                    return
asyncio.run(main())
"""
    result = _run_json([
        *_sandbox_exec(sandbox_id), "bash", "-c",
        'set -euo pipefail; source "$1"; exec "$HERMES_VENV/bin/python" -c "$2"',
        "hermes-native-observation", f"{config[0]}/astrabox-hermes.env", program,
    ])
    assert isinstance(result, list)
    return result


@pytest.mark.assistant_live
def test_assistant_conversation_end_starts_fresh_context_on_same_workspace(
    e2e_client: httpx.Client,
) -> None:
    """End an evicted native conversation, keep its sibling, and start a fresh context."""
    assistant_id = ""
    first_id = ""
    second_id = ""
    sibling_id = ""
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
        planted = send_and_follow(
            e2e_client,
            first_id,
            "Start this conversation with a brief greeting. Do not use any tool.",
            until="end",
        )
        assert planted.error is None and planted.finish_reason == "stop", (
            f"the first Assistant conversation's response did not end normally: {planted.response}"
        )
        assert planted.n_text_delta > 0, "the first Assistant conversation's response streamed no text"
        first_detail = wait_until_settled(e2e_client, first_id)
        first_admin_detail = get_admin_session_detail(e2e_client, first_id)
        first_engine_session = str(first_admin_detail.get("engine_session_key") or "").strip()
        assert first_engine_session, f"first conversation persisted no engine session: {first_detail}"

        sibling_id = data(e2e_client.post(
            f"/api/v1/assistants/{assistant_id}/conversations", json={},
        ))["session_id"]
        poll_until_ready(e2e_client, sibling_id, expected_image=assistant_image)
        marker = f"SIBLING-{uuid.uuid4().hex[:8]}"
        sibling = send_and_follow(e2e_client, sibling_id, (
            f"Remember {marker}. Reply with exactly OK. Do not use tools."
        ), until="end")
        assert sibling.error is None and sibling.finish_reason == "stop", sibling.response
        wait_until_settled(e2e_client, sibling_id)
        sibling_key = get_admin_session_detail(e2e_client, sibling_id)["engine_session_key"]
        waiting = send_and_follow(e2e_client, first_id, (
            "Use the clarify tool to ask whether I prefer RED or BLUE, with those two options. "
            "Wait for my answer before continuing. Do not answer the question yourself."
        ), until="pause")
        _waiting_interaction(e2e_client, waiting)
        native = _assistant_native_sessions(workspace_sandbox, assistant_id)
        target = [row for row in native if row["session_key"] == first_engine_session]
        sibling_native = [row for row in native if row["session_key"] == sibling_key]
        assert len(target) == len(sibling_native) == 1, native
        dispose_started = time.time()
        evicted = data(e2e_client.post(f"/api/v1/admin/sessions/{first_id}/evict-runtime"))
        assert evicted["evicted"] == first_id, evicted
        ended = data(e2e_client.post(f"/api/v1/sessions/{first_id}/conversation/end"))
        assert ended.get("status") == "conversation-ended", ended
        assert ended.get("killed") is False, ended
        assert str(ended.get("sandbox_id") or "") == workspace_sandbox, ended
        assert str(get_session(e2e_client, first_id).get("state") or "") == "TERMINATED"
        log = _server_log(require_service_container(SERVER_CONTAINER_HANDLE), dispose_started)
        assert f"session={first_id} engine=assistant source=native-identity" in log, (
            "The scene did not reach final disposal without a local client"
        )
        remaining = _assistant_native_sessions(workspace_sandbox, assistant_id)
        assert all(row["id"] != target[0]["id"] and row["session_key"] != first_engine_session for row in remaining), remaining
        assert sibling_native[0] in remaining, "Finalizing one native conversation closed its sibling"
        recalled = send_and_follow(e2e_client, sibling_id, (
            "Repeat the token I asked you to remember. Do not use tools."
        ), until="end")
        assert recalled.error is None and marker in recalled.text, recalled.error or recalled.text
        assert get_admin_session_detail(e2e_client, sibling_id)["sandbox_id"] == workspace_sandbox

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
        greeted = send_and_follow(
            e2e_client,
            second_id,
            "Start this conversation with a brief greeting. Do not use any tool.",
            until="end",
        )
        assert greeted.error is None and greeted.finish_reason == "stop", (
            f"the second Assistant conversation's response did not end normally: {greeted.response}"
        )
        assert greeted.n_text_delta > 0, "the second Assistant conversation's response streamed no text"
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
        release_session(sibling_id)
        release_assistant(assistant_id)
