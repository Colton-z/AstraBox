"""Initialized inventory binds persisted Session identities without core Vault."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile, data, get_admin_session_detail, history,
    poll_until_ready, psql_rows, release_agent, release_session,
    running_sandbox_image, send_and_follow, sql_literal, transcript_entries,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.conftest import _wait_health
from tests.e2e.runtime_preparation_test import PREPARATION_IMAGE_ENV
from tests.e2e.test_prepared_runtime_activation import (
    _prepared_identity, _run_json, _sandbox_exec, _supplier_processes,
    _wait_prepared, _wait_prepared_count,
)
from tests.e2e.test_reattach import _restart_server_container

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape, pytest.mark.backend_restart]


def _server_json(program: str, *arguments: str):
    return _run_json(["docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE), "python", "-c", program, *arguments])


def _user_client(admin: httpx.Client, user: str) -> httpx.Client:
    token = subprocess.check_output([
        "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE), "python", "-c",
        "import os,sys; from astrabox.providers.identity_oidc import mint_session_token; "
        "print(mint_session_token(user_id=sys.argv[1],email=None,display_name=None,roles=[],"
        "casdoor_organization=os.environ.get('ASTRABOX_CASDOOR_ORGANIZATION','')))", user,
    ], text=True).strip()
    client = httpx.Client(base_url=str(admin.base_url), cookies={"astrabox_session": token}, timeout=30)
    who = data(client.get("/api/v1/user/current"))
    assert who["user_id"] == user and who["is_admin"] is False
    return client


def _gateway(session_id: str = "") -> dict:
    # LiteLLM's actual key metadata and request logs are independent of the
    # preparation receipts. Return only hashes and usage, never credentials.
    return _server_json("""
import hashlib,json,os,sys,httpx
from astrabox.providers.model import LiteLLMModelEndpointProvider
from astrabox.seams.model import ModelRequestContext,model_endpoint_for_name
headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}
base=LiteLLMModelEndpointProvider.server_side_base_url().rstrip('/')
with httpx.Client(base_url=base,headers=headers,timeout=15) as c:
 sid=sys.argv[1]
 digest='0'*64
 result={'available':True}
 if sid:
  key=model_endpoint_for_name('litellm').session_credential(context=ModelRequestContext(conversation_id=sid))
  digest=hashlib.sha256(key.encode()).hexdigest()
  info=c.get('/key/info',params={'key':digest});info.raise_for_status()
  meta=info.json()['info']['metadata']
  result.update(key_digest=digest,session_id=meta['astrabox_session_id'],principal_id=meta['astrabox_user_id'])
 response=c.get('/spend/logs',params={'api_key':digest});response.raise_for_status()
 rows=response.json();assert isinstance(rows,list)
 result['requests']=[{'key_digest':r['api_key'],'request_id':r['request_id'],'total_tokens':r['total_tokens']}
                     for r in rows if r['api_key']==digest]
 print(json.dumps(result))
""", session_id)


def _receipt(execute: list[str]) -> dict:
    return _run_json([*execute, "python3", "-c", """
import hashlib,json
from pathlib import Path
r=json.loads(Path('/home/agent/.astrabox-prepared-runtime.json').read_text())
credential=r['model_credential']
print(json.dumps({'initialized':r['engine_initialized'],'slot_id':r['slot_id'],
 'placeholder':credential,'inert':credential=='astrabox-slot-credential-'+r['slot_id'],
 'digest':hashlib.sha256(credential.encode()).hexdigest()}))
"""])


def _native_credentials(execute: list[str], identity: dict) -> list[str]:
    # Read the supplier processes' actual environment; a receipt alone cannot
    # prove that the child did not freeze the deployment model key.
    return _run_json([*execute, "runuser", "-u", identity["linux_user"], "--", "python3", "-c", """
from pathlib import Path
import hashlib,json,sys
found=[]
for p in Path('/proc').iterdir():
 if not p.name.isdecimal():continue
 try:
  args=p.joinpath('cmdline').read_bytes().split(b'\\0')
  if (b'--input-format',b'stream-json') not in set(zip(args,args[1:])):continue
  env=dict(x.split(b'=',1) for x in p.joinpath('environ').read_bytes().split(b'\\0') if b'=' in x)
  if env.get(b'HOME',b'').decode()!=sys.argv[1]:continue
  found.extend(hashlib.sha256(env[k]).hexdigest() for k in (b'ANTHROPIC_API_KEY',b'ANTHROPIC_AUTH_TOKEN') if env.get(k))
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
print(json.dumps(found))
""", identity["home_dir"]])


def _until(read, predicate, deadline):
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.4)
    pytest.fail(f"live condition did not settle: {value}")


def _assert_inventory_image(admin: httpx.Client, sandbox_id: str, image: str):
    descriptor = data(admin.get(f"/api/v1/admin/sandboxes/{sandbox_id}", params={"backend": "binding_fixture"}))
    assert running_sandbox_image(endpoint=descriptor["endpoint"]) == image


def _assert_native_session_environment(client, sid, user, execute, identity, processes, marker, deadline):
    expected = {
        "ASTRABOX_SESSION_URL": str(client.base_url).rstrip("/") + "/sessions/" + sid,
        "RUNTIME_IDENTITY_SESSION_ID": sid,
        "BINDING_FIXTURE_OWNER": user,
        "PREWARM_ENV_BASELINE": "agent-setting-preserved",
    }
    program = (
        "import os,json; print(json.dumps({'phase':" + repr(marker)
        + ",**{k:os.environ.get(k) for k in "
        + repr([*expected, "CLAUDE_PID"]) + "}}))"
    )
    reply = send_and_follow(client, sid, "Run this Bash command exactly, then reply " + marker
                            + ":\npython3 -c " + shlex.quote(program), until="end")
    assert reply.ended and not reply.error and marker in reply.text
    # The supplier's committed Bash stdout is independent of the event adapter.
    records = _until(lambda: transcript_entries(sid), lambda rows: any(
        isinstance(row["entry"].get("toolUseResult"), dict)
        and marker in row["entry"]["toolUseResult"].get("stdout", "") for row in rows
    ), deadline)
    outputs = [
        row["entry"].get("toolUseResult", {}).get("stdout", "")
        for row in records if isinstance(row["entry"].get("toolUseResult"), dict)
    ]
    output = next(value for value in reversed(outputs) if marker in value)
    actual = json.loads(output)
    assert all(actual.get(key) == value for key, value in expected.items()), actual
    assert int(actual["CLAUDE_PID"]) in {row["pid"] for row in processes}
    hooks = _run_json([*execute, "runuser", "-u", identity["linux_user"], "--", "python3", "-c",
        "import json;from pathlib import Path;"
        "print(json.dumps([json.loads(line) for line in "
        "(Path.home()/'.prewarm-session-environment.jsonl').read_text().splitlines()]))"])
    assert hooks and all(all(row.get(key) == value for key, value in expected.items()) for row in hooks), hooks
    assert all(int(row["CLAUDE_PID"]) in {p["pid"] for p in processes} for row in hooks)
    visible = json.dumps(history(client, sid))
    assert marker in visible and all(value in visible for value in expected.values())
    return {"native_stdout": actual, "plugin_hooks": hooks}


def test_initialized_inventory_binds_each_owner_before_native_input(e2e_client: httpx.Client, live_test_deadline: float):
    admin = e2e_client
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code"
    assert _server_json("from astrabox.common.utils.settings import load_astrabox_settings; import json; print(json.dumps(load_astrabox_settings().sandbox_credential_vault_enabled))") is False
    assert _gateway()["available"], "the real gateway request-log source must be available before the journey"
    nonce = uuid.uuid4().hex[:10]
    users = [f"binding-{nonce}-{kind}" for kind in ("a", "b", "unauthorized")]
    clients = [_user_client(admin, user) for user in users]
    source = next(row for row in data(admin.get("/api/v1/admin/environments")) if row["name"] == profile["environment_name"])
    canonical = data(admin.get(f"/api/v1/agents/{profile['agent_id']}"))
    policy = {"session_bound": False, "users": users[:2], "refuse_binding": False,
              "startup_environment": True, "console_base_url": str(admin.base_url).rstrip("/")}
    options = dict(canonical.get("engine_options") or {})
    options["sdk_options"] = dict(options.get("sdk_options") or {})
    settings = dict(options["sdk_options"].get("settings") or {})
    options["sdk_options"]["settings"] = {
        **settings, "env": {**settings.get("env", {}), "PREWARM_ENV_BASELINE": "agent-setting-preserved"},
        "extraKnownMarketplaces": {**settings.get("extraKnownMarketplaces", {}), "prewarm-environment-fixture": {
            "source": {"source": "directory", "path": "/opt/prewarm-session-environment-marketplace"},
        }},
        "enabledPlugins": {**settings.get("enabledPlugins", {}),
                           "prewarm-session-environment@prewarm-environment-fixture": True},
    }
    agents = []
    environments = []
    sessions = []
    evidence = {"users": users, "environments": [], "inventory": [], "sessions": []}
    succeeded = False
    try:
        for kind in ("initialized", "session-bound", "cold"):
            name = f"binding-{nonce}-{kind}"
            environment = {**source, "name": name, "display_name": name, "sandbox_backend": "binding_fixture"}
            if kind == "session-bound":
                environment["runtime_template_name"] = os.environ[PREPARATION_IMAGE_ENV]
            data(admin.put(f"/api/v1/admin/environments/{name}", json=environment))
            environments.append(environment)
            evidence["environments"].append(name)
            data(admin.put(f"/api/v1/runtime-preparation-fixture/binding-policies/{name}", json={**policy, "session_bound": kind == "session-bound"}))
            agent = data(admin.post("/api/v1/agents", json={
                "name": name, "environment_name": name, "model": profile["model"],
                "engine_options": options, "prepared_capacity": 0,
            }))
            agents.append(agent)
            release_agent(agent["agent_id"])
            data(admin.put(f"/api/v1/agents/{agent['agent_id']}/access", json={
                "visibility": "public", "admins": [], "allowed_user_ids": [],
            }))
            data(admin.put(f"/api/v1/agents/{agent['agent_id']}", json={
                "name": name, "environment_name": name, "model": profile["model"],
                "engine_options": options,
                "prepared_capacity": 2 if kind == "initialized" else 1 if kind == "session-bound" else 0,
            }))
        normal, gated, cold = agents
        _wait_prepared_count(admin, normal["agent_id"], 2, live_test_deadline)
        gate_inventory = _wait_prepared(admin, gated["agent_id"], live_test_deadline)
        _assert_inventory_image(admin, gate_inventory["sandbox_id"], os.environ[PREPARATION_IMAGE_ENV])
        gate_execute = _sandbox_exec(gate_inventory["sandbox_id"])
        assert _receipt(gate_execute)["initialized"] is False
        receipts = []
        processes = []
        executions = []
        identities = []
        for index in range(2):
            inventory = _wait_prepared(admin, normal["agent_id"], live_test_deadline)
            _assert_inventory_image(admin, inventory["sandbox_id"], profile["image"])
            execute = _sandbox_exec(inventory["sandbox_id"])
            receipt = _receipt(execute)
            assert receipt["initialized"] is True and receipt["inert"] is True
            identity = _prepared_identity(normal["agent_id"], "conversation", execute)["runtime_identity"]
            before = _supplier_processes(execute, identity, "claude_code")
            assert set(_native_credentials(execute, identity)) == {receipt["digest"]}
            sid = data(clients[index].post(f"/api/v1/agents/{normal['agent_id']}/conversations", json={}))['session_id']
            sessions.append(sid)
            release_session(sid)
            poll_until_ready(clients[index], sid)
            assert get_admin_session_detail(admin, sid)["sandbox_id"] == inventory["sandbox_id"]
            assert _supplier_processes(execute, identity, "claude_code") == before
            assert set(_native_credentials(execute, identity)) == {receipt["digest"]}
            evidence["inventory"].append(inventory)
            evidence["sessions"].append({"session_id": sid, "sandbox_id": inventory["sandbox_id"], "processes": before})
            receipts.append(receipt)
            processes.append(before)
            executions.append(execute)
            identities.append(identity)
        assert receipts[0]["placeholder"] != receipts[1]["placeholder"]

        def answer(index):
            marker = f"BINDING_{nonce}_{index}"
            evidence.setdefault("environment", {})[str(index)] = _assert_native_session_environment(
                clients[index], sessions[index], users[index], executions[index],
                identities[index], processes[index], marker, live_test_deadline,
            )
            return marker

        with ThreadPoolExecutor(max_workers=2) as pool:
            markers = list(pool.map(answer, range(2)))
        for index, sid in enumerate(sessions):
            native = _until(lambda: transcript_entries(sid), lambda entries: markers[index] in json.dumps(entries), live_test_deadline)
            assert any(row["entry"].get("type") == "assistant" and markers[index] in json.dumps(row["entry"]) for row in native)

        _restart_server_container(require_service_container(SERVER_CONTAINER_HANDLE))
        _wait_health(str(admin.base_url).rstrip("/"))
        evidence["reattached_environment"] = _assert_native_session_environment(
            clients[0], sessions[0], users[0], executions[0], identities[0], processes[0],
            f"REATTACHED_{nonce}", live_test_deadline,
        )
        for index in range(2):
            assert _supplier_processes(executions[index], identities[index], "claude_code") == processes[index]
            assert set(_native_credentials(executions[index], identities[index])) == {receipts[index]["digest"]}

        unauthorized = data(clients[2].post(f"/api/v1/agents/{normal['agent_id']}/conversations", json={}))['session_id']
        release_session(unauthorized)
        sessions.append(unauthorized)
        blocked = _until(lambda: get_admin_session_detail(admin, unauthorized), lambda row: row.get("runtime_unavailable") and (row.get("startup_user_action") or {}).get("status") == "user_action_required", live_test_deadline)
        assert blocked["state"] == "RECOVERY_REQUIRED"
        assert not blocked.get("sandbox_id") and not blocked.get("runtime_identity")
        rejected = clients[2].post(f"/api/v1/sessions/{unauthorized}/inputs", json={"content": "MUST_NOT_ENTER_NATIVE", "client_message_id": uuid.uuid4().hex})
        assert psql_rows(f"SELECT startup_user_action FROM sessions WHERE session_id={sql_literal(unauthorized)}")[0]["startup_user_action"]
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "TURN_PREPARATION_USER_ACTION_REQUIRED"
        assert not psql_rows(f"SELECT input_id FROM session_inputs WHERE session_id={sql_literal(unauthorized)}")

        waiting = _wait_prepared(admin, normal["agent_id"], live_test_deadline)
        data(admin.put(f"/api/v1/runtime-preparation-fixture/binding-policies/{environments[0]['name']}", json={**policy, "refuse_binding": True}))
        refused = data(clients[0].post(f"/api/v1/agents/{normal['agent_id']}/conversations", json={}))['session_id']
        sessions.append(refused)
        release_session(refused)
        failure = _until(lambda: get_admin_session_detail(admin, refused), lambda row: row.get("runtime_unavailable") and row.get("state") == "TERMINATED", live_test_deadline)
        assert "preparation" in str(failure["last_error"]).lower()
        assert not failure.get("sandbox_id"), "the refused inventory claim must be disposed, not replaced silently"
        assert not any(row["entry"].get("type") == "assistant" for row in transcript_entries(refused)), "refused identity binding must not reach a model through cold fallback"
        evidence["refused_binding"] = {"session_id": refused, "prepared_box": waiting["sandbox_id"], "error": failure["last_error"]}

        gated_sid = data(clients[1].post(f"/api/v1/agents/{gated['agent_id']}/conversations", json={}))['session_id']
        sessions.append(gated_sid)
        release_session(gated_sid)
        poll_until_ready(clients[1], gated_sid)
        assert get_admin_session_detail(admin, gated_sid)["sandbox_id"] == gate_inventory["sandbox_id"]
        gated_identity = get_admin_session_detail(admin, gated_sid)["runtime_identity"]
        gate_record = _run_json([*gate_execute, "python3", "-c",
            "from pathlib import Path; import json,sys; print(json.dumps([json.loads(x) for x in (Path(sys.argv[1])/'.runtime-preparation-native.jsonl').read_text().splitlines()]))", gated_identity["home_dir"]])
        assert len(gate_record) == 1 and gate_record[0]["session_id"] == gated_sid and gate_record[0]["principal_id"] == users[1]
        assert gate_record[0]["mode"] == 0o600 and gate_record[0]["uid"] == gated_identity["uid"]
        evidence["session_bound_environment"] = _assert_native_session_environment(
            clients[1], gated_sid, users[1], gate_execute, gated_identity,
            _supplier_processes(gate_execute, gated_identity, "claude_code"),
            f"SESSION_MATERIAL_{nonce}", live_test_deadline,
        )
        cold_sid = data(clients[0].post(f"/api/v1/agents/{cold['agent_id']}/conversations", json={}))["session_id"]
        sessions.append(cold_sid)
        release_session(cold_sid)
        poll_until_ready(clients[0], cold_sid)
        cold_detail = get_admin_session_detail(admin, cold_sid)
        cold_execute = _sandbox_exec(cold_detail["sandbox_id"])
        cold_identity = cold_detail["runtime_identity"]
        evidence["cold_environment"] = _assert_native_session_environment(
            clients[0], cold_sid, users[0], cold_execute, cold_identity,
            _supplier_processes(cold_execute, cold_identity, "claude_code"),
            f"COLD_MATERIAL_{nonce}", live_test_deadline,
        )
        gateway = []
        for index, sid in enumerate(sessions[:2]):
            facts = _until(lambda: _gateway(sid), lambda facts: bool(facts["requests"]), live_test_deadline)
            assert facts["session_id"] == sid and facts["principal_id"] == users[index]
            assert all(row["key_digest"] == facts["key_digest"] and row["total_tokens"] > 0 for row in facts["requests"])
            gateway.append(facts)
        assert gateway[0]["key_digest"] != gateway[1]["key_digest"]
        assert all(receipt["digest"] not in {row["key_digest"] for row in gateway} for receipt in receipts)
        evidence["gateway"] = gateway
        evidence["session_bound_native"] = gate_record
        print(json.dumps({"phase": "provider-owned-model-binding-accepted", **evidence}), flush=True)
        succeeded = True
    finally:
        for client in clients:
            client.close()
        if succeeded:
            # These model keys belong to the fixture integration, not core Vault.
            _server_json("""
import asyncio,json,sys
from astrabox.providers.model import LiteLLMModelEndpointProvider
from astrabox.seams.model import ModelRequestContext
async def cleanup():
 provider=LiteLLMModelEndpointProvider()
 for sid in json.loads(sys.argv[1]):
  await provider.release_session_credential(context=ModelRequestContext(conversation_id=sid))
 print(json.dumps({'released':True}))
asyncio.run(cleanup())
""", json.dumps(sessions))
