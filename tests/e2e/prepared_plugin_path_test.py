"""A prepared Claude plugin remains usable across claim, reattach and replacement.

The selected profile Agent supplies the immutable Git checkout of
fixtures/prepared_plugin, served over HTTPS by the acceptance fixture.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile, data, environment_with_tenancy, get_admin_session_detail,
    poll_until_agent_ready, release_agent, release_session, send_and_follow,
    transcript_entries, wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.conftest import _wait_health
from tests.e2e.test_prepared_runtime_activation import _run_json, _sandbox_exec, _wait_prepared
from tests.e2e.test_reattach import _restart_server_container

pytestmark = [pytest.mark.e2e, pytest.mark.backend_restart, pytest.mark.xdist_group("backend-restart")]


def _plugin_facts(detail: dict) -> dict:
    identity = detail["runtime_identity"]
    plan = identity["capability_plan"]["plugin_repo_plan"]
    assert len(plan) == 1
    checkout = plan[0]["target_path"]
    program = """
import json,os,sys,subprocess
from pathlib import Path
checkout=Path(sys.argv[1])
manifest=json.loads((checkout/'.claude-plugin/plugin.json').read_text())
servers=json.loads((checkout/'.mcp.json').read_text())['mcpServers']
processes=[]
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal(): continue
    try:
        args=proc.joinpath('cmdline').read_bytes().split(b'\\0')
        paths=[args[i+1].decode() for i,arg in enumerate(args[:-1]) if arg==b'--plugin-dir']
        if paths:
            environment=dict(item.split(b'=',1) for item in proc.joinpath('environ').read_bytes().split(b'\\0') if b'=' in item)
            processes.append({'pid':int(proc.name),'paths':paths,
                              'config_dir':environment[b'CLAUDE_CONFIG_DIR'].decode()})
    except (FileNotFoundError,ProcessLookupError,PermissionError): continue
receipts=[]
assert len(processes)==1,processes
config=Path(processes[0]['config_dir'])
for path in (config/'plugins/data').glob('*/fixture_*.jsonl'):
    receipts.extend(json.loads(line) for line in path.read_text().splitlines() if line)
print(json.dumps({'uid':os.geteuid(),'manifest':manifest,'servers':sorted(servers),
                  'revision':subprocess.check_output(['git','-c','safe.directory='+str(checkout.resolve()),
                      '-C',str(checkout),'rev-parse','HEAD'],text=True).strip(),
                  'skill':(checkout/'skills/verify/SKILL.md').read_text(),
                  'processes':processes,'receipts':receipts,'checkout':str(checkout)}))
"""
    facts = _run_json([
        *_sandbox_exec(detail["sandbox_id"]), "runuser", "-u", identity["linux_user"],
        "--", "python3", "-c", program, checkout,
    ])
    assert facts["uid"] == identity["uid"]
    assert facts["manifest"]["name"] == "prepared-path-fixture"
    assert facts["servers"] == ["fixture_one", "fixture_two"]
    assert len(facts["processes"]) == 1, facts
    assert facts["processes"][0]["paths"] == [checkout], facts
    return facts


def test_prepared_plugins_work_after_claim_reconnect_and_recovery(
    e2e_client: httpx.Client, live_test_deadline: float, request: pytest.FixtureRequest,
) -> None:
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code"
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    packages = canonical["packages"]
    assert len(packages) == 1 and len(packages[0]["sha"]) == 40, (
        "selected profile must supply the pinned prepared_plugin fixture", packages
    )
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": "Prepared plugin " + uuid.uuid4().hex[:8], "model": profile["model"],
        "environment_name": environment_with_tenancy(e2e_client, "conversation"),
        "prepared_capacity": 1, "packages": packages,
    }))
    aid = agent["agent_id"]
    release_agent(aid)
    prepared = _wait_prepared(e2e_client, aid, live_test_deadline)
    session = data(e2e_client.post(f"/api/v1/agents/{aid}/conversations", json={}))
    sid = session["session_id"]
    release_session(sid)
    poll_until_agent_ready(e2e_client, sid)
    claimed = get_admin_session_detail(e2e_client, sid)
    assert claimed["sandbox_id"] == prepared["sandbox_id"]
    assert f"/{sid}/" not in _plugin_facts(claimed)["checkout"]
    first_marker = "PLUGIN-" + uuid.uuid4().hex
    evidence = []

    def exercise(stage: str) -> dict:
        marker = first_marker if stage == "claimed" else "PLUGIN-" + uuid.uuid4().hex
        result = send_and_follow(e2e_client, sid, f"/prepared-path-fixture:verify {marker}", until="end")
        assert result.error is None and "PLUGIN-SKILL-058-CONTENT-OK" in result.text, result.response
        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["outcome"] == "completed", settled
        detail = get_admin_session_detail(e2e_client, sid)
        facts = _plugin_facts(detail)
        assert facts["revision"] == packages[0]["sha"], facts
        assert "PLUGIN-SKILL-058-CONTENT-OK" in facts["skill"]
        receipts = [row for row in facts["receipts"] if row["marker"] == marker]
        assert {row["server"] for row in receipts} == set(facts["servers"]), facts
        assert all(row["uid"] == facts["uid"] for row in receipts), facts
        calls = []
        while time.monotonic() < live_test_deadline:
            calls = [
                block for row in transcript_entries(sid)
                if row["entry"].get("type") == "assistant"
                for block in row["entry"].get("message", {}).get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("input", {}).get("marker") == marker
                and str(block.get("name", "")).startswith("mcp__plugin_")
            ]
            if all(any(call["name"].endswith(f"_{server}__record") for call in calls)
                   for server in facts["servers"]):
                break
            time.sleep(0.2)
        assert all(any(call["name"].endswith(f"_{server}__record") for call in calls)
                   for server in facts["servers"]), calls
        evidence.append({"stage": stage, "sandbox_id": detail["sandbox_id"],
                         "native_session": detail["engine_session_key"],
                         "native_tools": calls, "facts": facts})
        print(f"PLUGIN_PATH stage={stage} both MCP tools and Plugin Skill passed", flush=True)
        return detail

    original = exercise("claimed")
    assert first_marker in json.dumps(transcript_entries(sid)), "native history was not mirrored"
    _restart_server_container(require_service_container(SERVER_CONTAINER_HANDLE))
    _wait_health(str(e2e_client.base_url))
    e2e_client = httpx.Client(base_url=e2e_client.base_url, headers=e2e_client.headers, timeout=30)
    request.addfinalizer(e2e_client.close)
    reattached = exercise("reattached")
    assert reattached["sandbox_id"] == original["sandbox_id"]
    assert reattached["runtime_identity"]["capability_plan"] == original["runtime_identity"]["capability_plan"]
    assert evidence[-1]["facts"]["processes"] == evidence[0]["facts"]["processes"]

    waiting = _wait_prepared(e2e_client, aid, live_test_deadline)
    assert waiting["sandbox_id"] != original["sandbox_id"]
    removed = data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
    assert removed["killed"] is True
    data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60))
    poll_until_agent_ready(e2e_client, sid)
    recovered = exercise("recovered")
    assert recovered["sandbox_id"] == waiting["sandbox_id"]
    assert recovered["engine_session_key"] == original["engine_session_key"]
    assert first_marker in json.dumps(transcript_entries(sid))
    request.node.user_properties.append(("prepared_plugin_paths", json.dumps(evidence)))
