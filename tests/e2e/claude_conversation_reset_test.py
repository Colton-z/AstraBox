"""A native conversation reset remains readable through an ordinary reader reattach."""

import datetime
import json
import os
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e import _sandbox_helpers as h
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_evict_runtime import _evict_runtime

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def test_native_clear_continues_after_reset_and_reader_replay(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    environment = os.environ["ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"]
    assert environment.startswith("claude-reset-"), "use this case's isolated RuntimeProfile"
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    agent = h.data(client.post("/api/v1/agents", json={
        "name": f"Native reset {uuid.uuid4().hex[:8]}", "runtime_profile_name": environment,
        "model": os.environ["ASTRABOX_E2E_ASSISTANT_MODEL"], "prepared_capacity": 0,
    }))["agent_id"]
    sid = h.data(client.post(f"/api/v1/agents/{agent}/conversations", json={}))['session_id']
    h.poll_until_ready(client, sid, expected_image=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE"])
    assert h.get_session(client, sid)["engine_kind"] == "claude_code"
    box = h.get_session(client, sid)["sandbox_id"]
    h.data(client.post(f"/api/v1/sessions/{sid}/permission-mode",
                       json={"permission_mode": "bypassPermissions"}))
    before_marker = "BEFORE_RESET_" + uuid.uuid4().hex
    before = h.send_and_follow(client, sid, f"Use Bash once to run: printf {before_marker}. "
                               "Reply with only its output.", until="end")
    assert before.error is None and before_marker in before.text and before.tool_names
    h.wait_until_settled(client, sid)
    native_sql = (
        "SELECT s.scope_id,s.native_session_id,e.seq,e.entry_json FROM transcript_entries e "
        "JOIN transcript_scopes s ON s.scope_id=e.scope_id "
        f"WHERE s.platform_session_id={h.sql_literal(sid)} ORDER BY s.scope_id,e.seq"
    )

    def native():
        return h.psql_rows(native_sql)

    def resume_key():
        return h.psql_rows("SELECT engine_session_key FROM sessions "
                           f"WHERE session_id={h.sql_literal(sid)}")[0]["engine_session_key"]

    original_key = resume_key()
    original_native = native()
    assert original_key and original_native and before_marker in json.dumps(original_native)
    cleared = h.send_and_follow(client, sid, "/clear", until="end")
    assert cleared.error is None and cleared.ended, cleared.stream.summary()
    h.wait_until_settled(client, sid)
    reset_results = [part["data"] for part in h.recorded_output(sid, cleared.response_id)
                     if part["type"] == "data-result"]
    assert len(reset_results) == 1
    assert reset_results[0]["total_cost_usd"] == 0, reset_results
    after_marker = "AFTER_RESET_" + uuid.uuid4().hex
    after = h.send_and_follow(client, sid, f"Use Bash once to run: printf {after_marker}. "
                              "Reply with only its output.", until="end")
    assert after.error is None and after_marker in after.text and after.tool_names
    h.wait_until_settled(client, sid)
    fresh_key = resume_key()
    assert fresh_key and fresh_key != original_key
    while time.monotonic() < live_test_deadline:
        fresh_native = native()
        if any(r["native_session_id"] == fresh_key and after_marker in r["entry_json"]
               for r in fresh_native):
            break
        time.sleep(.2)
    else:
        raise AssertionError("new native conversation did not reach SessionStore custody")
    preserved = {(r["scope_id"], r["seq"]): r for r in fresh_native}
    assert all(preserved[(r["scope_id"], r["seq"])] == r for r in original_native)
    current = [r for r in fresh_native if r["native_session_id"] == fresh_key]
    assert before_marker not in json.dumps(current), "native /clear retained the discarded context"
    cold_before = h.history(client, sid)
    old_responses = ",".join(h.sql_literal(r.response_id) for r in (before, cleared, after))
    old_output_sql = ("SELECT seq,response_id,part FROM session_output "
                      f"WHERE session_id={h.sql_literal(sid)} AND response_id IN ({old_responses}) ORDER BY seq")
    response_output_before = h.psql_rows(old_output_sql)
    saved_output = h.psql_rows("SELECT seq,response_id,part FROM session_output "
                              f"WHERE session_id={h.sql_literal(sid)} ORDER BY seq")
    _evict_runtime(client, sid)
    h.wait_until_settled(client, sid)
    assert h.get_session(client, sid)["sandbox_id"] == box
    assert resume_key() == fresh_key
    assert h.history(client, sid) == cold_before
    assert h.psql_rows("SELECT seq,response_id,part FROM session_output "
                       f"WHERE session_id={h.sql_literal(sid)} ORDER BY seq") == saved_output

    recalled = h.send_and_follow(client, sid, "Without tools, repeat only the AFTER_RESET marker "
                                 "the Bash command printed.", until="end")
    assert recalled.error is None and after_marker in recalled.text and not recalled.tool_names
    h.wait_until_settled(client, sid)
    assert h.get_session(client, sid)["sandbox_id"] == box and resume_key() == fresh_key
    assert h.psql_rows(old_output_sql) == response_output_before, "reattachment replayed committed responses"
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    logs = subprocess.check_output(["docker", "logs", "--since", started, server],
                                   text=True, stderr=subprocess.STDOUT, timeout=20)
    prefix = f"runtime diagnostic session={sid} type=claude_code.sdk.conversation_reset raw="
    resets = [json.loads(line.split(prefix, 1)[1]) for line in logs.splitlines() if prefix in line]
    assert len(resets) == 1, resets
    assert resets[0]["__sdk_type"] == "ConversationResetMessage"
    assert resets[0]["session_id"] == original_key and resets[0]["new_conversation_id"]
    assert resets[0]["uuid"]
    assert not any("reader failed" in line and sid in line for line in logs.splitlines())
    final = h.history(client, sid)
    assert len({message["id"] for message in final}) == len(final)
    assert len(h.user_messages(final)) == 4
    assert before_marker in json.dumps(final) and after_marker in json.dumps(final)
    print(json.dumps({"phase": "claude-conversation-reset-accepted", "session_id": sid,
                      "reset": resets[0], "native_keys": [original_key, fresh_key],
                      "same_box_after_reattach": True, "reset_result": reset_results[0],
                      "audit_native_rows_retained": len(original_native), "explicit_inputs": 4}))
    h.data(client.delete(f"/api/v1/sessions/{sid}", timeout=60))
    h.data(client.delete(f"/api/v1/agents/{agent}", timeout=60))
    h.data(client.delete(f"/api/v1/admin/runtime-profiles/{environment}"))
