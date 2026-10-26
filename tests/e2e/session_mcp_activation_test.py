"""Personal MCPs bind at claim and recover without replacing native history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e import _docker_compute
from tests.e2e._sandbox_helpers import (
    current_profile, data, environment_with_tenancy, get_admin_session_detail,
    get_session, history, release_agent, release_session, send_and_follow,
    transcript_entries, wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_prepared_runtime_activation import (
    _run_json, _sandbox_exec, _supplier_processes, _wait_prepared_count,
)

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def _native_history(sid: str) -> list[dict]:
    return [row["entry"] for row in transcript_entries(sid) if not row["subpath"]]


def _journals(execute: list[str], identity: dict) -> list[dict]:
    rows = _run_json([*execute, "python3", "-c", """
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'.session-mcp-receipts.jsonl'
print(json.dumps([json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []))
""", identity["home_dir"]])
    assert isinstance(rows, list)
    return rows


def _block(server: str, principals: list[str]) -> None:
    subprocess.run(["docker", "exec", "-u", "0", server, "python", "-c", """
from pathlib import Path
import sys
p=Path('/data/session-mcp-fixture');p.mkdir(exist_ok=True)
(p/'blocked.json').write_text(sys.argv[1])
""", json.dumps(principals)], check=True, capture_output=True, timeout=15)


def _settled_recovery(client: httpx.Client, sid: str, deadline: float) -> dict:
    data(client.post(f"/api/v1/sessions/{sid}/recover"))
    while time.monotonic() < deadline:
        row = get_session(client, sid)
        if row.get("state") != "CREATING":
            return row
        time.sleep(0.2)
    pytest.fail("explicit MCP recovery did not settle")


def test_user_bound_mcp_activates_in_the_prepared_process_and_recovers(
    e2e_client: httpx.Client, e2e_base_url: str, live_test_deadline: float,
) -> None:
    """Two owners, ordinary and Plugin MCPs, consent, and one failed connection."""
    a = e2e_client
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code"
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    token_path = Path(os.environ["ASTRABOX_E2E_SECOND_AUTH_TOKEN_FILE"])
    assert token_path.is_absolute() and token_path.is_file() and not token_path.is_symlink()
    with httpx.Client(base_url=e2e_base_url, headers={
        "Authorization": "Bearer " + token_path.read_text().strip(),
    }, timeout=30) as b:
        owners = [data(client.get("/api/v1/user/current"))["user_id"] for client in (a, b)]
        assert len(set(owners)) == 2
        environment = environment_with_tenancy(a, "agent")
        canonical = data(a.get(f"/api/v1/agents/{profile['agent_id']}"))
        packages = canonical["packages"]
        assert len(packages) == 1 and len(packages[0]["sha"]) == 40, packages
        agent = data(a.post("/api/v1/agents", json={
            "name": "Session MCP " + uuid.uuid4().hex[:8],
            "runtime_profile_name": environment, "model": profile["model"],
            "prepared_capacity": 2,
            "packages": packages,
            "mcp_servers": {kind: {"command": "python3", "args": [
                "/opt/astrabox/session-mcp-fixture/personal_mcp.py", kind,
            ]} for kind in ("personal", "shared")},
        }))["agent_id"]
        release_agent(agent)
        _wait_prepared_count(a, agent, 2, live_test_deadline)
        boxes = subprocess.check_output(
            ["docker", "ps", "-q", "--filter", "ancestor=" + profile["image"]],
            text=True, timeout=15,
        ).split()
        slots = []
        for box in boxes:
            slots.extend(_run_json(["docker", "exec", box, "python3", "-c", """
import json,sys
from pathlib import Path
path=Path('/home/agent/.astrabox-prepared-seats.json')
seats=json.loads(path.read_text()) if path.exists() else []
print(json.dumps([{k:s.get(k) for k in ('slot_id','sandbox_id','runtime_identity','engine_initialized')}
                  for s in seats if s.get('state')=='prepared'
                  and s['runtime_identity'].get('agent_id')==sys.argv[1]]))
""", agent]))
        assert len(slots) >= 2, slots
        inventory = {}
        for slot in slots:
            assert slot["engine_initialized"] is True
            assert not _docker_compute.durable_mounts("sandbox-" + slot["sandbox_id"]), "core MCP lifecycle used a persistent mount"
            identity = slot["runtime_identity"]
            execute = _sandbox_exec(slot["sandbox_id"])
            native = _supplier_processes(execute, identity, "claude_code")
            assert len(native) == 1
            while time.monotonic() < live_test_deadline:
                rows = _journals(execute, identity)
                assert not any(row["kind"] in {"personal", "plugin"} for row in rows), rows
                if any(row["event"] == "identity_bound" and row["kind"] == "shared" for row in rows):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("ordinary shared MCP did not start in prepared inventory")
            inventory[identity["home_dir"]] = {"native": native, "rows": rows}

        # Missing authorization must not claim/start a personal MCP or persist input.
        _block(server, [owners[1]])
        sid_b = data(b.post(f"/api/v1/agents/{agent}/conversations", json={}))["session_id"]
        release_session(sid_b)
        while get_session(b, sid_b).get("state") == "CREATING":
            assert time.monotonic() < live_test_deadline
            time.sleep(0.2)
        assert data(b.get(f"/api/v1/sessions/{sid_b}/user-action"))["ready"] is False
        refused = b.post(f"/api/v1/sessions/{sid_b}/inputs", json={
            "content": "MUST_NOT_REACH_NATIVE", "client_message_id": str(uuid.uuid4()),
        })
        assert refused.status_code == 409, refused.text
        assert not get_session(b, sid_b).get("sandbox_id"), "unauthorized user claimed a native box"

        def read_identity(client: httpx.Client, sid: str) -> tuple[str, object]:
            marker = "IDENTITY_OK_" + uuid.uuid4().hex
            reply = send_and_follow(client, sid,
                "Call each of the three MCP whoami tools once: personal, shared, and the fixture-identity Plugin's personal server. "
                "Return their exact JSON results, then " + marker + ". Do not use Bash or change files.", until="end", permission_mode="bypassPermissions")
            assert reply.error is None and marker in reply.text, reply
            wait_until_settled(client, sid)
            return marker, reply

        with ThreadPoolExecutor(max_workers=2) as pool:
            sid_a = data(a.post(f"/api/v1/agents/{agent}/conversations", json={}))["session_id"]
            release_session(sid_a)
            first_a = pool.submit(read_identity, a, sid_a)
            _block(server, [])
            data(b.post(f"/api/v1/sessions/{sid_b}/recover"))
            first_b = pool.submit(read_identity, b, sid_b)
            for client, sid in ((a, sid_a), (b, sid_b)):
                while get_session(client, sid).get("state") == "CREATING":
                    assert time.monotonic() < live_test_deadline
                    time.sleep(0.2)
                ready = get_session(client, sid)
                assert ready.get("state") == "READY" and not ready.get("last_error"), ready
            details = [get_admin_session_detail(a, sid) for sid in (sid_a, sid_b)]
            # Stop replenishing unrelated waiting inventory after the claims.
            data(a.put(f"/api/v1/agents/{agent}", json={"prepared_capacity": 0, "model": profile["model"], "runtime_profile_name": environment}))
            for detail in details:
                identity = detail["runtime_identity"]
                assert identity["home_dir"] in inventory, "claim bypassed initialized inventory"
                assert _supplier_processes(_sandbox_exec(detail["sandbox_id"]), identity, "claude_code") == inventory[identity["home_dir"]]["native"]
            answers = [first_a.result(), first_b.result()]
        for index, (client, sid, detail) in enumerate(((a, sid_a, details[0]), (b, sid_b, details[1]))):
            identity = detail["runtime_identity"]
            rows = _journals(_sandbox_exec(detail["sandbox_id"]), identity)
            assert not any(row["event"] in {"authorization_required", "stale_environment"}
                           for row in rows), "personal MCP attempted a handshake before its owner material"
            calls = [row for row in rows if row["event"] == "call"]
            assert {row["kind"] for row in calls} == {"personal", "plugin", "shared"}, rows
            for row in calls:
                assert row["principal_id"] == ("shared" if row["kind"] == "shared" else owners[index])
                assert row["session_id"] == (None if row["kind"] == "shared" else sid)
            visible = json.dumps(history(client, sid), ensure_ascii=False)
            assert owners[index] in visible and sid in visible and answers[index][0] in visible
            assert owners[1-index] not in visible
            native = _native_history(sid)
            root_inputs = [entry for entry in native if entry.get("type") == "user"
                           and "IDENTITY_OK_" in json.dumps(entry.get("message", {}))]
            assert len(root_inputs) == 1
            submitted = datetime.fromisoformat(root_inputs[0]["timestamp"].replace("Z", "+00:00")).timestamp()
            assert submitted >= max(row["at"] for row in rows
                                    if row["event"] == "identity_bound" and row["kind"] in {"personal", "plugin"})

        # An existing claimed MCP loses authorization; recover the connection,
        # retaining Claude, the ordinary MCP, and every original native input.
        detail = details[0]
        identity = detail["runtime_identity"]
        execute = _sandbox_exec(detail["sandbox_id"])
        old_rows = _journals(execute, identity)
        plugin = next(row for row in reversed(old_rows) if row["kind"] == "plugin" and row["event"] == "identity_bound")
        old_native = _native_history(sid_a)
        subprocess.run([*execute, "python3", "-c", """
from pathlib import Path
import os,signal,sys
home,pid=Path(sys.argv[1]),int(sys.argv[2])
assert b'/opt/astrabox/session-mcp-fixture/personal_mcp.py' in Path(f'/proc/{pid}/cmdline').read_bytes()
(home/'.session-mcp-deny').touch()
os.kill(pid,signal.SIGTERM)
""", identity["home_dir"], str(plugin["pid"])], check=True, capture_output=True, timeout=15)
        failed = _settled_recovery(a, sid_a, live_test_deadline)
        assert failed.get("last_error") and "MCP" in failed["last_error"], failed
        assert _supplier_processes(execute, identity, "claude_code") == inventory[identity["home_dir"]]["native"]
        failed_native = _native_history(sid_a)
        assert failed_native[:len(old_native)] == old_native, "recovery replaced native history"
        assert [row for row in failed_native if row.get("type") == "user"] == [row for row in old_native if row.get("type") == "user"], "recovery replayed native input"
        subprocess.run([*execute, "rm", str(Path(identity["home_dir"]) / ".session-mcp-deny")],
                       check=True, capture_output=True, timeout=15)
        data(a.post(f"/api/v1/sessions/{sid_a}/recover"))
        recovered_marker, recovered_reply = read_identity(a, sid_a)
        restored = get_session(a, sid_a)
        assert restored.get("state") == "READY" and not restored.get("last_error"), restored
        assert _supplier_processes(execute, identity, "claude_code") == inventory[identity["home_dir"]]["native"]
        after = _journals(execute, identity)
        assert {row["pid"] for row in after if row["kind"] == "shared"} == {row["pid"] for row in old_rows if row["kind"] == "shared"}
        assert any(row["event"] == "identity_bound" and row["kind"] == "plugin"
                   and row["pid"] != plugin["pid"] and row["principal_id"] == owners[0] for row in after)
        for sid in (sid_a, sid_b):
            native = _native_history(sid)
            assert "MUST_NOT_REACH_NATIVE" not in json.dumps(native)
            for marker, _reply in answers:
                count = sum(entry.get("type") == "user" and marker in json.dumps(entry.get("message", {})) for entry in native)
                assert count == (1 if marker == answers[0 if sid == sid_a else 1][0] else 0)
        final_native = _native_history(sid_a)
        recovered_input = [entry for entry in final_native if entry.get("type") == "user"
                           and recovered_marker in json.dumps(entry.get("message", {}))]
        assert len(recovered_input) == 1
        consumed_at = datetime.fromisoformat(recovered_input[0]["timestamp"].replace("Z", "+00:00")).timestamp()
        assert consumed_at >= max(row["at"] for row in after if row["event"] == "identity_bound")
        print(json.dumps({"phase": "session-mcp-activation-accepted", "agent_id": agent,
                          "sessions": [sid_a, sid_b], "owners": owners,
                          "inventory": inventory, "claimed": [{key: detail.get(key) for key in ("session_id", "sandbox_id", "runtime_identity")} for detail in details],
                          "native_mcp_receipts": {sid_a: after, sid_b: _journals(_sandbox_exec(details[1]["sandbox_id"]), details[1]["runtime_identity"])},
                          "native_history": {sid_a: final_native, sid_b: _native_history(sid_b)},
                          "visible_history": {sid_a: history(a, sid_a), sid_b: history(b, sid_b)},
                          "first_live_answers": [{"marker": marker, "text": reply.text} for marker, reply in answers],
                          "recovery_live_answer": {"marker": recovered_marker, "text": recovered_reply.text},
                          "failed_recovery": {"session": failed, "native_history": failed_native},
                          "persistent_workspace_volume": False}))
