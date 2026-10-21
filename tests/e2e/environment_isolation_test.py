"""Independent Environments retain access, native history and execution ownership."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.e2e import _sandbox_helpers as h
from tests.e2e.test_prepared_runtime_activation import _sandbox_exec

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]

_ROWS = """
import asyncio,asyncpg,json,sys
from astrabox.config.settings import get_settings
async def main():
 c=await asyncpg.connect(get_settings().resolved_db_url.replace('postgresql+asyncpg://','postgresql://',1))
 try:print(json.dumps([dict(r) for r in await c.fetch(sys.argv[1])],default=str))
 finally:await c.close()
asyncio.run(main())
"""
_FOREIGN_DATABASE = """
import asyncio,asyncpg,json,sys
from sqlalchemy.engine import make_url
from astrabox.config.settings import get_settings
async def main():
 url=make_url(get_settings().resolved_db_url).set(drivername='postgresql',database=sys.argv[1])
 try:c=await asyncpg.connect(url.render_as_string(hide_password=False))
 except asyncpg.PostgresError as exc:
  print(json.dumps({'connected':False,'sqlstate':exc.sqlstate}));return
 await c.close();print(json.dumps({'connected':True}))
asyncio.run(main())
"""
_WRONG_ENVIRONMENT = """
import asyncio,json,sys
from astrabox.config.settings import get_settings
from astrabox.providers.relational_postgres import PostgresPersistenceProvider
async def main():
 p=PostgresPersistenceProvider(database_url=get_settings().resolved_db_url,environment_name=sys.argv[1])
 try:await p.initialize_schema()
 except RuntimeError as exc:
  print(json.dumps({'refused':True,'reason':str(exc)}));return
 print(json.dumps({'refused':False}))
asyncio.run(main())
"""


def _native(server: str, code: str, *args: str):
    result = subprocess.run(
        ["docker", "exec", server, "python", "-c", code, *args],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-1200:]
    return json.loads(result.stdout)


def test_environments_isolate_data_access_and_native_execution(
    e2e_client: httpx.Client, e2e_auth_headers: dict[str, str], live_test_deadline: float,
) -> None:
    shape = json.loads(Path(os.environ["ASTRABOX_E2E_ENVIRONMENTS_FILE"]).read_text())
    prod = e2e_client
    gray = httpx.Client(base_url=shape["gray_url"], headers=e2e_auth_headers, timeout=30)
    headers = {"Authorization": "Bearer " + Path(shape["member_token_file"]).read_text().strip()}
    member = httpx.Client(base_url=str(prod.base_url), headers=headers, timeout=30)
    gray_member = httpx.Client(base_url=shape["gray_url"], headers=headers, timeout=30)
    prod_server, gray_server = shape["production_container"], shape["gray_container"]
    nonce = uuid.uuid4().hex
    profile = "isolation-" + nonce
    agents: list[tuple[httpx.Client, str]] = []
    sessions: list[tuple[httpx.Client, str]] = []
    evidence: dict = {"passed": False, "original_downstream_scene_rechecked": False}
    received: list[dict] = []

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

    def wait(predicate, reason):
        while time.monotonic() < live_test_deadline:
            if value := predicate():
                return value
            time.sleep(.5)
        pytest.fail(reason)

    def rows(server, sql):
        return _native(server, _ROWS, sql)

    def refused(response, status, code=None):
        assert response.status_code == status, response.text[:500]
        if code:
            assert response.json()["code"] == code, response.text[:500]

    def native(sid):
        return rows(prod_server, "SELECT s.scope_id,s.native_session_id,e.seq,e.entry_json "
                    "FROM transcript_entries e JOIN transcript_scopes s ON s.scope_id=e.scope_id "
                    f"WHERE s.platform_session_id={h.sql_literal(sid)} ORDER BY s.scope_id,e.seq")

    def healthy():
        try:
            return prod.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    try:
        evidence["phase"] = "database-and-identity-boundaries"
        environments = [h.data(http.get("/api/v1/environments/current")) for http in (prod, gray)]
        prod_name, gray_name = [row["name"] for row in environments]
        assert prod_name != gray_name
        assert environments[0]["runtime_policy"]["background_execution_enabled"]
        assert not environments[1]["runtime_policy"]["background_execution_enabled"]
        assert not environments[1]["runtime_policy"]["agent_prewarm_enabled"]
        databases = [rows(server, "SELECT current_database() AS database,current_user AS role")[0]
                     for server in (prod_server, gray_server)]
        assert databases[0]["database"] != databases[1]["database"]
        assert databases[0]["role"] != databases[1]["role"]
        binding = rows(prod_server, "SELECT name FROM environments")
        revision = rows(prod_server, "SELECT version_num FROM alembic_version")
        wrong = _native(prod_server, _WRONG_ENVIRONMENT, gray_name)
        assert wrong["refused"] and "database belongs to Environment" in wrong["reason"]
        assert rows(prod_server, "SELECT name FROM environments") == binding
        assert rows(prod_server, "SELECT version_num FROM alembic_version") == revision
        for server, other in ((prod_server, databases[1]), (gray_server, databases[0])):
            assert _native(server, _FOREIGN_DATABASE, other["database"]) == {
                "connected": False, "sqlstate": "42501",
            }
        admin = h.data(prod.get("/api/v1/user/current"))
        actor = h.data(member.get("/api/v1/user/current"))
        assert admin["is_admin"] and not actor["is_admin"]
        assert actor["user_id"] == h.data(gray_member.get("/api/v1/user/current"))["user_id"]
        original_members = {}
        for http, name in ((prod, prod_name), (gray, gray_name)):
            row = h.data(http.get(f"/api/v1/admin/environments/{name}"))
            original_members[name] = row["member_user_ids"]
            allowed = [admin["user_id"], actor["user_id"]] if http is prod else [admin["user_id"]]
            h.data(http.put(f"/api/v1/admin/environments/{name}", json={
                "display_name": row["display_name"], "description": row.get("description"),
                "member_user_ids": allowed, "version": row["version"],
            }))
        refused(gray_member.get("/api/v1/agents"), 403, "ENVIRONMENT_ACCESS_DENIED")
        refused(prod.put(f"/api/v1/admin/environments/{gray_name}", json={
            "display_name": "Foreign", "member_user_ids": [], "version": 1,
        }), 404, "ENVIRONMENT_NOT_FOUND")
        evidence["databases"] = databases

        evidence["phase"] = "independent-configurations-and-conversations"
        for http in (prod, gray):
            h.data(http.put(f"/api/v1/admin/runtime-profiles/{profile}", json={
                "name": profile, "display_name": profile, "enabled": True,
                "engine_kind": "claude_code", "endpoint_provider": "litellm",
                "runtime_template_name": shape["agent_image"], "sandbox_backend": "open_sandbox",
                "sandbox_tenancy": "conversation", "sandbox_permission_level": "advanced",
                "idle_action": "terminate", "provider_access": {}, "tracing": {},
                "networking": {"type": "limited", "allowed_hosts": [], "allow_mcp_servers": False},
            }))
            agent = h.data(http.post("/api/v1/agents", json={
                "name": "Environment isolation " + nonce, "runtime_profile_name": profile,
                "model": h.current_profile()["model"], "prepared_capacity": 0,
            }))
            agents.append((http, agent["agent_id"]))
            h.data(http.put(f"/api/v1/agents/{agent['agent_id']}/access", json={
                "visibility": "allowlist", "admins": [], "allowed_user_ids": [actor["user_id"]],
            }))
        prod_agent, gray_agent = agents[0][1], agents[1][1]
        refused(gray.get(f"/api/v1/agents/{prod_agent}"), 404)
        refused(prod.get(f"/api/v1/agents/{gray_agent}"), 404)
        prod_config = h.data(prod.get(f"/api/v1/agents/{prod_agent}"))
        gray_config = h.data(gray.get(f"/api/v1/agents/{gray_agent}"))
        h.data(gray.put(f"/api/v1/agents/{gray_agent}", json={
            "name": "Gray only " + nonce, "model": gray_config["model"],
            "runtime_profile_name": profile, "version": gray_config["version"],
        }))
        assert h.data(prod.get(f"/api/v1/agents/{prod_agent}")) == prod_config
        sid = h.data(member.post(f"/api/v1/agents/{prod_agent}/conversations", json={}))["session_id"]
        sessions.append((member, sid))
        h.poll_until_ready(
            member, sid, expected_image=shape["agent_image"], sandbox_admin_client=prod,
        )
        gray_sid = h.data(gray.post(f"/api/v1/agents/{gray_agent}/conversations", json={}))["session_id"]
        sessions.append((gray, gray_sid))
        h.poll_until_ready(gray, gray_sid, expected_image=shape["agent_image"])
        for http, foreign_sid in ((prod, gray_sid), (gray, sid)):
            refused(http.get(f"/api/v1/sessions/{foreign_sid}/messages"), 404)
            refused(http.post(f"/api/v1/sessions/{foreign_sid}/interrupt"), 404)
            refused(http.post(f"/api/v1/sessions/{foreign_sid}/recover", json={}), 404)
            refused(http.delete(f"/api/v1/sessions/{foreign_sid}"), 404)
        command = "python3 -c \"import secrets; print('ENV_'+secrets.token_hex(12))\""
        h.data(member.post(f"/api/v1/sessions/{sid}/permission-mode", json={"permission_mode": "bypassPermissions"}))
        reply = h.send_and_follow(member, sid, "Use Bash once to run exactly: " + command
                                  + ". Reply only with its output.", until="end")
        assert reply.error is None and "Bash" in reply.tool_names
        h.wait_until_settled(member, sid)
        wait(lambda: any('tool_result' in str(r["entry_json"]) for r in native(sid)),
             "native Bash result was not preserved in SessionStore")
        blocks = []
        for row in native(sid):
            entry = json.loads(row["entry_json"]) if isinstance(row["entry_json"], str) else row["entry_json"]
            content = entry.get("message", {}).get("content")
            if isinstance(content, list):
                blocks.extend(block for block in content if isinstance(block, dict))
        calls = [b for b in blocks if b.get("type") == "tool_use" and b.get("input", {}).get("command") == command]
        assert len(calls) == 1, calls
        results = [b for b in blocks if b.get("type") == "tool_result" and b.get("tool_use_id") == calls[0]["id"]]
        assert len(results) == 1, results
        tokens = re.findall(r"ENV_[0-9a-f]{24}", json.dumps(results))
        assert len(set(tokens)) == 1, results
        token = tokens[0]
        visible = h.history(member, sid)
        assert token in reply.text and token in json.dumps(visible) and calls[0]["id"] in json.dumps(visible)
        evidence["native_result"] = {"tool_use_id": calls[0]["id"], "token": token}

        evidence["phase"] = "restart-and-volume-free-native-resume"
        before = native(sid)
        box = h.get_session(member, sid)["sandbox_id"]
        subprocess.run(["docker", "restart", "--time", "10", prod_server], check=True, capture_output=True, timeout=60)
        wait(healthy, "production Environment failed to restart")
        assert h.history(member, sid) == visible
        assert h.get_session(member, sid)["sandbox_id"] == box
        assert native(sid) == before
        h.data(gray.post("/api/v1/admin/sandbox-idle-sweep"))
        _sandbox_exec(box)
        h.destroy_box(box)
        recalled = h.send_and_follow(member, sid, "What token did the earlier Bash command print? "
                                     "Reply only with that token from history; do not use tools.", until="end")
        assert recalled.error is None and token in recalled.text
        h.wait_until_settled(member, sid)
        replacement = h.get_session(member, sid)["sandbox_id"]
        assert replacement and replacement != box
        execute = _sandbox_exec(replacement)
        kubeconfig, namespace = h.kube_access()
        assert not h.durable_mounts_of_sandbox(
            pod=execute[execute.index("exec") + 1], kubeconfig=kubeconfig, namespace=namespace,
        )
        after = {(r["scope_id"], r["seq"]): r for r in native(sid)}
        assert all(after[(r["scope_id"], r["seq"])] == r for r in before)
        assert calls[0]["id"] in json.dumps(h.history(member, sid))
        refused(gray.get(f"/api/v1/sessions/{sid}/messages"), 404)
        assert not rows(gray_server, f"SELECT session_id FROM sessions WHERE session_id={h.sql_literal(sid)}")
        evidence["resume"] = {"old_box": box, "new_box": replacement, "native_rows_preserved": len(before)}

        evidence["phase"] = "native-scheduler-and-membership-revocation"
        scheduled_token = "SCHEDULE_" + nonce
        schedule = h.data(prod.post(f"/api/v1/admin/agents/{prod_agent}/deployments", json={
            "scene": "schedule", "name": "Environment schedule " + nonce,
            "prompt_prefix": "Reply " + scheduled_token + "; do not use tools.",
            "schedule": {"cron": "0 0 1 1 *", "timezone": "UTC"},
        }))
        route = f"/api/v1/admin/agents/{prod_agent}/deployments/{schedule['deployment_id']}"
        refused(gray.post(route + "/runs", json={}), 404)
        run_id = h.data(prod.post(route + "/runs", json={}))["run_id"]
        run = wait(lambda: [r for r in h.data(prod.get(route + "/runs"))
                           if r["run_id"] == run_id and r.get("session_id")],
                   "local scheduler did not publish its Session")[0]
        sessions.append((prod, run["session_id"]))
        wait(lambda: any(scheduled_token in h.message_text(message)
                         for message in h.assistant_messages(h.history(prod, run["session_id"]))),
             "local scheduled reply was not visible")
        h.wait_until_settled(prod, run["session_id"])
        dbos = wait(lambda: [r for r in rows(prod_server, "SELECT application_name,status FROM dbos.workflow_status WHERE workflow_uuid="
                                           + h.sql_literal(run_id)) if r["status"] == "SUCCESS"],
                    "native DBOS dispatch did not complete")
        assert dbos[0]["application_name"] == prod_name
        assert not rows(gray_server, "SELECT workflow_uuid FROM dbos.workflow_status WHERE workflow_uuid=" + h.sql_literal(run_id))
        channel_token = "CHANNEL_" + nonce
        channel = h.data(prod.post(f"/api/v1/admin/agents/{prod_agent}/deployments", json={
            "scene": "channel:generic_json", "prompt_prefix": "", "execution_mode": "account",
            "execution_user_id": actor["user_id"],
        }))

        def trigger(text, conversation):
            return h.data(prod.post(f"/api/v1/deployments/{channel['deployment_id']}/trigger", headers={
                "x-channel-secret": channel["secret"],
            }, json={"text": text, "message_id": str(uuid.uuid4()), "conversation_id": conversation,
                     "reply": {"callback_url": f"http://{shape['worker_address']}:{recipient.server_port}/reply"}}))

        channel_sid = trigger("Reply " + channel_token + "; do not use tools.", nonce)["session_id"]
        sessions.append((member, channel_sid))
        wait(lambda: any(channel_token in row.get("text", "") for row in received),
             "authorized execution account did not receive its channel reply")
        h.wait_until_settled(member, channel_sid)
        membership = h.data(prod.get(f"/api/v1/admin/environments/{prod_name}"))
        h.data(prod.put(f"/api/v1/admin/environments/{prod_name}", json={
            "display_name": membership["display_name"], "member_user_ids": [admin["user_id"]],
            "description": membership.get("description"), "version": membership["version"],
        }))
        count_sql = f"SELECT count(*) AS count FROM session_inputs WHERE session_id={h.sql_literal(sid)}"
        input_count = rows(prod_server, count_sql)
        refused(member.get(f"/api/v1/sessions/{sid}/messages"), 403, "ENVIRONMENT_ACCESS_DENIED")
        refused(member.post(f"/api/v1/sessions/{sid}/inputs", json={
            "content": "Unauthorized input", "client_message_id": str(uuid.uuid4()),
        }), 403, "ENVIRONMENT_ACCESS_DENIED")
        assert rows(prod_server, count_sql) == input_count
        session_count = rows(prod_server, "SELECT count(*) AS count FROM sessions")
        reply_count = len(received)
        trigger("Reply UNAUTHORIZED; do not use tools.", nonce + "-denied")
        wait(lambda: any("access to this Environment" in row.get("text", "") for row in received[reply_count:]),
             "revoked channel execution account was not refused visibly")
        assert rows(prod_server, "SELECT count(*) AS count FROM sessions") == session_count
        evidence["scheduler"] = {"run_id": run_id, "native": dbos}
        evidence["passed"] = True
    finally:
        evidence["resources"] = {"agents": [aid for _, aid in agents], "sessions": [sid for _, sid in sessions]}
        Path(shape["evidence_file"]).write_text(json.dumps(evidence, indent=2) + "\n")
        if evidence["passed"]:
            row = h.data(prod.get(f"/api/v1/admin/environments/{prod_name}"))
            h.data(prod.put(f"/api/v1/admin/environments/{prod_name}", json={
                "display_name": row["display_name"], "description": row.get("description"),
                "member_user_ids": [admin["user_id"], actor["user_id"]], "version": row["version"],
            }))
            for http, sid in reversed(sessions):
                h.data(http.delete(f"/api/v1/sessions/{sid}"))
            for http, aid in reversed(agents):
                h.data(http.delete(f"/api/v1/agents/{aid}"))
            for http, name in ((prod, prod_name), (gray, gray_name)):
                row = h.data(http.get(f"/api/v1/admin/environments/{name}"))
                h.data(http.put(f"/api/v1/admin/environments/{name}", json={
                    "display_name": row["display_name"], "description": row.get("description"),
                    "member_user_ids": original_members[name], "version": row["version"],
                }))
        for http in (member, gray_member, gray):
            http.close()
        recipient.shutdown()
        recipient.server_close()
