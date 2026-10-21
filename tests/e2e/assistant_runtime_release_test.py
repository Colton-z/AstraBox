"""Released Assistant compute must not keep an unused Environment in use."""

import json
import os
import re
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_kubernetes_runtime_image, data, durable_mounts_of_sandbox, history, kube_access, poll_until_ready,
    psql_rows, send_and_follow, sql_literal, wait_until_settled,
)
from tests.e2e.test_conversation_end import _wake_assistant
from tests.e2e.test_durable_workspace import _assistant_snapshot
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live, pytest.mark.deployment_shape]


def test_released_assistant_box_stops_holding_its_environment(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    environment = os.environ["ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"]
    assert environment.startswith("assistant-release-"), "use this case's isolated Environment"
    model = os.environ["ASTRABOX_E2E_ASSISTANT_MODEL"]
    aid = data(client.post("/api/v1/assistants", json={
        "display_name": f"Runtime release {uuid.uuid4().hex[:8]}",
        "environment_name": environment, "model_config_override": {"model_name": model},
    }))["assistant_id"]
    workspace = _wake_assistant(client, aid, timeout=max(.1, live_test_deadline - time.monotonic()))
    first_box = workspace["current_sandbox_id"]
    sid = data(client.post(f"/api/v1/assistants/{aid}/conversations", json={}))['session_id']
    poll_until_ready(client, sid)
    command = _sandbox_exec(first_box)
    first_pod = command[command.index("exec") + 1]
    kubeconfig, namespace = kube_access()
    assert_kubernetes_runtime_image(
        sandbox_id=first_box, expected_image=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE"],
        expected_digest=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE_DIGEST"],
        kubeconfig=kubeconfig, namespace=namespace,
    )
    assert not durable_mounts_of_sandbox(
        pod=first_pod, kubeconfig=kubeconfig, namespace=namespace,
    ), "the original conversation must resume from database custody, without a workspace volume"
    owned = f"(assistant_id={sql_literal(aid)} OR owner_id={sql_literal(aid)})"

    def references() -> list[dict]:
        return psql_rows(
            "SELECT session_id,hidden,state,sandbox_id,engine_session_key FROM sessions "
            f"WHERE workspace_kind='assistant' AND {owned} ORDER BY session_id"
        )

    before_bindings = references()
    assert any(row["hidden"] for row in before_bindings), "include the native workspace's bootstrap Session"
    assert all(row["sandbox_id"] == first_box for row in before_bindings), before_bindings
    refused = client.delete(f"/api/v1/admin/environments/{environment}")
    assert refused.status_code == 409 and refused.json()["code"] == "ENVIRONMENT_IN_USE"
    assert any(holder["target_id"] == aid for holder in refused.json()["data"]["holders"])

    prompt = (
        "Use terminal once to run exactly: python3 -c \"import secrets; "
        "print('RELEASE_PROOF_'+secrets.token_hex(8))\". "
        "Wait for the command, then reply with only its printed token."
    )
    first = send_and_follow(client, sid, prompt, until="end")
    assert first.error is None and first.tool_names, first.response
    wait_until_settled(client, sid)
    before = _assistant_snapshot(sid, aid, prompt=prompt, deadline=live_test_deadline)
    tool_rows = [row for row in before["messages"] if row["role"] == "tool"]
    tokens = re.findall(r"RELEASE_PROOF_[0-9a-f]{16}", json.dumps(tool_rows))
    assert tokens and len(set(tokens)) == 1, tool_rows
    token = tokens[0]
    assert token in first.text
    original_history = history(client, sid)
    for row in tool_rows:
        parts = [part for message in original_history for part in message["parts"]
                 if part.get("toolCallId") == row["tool_call_id"]]
        assert len(parts) == 1 and token in json.dumps(parts[0]), parts

    released = data(client.post(f"/api/v1/assistants/{aid}/workspace/hibernate", timeout=60))
    assert released["released"] and released["previous_sandbox_id"] == first_box, released
    subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, "wait",
                    "--for=delete", f"pod/{first_pod}", "--timeout=15s"], check=True, timeout=20)
    assert all(row["sandbox_id"] is None for row in references()), references()
    assert history(client, sid) == original_history
    still_bound = client.delete(f"/api/v1/admin/environments/{environment}")
    assert still_bound.status_code == 409 and still_bound.json()["code"] == "ENVIRONMENT_IN_USE"

    ready = _wake_assistant(client, aid, timeout=max(.1, live_test_deadline - time.monotonic()))
    second_box = ready["current_sandbox_id"]
    assert second_box != first_box
    assert_kubernetes_runtime_image(
        sandbox_id=second_box, expected_image=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE"],
        expected_digest=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE_DIGEST"],
        kubeconfig=kubeconfig, namespace=namespace,
    )
    recall = "Without tools, repeat only the random RELEASE_PROOF token the terminal printed earlier."
    second = send_and_follow(client, sid, recall, until="end")
    assert second.error is None and token in second.text and not second.tool_names, second.response
    wait_until_settled(client, sid)
    restored = _assistant_snapshot(sid, aid, prompt=recall, deadline=live_test_deadline)
    assert restored["native_session_id"] == before["native_session_id"]
    assert restored["messages"][:len(before["messages"])] == before["messages"]
    assert history(client, sid)[:len(original_history)] == original_history
    command = _sandbox_exec(second_box)
    second_pod = command[command.index("exec") + 1]
    assert not durable_mounts_of_sandbox(
        pod=second_pod, kubeconfig=kubeconfig, namespace=namespace,
    )

    audit_sql = (
        "SELECT * FROM (SELECT 'input' AS kind,to_jsonb(i) AS record FROM session_inputs i "
        f"WHERE i.session_id={sql_literal(sid)} UNION ALL "
        "SELECT 'response' AS kind,to_jsonb(r) AS record FROM session_responses r "
        f"WHERE r.session_id={sql_literal(sid)}) audit ORDER BY kind,record::text"
    )
    audit_before = psql_rows(audit_sql)
    assert sum(row["kind"] == "input" for row in audit_before) == 2
    assert sum(row["kind"] == "response" for row in audit_before) >= 2
    data(client.delete(f"/api/v1/assistants/{aid}", timeout=60))
    subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, "wait",
                    "--for=delete", f"pod/{second_pod}", "--timeout=15s"], check=True, timeout=20)
    after_bindings = references()
    assert {row["session_id"] for row in before_bindings} <= {row["session_id"] for row in after_bindings}
    assert all(row["sandbox_id"] is None for row in after_bindings), after_bindings
    assert next(row for row in after_bindings if row["session_id"] == sid)["state"] == "DELETED"
    assert psql_rows(audit_sql) == audit_before, "audit inputs and complete response records must survive"
    heads = psql_rows(
        "SELECT archive,sha256,size,chunk_count FROM runtime_state_heads "
        f"WHERE subject_kind='assistant' AND subject_id={sql_literal(aid)}"
    )
    assert any(head["archive"] == "native_session_store" and head["size"] > 0
               and head["chunk_count"] > 0 for head in heads), heads
    assert data(client.delete(f"/api/v1/admin/environments/{environment}")) == {
        "name": environment, "deleted": True,
    }
    assert all(row["name"] != environment for row in data(client.get('/api/v1/admin/environments')))
    print(json.dumps({"phase": "assistant-runtime-release-accepted", "assistant_id": aid,
                      "session_id": sid, "boxes": [first_box, second_box],
                      "retained_session_ids": [row["session_id"] for row in after_bindings],
                      "native_message_rows_preserved": len(before["messages"]),
                      "environment_deleted": environment, "audit_records_preserved": len(audit_before)}))
