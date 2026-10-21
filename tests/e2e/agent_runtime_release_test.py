"""Deleted Agent conversations release compute references and retain audit history."""

import json
import os
import re
import subprocess
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_kubernetes_runtime_image, data, durable_mounts_of_sandbox, history,
    kube_access, poll_until_ready, psql_rows, send_and_follow, sql_literal, wait_until_settled,
)
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def test_deleted_agent_conversation_stops_holding_its_environment(e2e_client: httpx.Client) -> None:
    client = e2e_client
    environment = os.environ["ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"]
    assert environment.startswith("agent-release-"), "use this case's isolated Environment"
    environments = data(client.get("/api/v1/admin/environments"))
    assert [row["engine_kind"] for row in environments if row["name"] == environment] == ["claude_code"]
    agent = data(client.post("/api/v1/agents", json={
        "name": f"Agent release {uuid.uuid4().hex[:8]}", "environment_name": environment,
        "model": os.environ["ASTRABOX_E2E_ASSISTANT_MODEL"], "prepared_capacity": 0,
    }))["agent_id"]
    sid = data(client.post(f"/api/v1/agents/{agent}/conversations", json={}))['session_id']
    poll_until_ready(client, sid)
    row = psql_rows(
        "SELECT sandbox_id,engine_session_key FROM sessions "
        f"WHERE session_id={sql_literal(sid)}"
    )[0]
    box = row["sandbox_id"]
    command = _sandbox_exec(box)
    pod = command[command.index("exec") + 1]
    kubeconfig, namespace = kube_access()
    kubectl = ["kubectl", "--kubeconfig", kubeconfig, "-n", namespace]
    pod_record = json.loads(subprocess.check_output(
        [*kubectl, "get", "pod", pod, "-o", "json"], text=True, timeout=20,
    ))
    deletion_timeout = pod_record["spec"]["terminationGracePeriodSeconds"] + 15
    assert_kubernetes_runtime_image(
        sandbox_id=box, expected_image=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE"],
        expected_digest=os.environ["ASTRABOX_E2E_ASSISTANT_IMAGE_DIGEST"],
        kubeconfig=kubeconfig, namespace=namespace,
    )
    assert not durable_mounts_of_sandbox(pod=pod, kubeconfig=kubeconfig, namespace=namespace)
    refused = client.delete(f"/api/v1/admin/environments/{environment}")
    assert refused.status_code == 409 and refused.json()["code"] == "ENVIRONMENT_IN_USE"

    data(client.post(f"/api/v1/sessions/{sid}/permission-mode",
                     json={"permission_mode": "bypassPermissions"}))
    prompt = (
        "Use Bash once to run exactly: python3 -c \"import secrets; "
        "print('AGENT_RELEASE_'+secrets.token_hex(8))\". Wait for the command, "
        "then reply with only its printed token."
    )
    reply = send_and_follow(client, sid, prompt, until="end")
    assert reply.error is None and reply.tool_names, reply.response
    wait_until_settled(client, sid)
    row = psql_rows(
        "SELECT sandbox_id,engine_session_key FROM sessions "
        f"WHERE session_id={sql_literal(sid)}"
    )[0]
    key = row["engine_session_key"]
    native_sql = (
        "SELECT s.scope_id,s.native_session_id,e.seq,e.entry_json FROM transcript_entries e "
        "JOIN transcript_scopes s ON s.scope_id=e.scope_id "
        f"WHERE s.platform_session_id={sql_literal(sid)} ORDER BY s.scope_id,e.seq"
    )
    native_before = psql_rows(native_sql)
    assert native_before and any(r["native_session_id"] == key for r in native_before)
    blocks = [block for r in native_before for block in
              (json.loads(r["entry_json"]).get("message") or {}).get("content", [])
              if isinstance(block, dict)]
    tools = [block for block in blocks if block.get("type") == "tool_result"]
    tokens = re.findall(r"AGENT_RELEASE_[0-9a-f]{16}", json.dumps(tools))
    assert tokens and len(set(tokens)) == 1 and tokens[0] in reply.text, tools
    visible = history(client, sid)
    for tool in tools:
        parts = [part for message in visible for part in message["parts"]
                 if part.get("toolCallId") == tool["tool_use_id"]]
        assert len(parts) == 1 and tokens[0] in json.dumps(parts[0]), parts
    audit_sql = " UNION ALL ".join(
        f"SELECT '{kind}' AS kind,to_jsonb(r) AS record FROM {table} r "
        f"WHERE r.session_id={sql_literal(sid)}"
        for kind, table in [("input", "session_inputs"), ("response", "session_responses"),
                            ("output", "session_output")]
    )
    audit_sql = f"SELECT * FROM ({audit_sql}) audit ORDER BY kind,record::text"
    audit_before = psql_rows(audit_sql)
    assert {record["kind"] for record in audit_before} == {"input", "response", "output"}

    assert data(client.delete(f"/api/v1/sessions/{sid}", timeout=60))["deleted"]
    deleted = psql_rows(
        "SELECT state,sandbox_id,engine_session_key,undestroyed_sandbox_ids FROM sessions "
        f"WHERE session_id={sql_literal(sid)}"
    )[0]
    assert deleted == {"state": "DELETED", "sandbox_id": None, "engine_session_key": key,
                       "undestroyed_sandbox_ids": []}, deleted
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert client.get(f"/api/v1/sessions/{sid}/messages").status_code == 404
    assert psql_rows(audit_sql) == audit_before
    data(client.delete(f"/api/v1/agents/{agent}", timeout=60))
    subprocess.run([*kubectl, "wait", "--for=delete", f"pod/{pod}",
                    f"--timeout={deletion_timeout}s"], check=True, timeout=deletion_timeout + 5)
    native_after = {(r["scope_id"], r["seq"]): r for r in psql_rows(native_sql)}
    assert all(native_after[(r["scope_id"], r["seq"])] == r for r in native_before)
    assert data(client.delete(f"/api/v1/admin/environments/{environment}")) == {
        "name": environment, "deleted": True,
    }
    assert psql_rows(audit_sql) == audit_before
    print(json.dumps({"phase": "agent-runtime-release-accepted", "agent_id": agent,
                      "session_id": sid, "sandbox_id": box, "environment_deleted": environment,
                      "native_messages_retained": len(native_before),
                      "pod_termination_grace_seconds": deletion_timeout - 15,
                      "audit_records_retained": len(audit_before)}))
