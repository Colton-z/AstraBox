"""One business database serves independent execution targets without lending compute."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile, data, get_session, history, permission_mode, poll_until_ready,
    psql_rows, send_and_follow, sql_literal, transcript_entries, wait_until_settled,
)
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def _native(server: str, code: str, *args: str):
    result = subprocess.run(
        ["docker", "exec", server, "python", "-c", code, *args],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-1200:]
    return json.loads(result.stdout)


_POOL = """
import asyncio,dataclasses,json,os,sys
from redis.asyncio import Redis
from opensandbox.async_redis_pool_store import AsyncRedisPoolStateStore
async def main():
 r=Redis.from_url(os.environ['ASTRABOX_AGENT_PREWARM_REDIS_URL'])
 s=AsyncRedisPoolStateStore(r,key_prefix='astrabox:opensandbox:client-pool')
 try:
  entries=await s.snapshot_idle_entries(sys.argv[1])
  print(json.dumps({'max_idle':await s.get_max_idle(sys.argv[1]),
                   'entries':[dataclasses.asdict(x) for x in entries]},default=str))
 finally:await r.aclose()
asyncio.run(main())
"""
_BOXES = """
import asyncio,json,sys
from opensandbox import SandboxManager
from astrabox.deploy.sandbox_server import lifecycle_base_url
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox._config import sdk_connection_config
async def main():
 m=await SandboxManager.create(connection_config=sdk_connection_config(
     load_astrabox_settings(),lifecycle_base_url_override=lifecycle_base_url()))
 try:
  rows=[]
  for sid in sys.argv[1:]:
   info=await m.get_sandbox_info(sid)
   rows.append({'id':info.id,'expires_at':str(info.expires_at),'status':str(info.status)})
  print(json.dumps(rows))
 finally:await m.close()
asyncio.run(main())
"""


def test_gray_control_and_cold_conversations_do_not_take_production_compute(
    e2e_client: httpx.Client, e2e_auth_headers: dict[str, str], live_test_deadline: float,
) -> None:
    """Native pools, readers, channel deliveries and DBOS retain their persisted owner."""
    shape = json.loads(Path(os.environ["ASTRABOX_E2E_EXECUTION_TARGETS_FILE"]).read_text())
    prod, gray_server = shape["production_container"], shape["gray_container"]
    gray = httpx.Client(base_url=shape["gray_url"], headers=e2e_auth_headers, timeout=30)
    client = e2e_client
    nonce = uuid.uuid4().hex
    sessions: list[tuple[httpx.Client, str]] = []
    received: list[dict] = []
    evidence: dict = {"original_downstream_scene_rechecked": False}
    agent_id = channel_sid = ""
    prod_stopped = passed = False

    def wait(fn, reason):
        while time.monotonic() < live_test_deadline:
            if value := fn():
                return value
            time.sleep(0.5)
        pytest.fail(reason)

    def command(*args):
        subprocess.run(list(args), check=True, capture_output=True, timeout=60)

    def health(http):
        try:
            return http.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def ledger(target="astrabox"):
        rows = psql_rows("SELECT execution_target,runtime_generation,client_pool_epoch,"
                         "client_pool_name FROM agent_runtimes WHERE agent_id="
                         + sql_literal(agent_id) + " AND execution_target=" + sql_literal(target))
        assert len(rows) == 1, rows
        return rows[0]

    def prepared():
        value = data(client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
        return value if value["ready"] and value["prepared_count"] == 2 else None

    def assert_cold():
        value = data(gray.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
        assert (value["execution_target"], value["prewarm_enabled"], value["configured_target"],
                value["target"], value["prepared_count"], value["enabled"]) == ("gray", False, 2, 0, 0, False)
        assert ledger("gray")["client_pool_name"] is None

    def unchanged(snapshot, native_pool):
        assert ledger() == snapshot
        assert _native(prod, _POOL, snapshot["client_pool_name"]) == native_pool

    class Recipient(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    recipient = ThreadingHTTPServer(("0.0.0.0", 0), Recipient)
    threading.Thread(target=recipient.serve_forever, daemon=True).start()
    try:
        assert data(client.get("/api/v1/user/current"))["user_id"] == data(gray.get("/api/v1/user/current"))["user_id"]
        policies = [data(http.get("/api/v1/execution-policy")) for http in (client, gray)]
        assert [(p["execution_target"], p["agent_prewarm_enabled"], p["background_execution_enabled"],
                 p["scheduler_worker_enabled"]) for p in policies] == [
                     ("astrabox", True, True, True), ("gray", False, False, False)]
        assert all(p["runtime_recovery_scope"] == "owned_target" for p in policies)
        environment = "execution-target-" + nonce
        data(client.put(f"/api/v1/admin/environments/{environment}", json={
            "name": environment, "display_name": environment, "enabled": True,
            "endpoint_provider": "litellm", "engine_kind": "claude_code", "idle_action": "terminate",
            "networking": {"type": "limited", "allowed_hosts": [], "allow_mcp_servers": False},
            "provider_access": {}, "runtime_template_name": shape["agent_image"],
            "sandbox_backend": "open_sandbox", "sandbox_tenancy": "conversation",
            "sandbox_permission_level": "advanced", "tracing": {},
        }))
        agent = data(client.post("/api/v1/agents", json={
            "name": "Execution ownership " + nonce, "environment_name": environment,
            "model": current_profile()["model"], "prepared_capacity": 2,
            "system": "Execute the requested Bash command exactly once. Report its output verbatim.",
        }))
        agent_id = agent["agent_id"]
        assert data(gray.get(f"/api/v1/agents/{agent_id}"))["agent_id"] == agent_id
        wait(prepared, "production did not publish two READY units")
        before = ledger()
        pool = _native(prod, _POOL, before["client_pool_name"])
        assert pool["max_idle"] == len(pool["entries"]) == 2, pool
        evidence["initial_pool"] = {"ledger": before, "native": pool}

        sid = data(gray.post(f"/api/v1/agents/{agent_id}/conversations", json={}))["session_id"]
        sessions.append((gray, sid))
        poll_until_ready(gray, sid)
        data(gray.post(f"/api/v1/sessions/{sid}/permission-mode", json={
            "permission_mode": permission_mode("unattended"),
        }))
        reply = send_and_follow(gray, sid,
                                f"Run this Bash command exactly once: printf 'GRAY_{nonce}\\n'; reply with its output.",
                                until="end")
        assert reply.error is None and "GRAY_" + nonce in reply.text, reply
        wait_until_settled(gray, sid)
        assert history(gray, sid) == history(client, sid)
        assert_cold()
        unchanged(before, pool)
        command("docker", "restart", "--time", "10", gray_server)
        wait(lambda: health(gray), "gray process failed to restart")
        assert_cold()
        unchanged(before, pool)
        evidence["gray_cold_session"] = sid

        channel = data(client.post(f"/api/v1/admin/agents/{agent_id}/deployments", json={
            "scene": "channel:generic_json", "prompt_prefix": "",
        }))
        did = channel["deployment_id"]
        assert channel["execution_target"] == "astrabox"
        callback = f"http://{shape['worker_address']}:{recipient.server_port}/reply"

        def trigger(text):
            return data(client.post(f"/api/v1/deployments/{did}/trigger", headers={
                "x-channel-secret": channel["secret"],
            }, json={"text": text, "message_id": str(uuid.uuid4()), "conversation_id": nonce,
                     "reply": {"callback_url": callback}}))["session_id"]

        channel_sid = trigger("Reply HELLO_" + nonce + "; do not use tools.")
        sessions.append((client, channel_sid))
        poll_until_ready(client, channel_sid)
        wait(lambda: any("HELLO_" + nonce in row.get("text", "") for row in received), "channel greeting was not delivered")
        wait_until_settled(client, channel_sid)
        data(client.post(f"/api/v1/sessions/{channel_sid}/permission-mode", json={
            "permission_mode": permission_mode("unattended"),
        }))
        box = get_session(client, channel_sid)["sandbox_id"]
        execute = _sandbox_exec(box)
        marker, release = f"/tmp/started-{nonce}", f"/tmp/release-{nonce}"
        shell = f"touch {marker}; for i in $(seq 1 180); do test -f {release} && break; sleep 1; done; test -f {release} && printf 'PROD_{nonce}\\n'"
        assert trigger("Run this Bash command exactly once, then reply with its output: " + shell) == channel_sid
        wait(lambda: subprocess.run([*execute, "test", "-f", marker], capture_output=True, timeout=10).returncode == 0,
             "production native tool never entered its held command")
        wait(prepared, "production pool did not replenish its two waiting units")
        before, pool = ledger(), _native(prod, _POOL, ledger()["client_pool_name"])
        binding = psql_rows("SELECT sandbox_id,sandbox_generation,execution_target FROM sessions WHERE session_id="
                            + sql_literal(channel_sid))[0]
        native_before = _native(prod, _BOXES, box)
        reader_before = psql_rows("SELECT owner,generation FROM session_readers WHERE session_id=" + sql_literal(channel_sid))
        assert reader_before
        command("docker", "stop", "--time", "10", prod)
        prod_stopped = True
        wait(lambda: psql_rows("SELECT session_id FROM session_readers WHERE session_id=" + sql_literal(channel_sid)
                               + " AND expires_at <= now()"), "old reader lease never expired")
        for _ in range(3):
            history(gray, channel_sid)
            data(gray.get("/api/v1/sessions"))
            time.sleep(2)
        assert psql_rows("SELECT owner,generation FROM session_readers WHERE session_id=" + sql_literal(channel_sid)) == reader_before
        assert psql_rows("SELECT sandbox_id,sandbox_generation,execution_target FROM sessions WHERE session_id="
                         + sql_literal(channel_sid)) == [binding]
        assert _native(gray_server, _BOXES, box) == native_before
        for path, body in [("recover", {}), ("inputs", {"content": "must not run"})]:
            refusal = gray.post(f"/api/v1/sessions/{channel_sid}/{path}", json=body)
            assert refusal.status_code == 409 and refusal.json()["code"] == "EXECUTION_TARGET_MISMATCH", refusal.text
        assert not psql_rows("SELECT input_id FROM session_inputs WHERE session_id=" + sql_literal(channel_sid)
                             + " AND content='must not run'")
        assert psql_rows("SELECT sandbox_id,sandbox_generation,execution_target FROM sessions WHERE session_id="
                         + sql_literal(channel_sid)) == [binding]
        command("docker", "start", prod)
        prod_stopped = False
        wait(lambda: health(client), "production process failed to restart")
        command(*execute, "touch", release)
        wait(lambda: any("PROD_" + nonce in row.get("text", "") for row in received), "restarted owner did not deliver native completion")
        wait_until_settled(client, channel_sid)
        assert len([row for row in received if "PROD_" + nonce in row.get("text", "")]) == 1
        assert get_session(client, channel_sid)["sandbox_id"] == box
        messages = history(gray, channel_sid)
        assert messages == history(client, channel_sid)
        native = transcript_entries(channel_sid)
        tools = [block for row in native for block in (row["entry"].get("message", {}).get("content") or [])
                 if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}]
        assert any(block.get("type") == "tool_use" and block.get("input", {}).get("command") == shell for block in tools), tools
        results = [b for b in tools if b.get("type") == "tool_result" and "PROD_" + nonce in json.dumps(b)]
        assert len(results) == 1, results
        wire = json.dumps(messages)
        assert results[0]["tool_use_id"] in wire and "PROD_" + nonce in wire
        unchanged(before, pool)
        evidence["recovered_channel"] = {"session_id": channel_sid, "box": box, "native_tool_result_count": len(results)}

        schedule = data(client.post(f"/api/v1/admin/agents/{agent_id}/deployments", json={
            "scene": "schedule", "name": "Owner schedule " + nonce,
            "prompt_prefix": "Reply SCHEDULE_" + nonce + "; do not use tools.",
            "schedule": {"cron": "0 0 1 1 *", "timezone": "UTC"},
        }))
        route = f"/api/v1/admin/agents/{agent_id}/deployments/{schedule['deployment_id']}"
        data(gray.put(route, json={"name": "Edited from gray " + nonce}))
        run = data(gray.post(route + "/runs", json={}))
        run_id = run["run_id"]
        rows = wait(lambda: [r for r in psql_rows("SELECT workflow_uuid,application_name,queue_name,executor_id,status "
                    "FROM dbos.workflow_status WHERE workflow_uuid=" + sql_literal(run_id)) if r["status"] == "SUCCESS"],
                    "native DBOS owner did not dispatch the schedule requested through gray")
        assert rows[0]["application_name"] == "astrabox" and rows[0]["queue_name"] == "astrabox-deployment-runs", rows
        assert rows[0]["executor_id"].startswith("astrabox-"), rows
        scheduled = wait(lambda: [r for r in data(gray.get(route + "/runs"))
                                  if r["run_id"] == run_id and r.get("session_id")],
                         "scheduled conversation was not readable through gray")[0]
        scheduled_sid = scheduled["session_id"]
        sessions.append((client, scheduled_sid))
        wait_until_settled(client, scheduled_sid)
        wait(lambda: "SCHEDULE_" + nonce in json.dumps(history(gray, scheduled_sid)),
             "scheduled native reply was missing from shared history")
        assert psql_rows("SELECT execution_target FROM sessions WHERE session_id="
                         + sql_literal(scheduled_sid)) == [{"execution_target": "astrabox"}]
        evidence["native_dbos_runs"] = rows
        current = data(gray.get(f"/api/v1/agents/{agent_id}"))
        data(gray.put(f"/api/v1/agents/{agent_id}", json={
            "name": current["name"], "model": current["model"],
            "environment_name": current["environment_name"], "version": current["version"],
            "system": current.get("system", "") + " Keep replies concise.",
        }))
        wait(lambda: prepared() and ledger()["client_pool_epoch"] != before["client_pool_epoch"], "shared Agent edit did not update the owner's pool")
        assert data(client.get(f"/api/v1/agents/{agent_id}"))["version"] == data(gray.get(f"/api/v1/agents/{agent_id}"))["version"]
        assert_cold()
        assert not psql_rows("SELECT session_id FROM sessions WHERE execution_target NOT IN ('astrabox','gray')")
        assert not psql_rows("SELECT workflow_uuid FROM dbos.workflow_status WHERE application_name='gray'")
        evidence["final_ledgers"] = [ledger(), ledger("gray")]
        passed = True
    finally:
        if prod_stopped:
            command("docker", "start", prod)
        Path(shape["evidence_file"]).write_text(json.dumps({**evidence, "passed": passed}, indent=2) + "\n")
        recipient.shutdown()
        if passed:
            for http, sid in reversed(sessions):
                data(http.delete(f"/api/v1/sessions/{sid}"))
            data(client.delete(f"/api/v1/agents/{agent_id}"))
        gray.close()
