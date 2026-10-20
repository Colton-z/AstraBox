"""An unused conversation recovers, then retains its first native tool history."""

from concurrent.futures import ThreadPoolExecutor
import json
import time
import uuid

import httpx
import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._sandbox_helpers import (
    create_session, data, durable_mounts_of_sandbox, get_admin_session_detail,
    history, kube_access, poll_until_agent_ready, psql_rows, release_session,
    send_and_follow, sql_literal, transcript_entries, wait_until_settled,
)
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = pytest.mark.e2e


def test_unused_conversation_recovers_and_keeps_its_first_native_history(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    assert current_profile()["engine_kind"] == "claude_code"
    sid = create_session(client)["session_id"]
    release_session(sid)
    poll_until_agent_ready(client, sid)
    initial = get_admin_session_detail(client, sid)
    assert initial["runtime_identity"]["isolated_session_id"], "this is the Agent-shared recovery scene"
    command = _sandbox_exec(initial["sandbox_id"])
    kubeconfig, namespace = kube_access()
    assert not durable_mounts_of_sandbox(
        pod=command[command.index("exec") + 1], kubeconfig=kubeconfig, namespace=namespace,
    ), "native custody must work without a persistent workspace volume"

    def custody() -> dict:
        return psql_rows(
            "SELECT s.engine_session_key, "
            "(SELECT count(*) FROM session_inputs i WHERE i.session_id=s.session_id) AS inputs, "
            "(SELECT count(*) FROM session_responses r WHERE r.session_id=s.session_id "
            "AND r.outcome IS NOT NULL) AS ended_responses, "
            "(SELECT coalesce(json_agg(json_build_object('native_session_id',t.native_session_id,"
            "'last_sequence',t.last_sequence)), '[]'::json) FROM transcript_scopes t "
            "WHERE t.platform_session_id=s.session_id AND t.subpath='') AS main_scopes "
            f"FROM sessions s WHERE s.session_id={sql_literal(sid)}"
        )[0]

    def recover() -> dict:
        data(client.post(f"/api/v1/admin/sessions/{sid}/kill", timeout=30))
        data(client.post(f"/api/v1/sessions/{sid}/recover", timeout=60))
        poll_until_agent_ready(client, sid)
        return get_admin_session_detail(client, sid)

    unused = custody()
    assert unused == {"engine_session_key": None, "inputs": 0, "ended_responses": 0, "main_scopes": []}
    assert history(client, sid) == []
    recovered = recover()
    assert recovered["runtime_identity"]["isolated_session_id"] != initial["runtime_identity"]["isolated_session_id"]
    assert custody() == unused

    marker = "native-memory-" + uuid.uuid4().hex[:12]
    with ThreadPoolExecutor(max_workers=1) as pool:
        reply = pool.submit(
            send_and_follow, client, sid,
            f"Remember the code {marker}. Use Bash once to run exactly: "
            f"printf '{marker}\\n'; sleep 8; printf 'native-tool-finished\\n'. "
            "Wait for the command to finish, then reply done. Do not start a background task.",
            until="end",
        )
        while time.monotonic() < live_test_deadline:
            running = custody()
            if running["main_scopes"]:
                break
            assert not reply.done(), "native history must exist before the first reply ends"
            time.sleep(0.15)
        else:
            pytest.fail("the real CLI never committed its first main transcript")
        assert not reply.done() and running["ended_responses"] == 0, running
        assert len(running["main_scopes"]) == 1, running
        native_key = running["main_scopes"][0]["native_session_id"]
        assert running["main_scopes"][0]["last_sequence"] > 0
        assert running["engine_session_key"] == native_key, running
        first = reply.result(timeout=max(0.1, live_test_deadline - time.monotonic()))
    assert first.error is None and "Bash" in first.tool_names, first.response
    wait_until_settled(client, sid)
    before = transcript_entries(sid)
    bash_ids = {
        block["id"] for row in before if row["entry"].get("type") == "assistant"
        for block in row["entry"].get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"
    }
    assert bash_ids
    assert any(
        block.get("tool_use_id") in bash_ids and not block.get("is_error")
        and marker in json.dumps(block.get("content")) and "native-tool-finished" in json.dumps(block.get("content"))
        for row in before if row["entry"].get("type") == "user"
        for block in row["entry"].get("message", {}).get("content", []) if isinstance(block, dict)
    ), "the native tool result, not merely the assistant's text, must be retained"
    visible = history(client, sid)
    shown = [part for message in visible for part in message.get("parts", [])
             if part.get("toolCallId") in bash_ids]
    assert shown and all(part.get("state") == "output-available" for part in shown), shown
    assert any(marker in json.dumps(part.get("output")) and "native-tool-finished" in json.dumps(part.get("output"))
               for part in shown), shown
    recover()
    assert custody()["engine_session_key"] == native_key
    assert transcript_entries(sid)[:len(before)] == before
    assert history(client, sid) == visible
    second = send_and_follow(client, sid, "What code did I ask you to remember? Reply with only that code; do not use tools.", until="end")
    assert second.error is None and marker in second.text, second.response
    wait_until_settled(client, sid)
    assert custody()["engine_session_key"] == native_key
    assert transcript_entries(sid)[:len(before)] == before
    assert history(client, sid)[:len(visible)] == visible
