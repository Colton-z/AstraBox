"""A prewarmed conversation adopts an already running supplier, in either tenancy."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    current_profile,
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_agent,
    release_session,
    send_and_follow,
    transcript_entries,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e


def _run_json(command: list[str]) -> dict | list:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, f"Evidence probe failed: {result.stderr[-800:]}"
    return json.loads(result.stdout)


def _sandbox_exec(sandbox_id: str) -> list[str]:
    kubeconfig = os.environ["ASTRABOX_E2E_KUBECONFIG"]
    namespace = os.environ["ASTRABOX_E2E_KUBE_NAMESPACE"]
    kubectl = ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace]
    batch = _run_json([*kubectl, "get", "batchsandbox", sandbox_id, "-o", "json"])
    assert isinstance(batch, dict)
    owner_uid = batch["metadata"]["uid"]
    inventory = _run_json([*kubectl, "get", "pods", "-o", "json"])
    assert isinstance(inventory, dict)
    pods = [
        pod for pod in inventory["items"]
        if any(
            owner.get("kind") == "BatchSandbox" and owner.get("uid") == owner_uid
            for owner in pod["metadata"].get("ownerReferences", [])
        )
    ]
    assert len(pods) == 1, f"Expected one physical Pod for prepared sandbox {sandbox_id}"
    assert pods[0]["status"]["phase"] == "Running"
    return [*kubectl, "exec", pods[0]["metadata"]["name"], "-c", "sandbox", "--"]


# Deliberately project only non-secret identity. Prepared receipts also contain
# credentials; neither subprocess output nor a failing assertion may expose them.
_PROJECT_RECEIPT = """
identity = receipt.get('runtime_identity') or {}
print(json.dumps({
    'sandbox_id': receipt.get('sandbox_id'),
    'slot_id': receipt.get('slot_id'),
    'prepared_native_session': receipt.get('prepared_native_session'),
    'runtime_identity': {key: identity.get(key) for key in (
        'home_dir', 'linux_user', 'isolated_session_id', 'workspace_dir'
    )},
}))
"""


# A shared-box claim takes the Agent's oldest prepared seat: the seats waiting
# in boxes the Agent holds, else the seats of the pool box it takes next.
_OLDEST_SEAT = """
seats = [seat for seat in seats if seat.get('state') == 'prepared']
receipt = min(seats, key=lambda seat: str(seat.get('prepared_at') or '')) if seats else {}
"""


def _prepared_identity(agent_id: str, tenancy: str, execute: list[str]) -> dict:
    if tenancy == "agent":
        ledger = """
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
ensure_database_wiring()
row = asyncio.run(AgentRepository().read_ledger(sys.argv[1]))
seats = list((row or {}).get('_prepared_slots') or [])
""" + _OLDEST_SEAT + _PROJECT_RECEIPT
        receipt = _run_json([
            "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
            "python", "-c", ledger, agent_id,
        ])
        if isinstance(receipt, dict) and receipt.get("slot_id"):
            command: list[str] = []
        else:
            program = """
import json
from pathlib import Path
path = Path('/home/agent/.astrabox-prepared-seats.json')
assert path.is_file(), 'prewarm READY has no prepared seat receipt'
seats = json.loads(path.read_text())
""" + _OLDEST_SEAT + _PROJECT_RECEIPT
            command = [*execute, "python3", "-c", program]
    else:
        program = """
import json
from pathlib import Path
path = Path('/home/agent/.astrabox-prepared-runtime.json')
assert path.is_file(), 'prewarm READY has no prepared engine receipt'
receipt = json.loads(path.read_text())
""" + _PROJECT_RECEIPT
        command = [*execute, "python3", "-c", program]
    if command:
        receipt = _run_json(command)
    assert isinstance(receipt, dict)
    assert receipt["slot_id"], "Prewarm READY has no initialized engine slot"
    identity = receipt["runtime_identity"]
    assert identity["home_dir"] and identity["linux_user"], receipt
    return receipt


def _answered(client: httpx.Client, sid: str, prompt: str) -> Reply:
    """Send one message, follow its response to the end, and require it completed."""
    reply = send_and_follow(client, sid, prompt, until="end")
    assert reply.error is None, reply.error
    assert reply.text.strip(), f"the response carried no reply text: {reply.response}"
    settled = wait_until_settled(client, sid)
    assert settled["last_response"]["response_id"] == reply.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled
    return reply


def _supplier_processes(execute: list[str], identity: dict, engine: str) -> list:
    # Native protocol flags (or Pi's overwritten process title) identify vendor
    # processes. /proc start ticks distinguish a surviving PID from PID reuse.
    program = """
import json, os, sys
from pathlib import Path
engine, home = sys.argv[1:]
found = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        args = proc.joinpath('cmdline').read_bytes().split(b'\\0')
        executable = str(proc.joinpath('exe').resolve())
        pairs = set(zip(args, args[1:]))
        native = {
            'claude_code': (b'--input-format', b'stream-json') in pairs
                and (b'--output-format', b'stream-json') in pairs,
            'pi': args[:1] == [b'pi'],
            'codex': b'app-server' in args and Path(executable).name == 'codex',
            'deepseek_harness': (b'--profile', b'web') in pairs
                and not Path(executable).name.startswith('python'),
        }[engine]
        if not native:
            continue
        env = dict(item.split(b'=', 1) for item in
            proc.joinpath('environ').read_bytes().split(b'\\0') if b'=' in item)
        if env.get(b'HOME', b'').decode() != home:
            continue
        fields = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        found.append({'pid': int(proc.name), 'start_ticks': fields[19],
            'executable': executable})
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
print(json.dumps(sorted(found, key=lambda item: item['pid'])))
"""
    processes = _run_json([
        *execute, "runuser", "-u", identity["linux_user"], "--", "python3", "-c",
        program, engine, identity["home_dir"],
    ])
    assert isinstance(processes, list)
    assert processes, f"Prewarm READY has no live {engine} supplier in {identity['home_dir']}"
    return processes


def _wait_prepared(
    client: httpx.Client, agent_id: str, deadline: float, *, previous_generation: str = "",
) -> dict:
    prepared: dict = {}
    while time.monotonic() < deadline:
        prepared = data(client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
        assert not prepared.get("last_error"), prepared
        if (
            prepared.get("ready")
            and int(prepared.get("prepared_count") or 0) > 0
            and prepared.get("sandbox_id")
            and prepared.get("runtime_generation")
            and prepared["runtime_generation"] != previous_generation
        ):
            return prepared
        time.sleep(0.3)
    pytest.fail(f"Agent preparation did not reach the requested generation: {prepared}")


def _wait_prepared_count(client: httpx.Client, agent_id: str, count: int, deadline: float) -> dict:
    prepared: dict = {}
    while time.monotonic() < deadline:
        prepared = data(client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
        assert not prepared.get("last_error"), prepared
        if prepared.get("ready") and int(prepared.get("prepared_count") or 0) >= count:
            return prepared
        time.sleep(0.5)
    pytest.fail(f"Agent did not reach {count} prepared sandboxes: {prepared}")


def test_concurrent_conversations_each_claim_a_prepared_sandbox(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """An Agent keeps the capacity it asks for; a burst claims all of it.

    The capacity is raised on a running Agent, so its pool is resized rather
    than started at the new size. Each conversation of a burst as large as the
    capacity must start on a sandbox that existed before the burst was sent: a
    cold start creates its sandbox afterwards. One more conversation finds the
    capacity exhausted, starts cold, and is reported as a miss. The pool then
    refills to the capacity without another conversation asking.
    """
    # The capacity, its refill and one cold box must schedule beside the
    # deployment's own prewarmed fixture Agents on a single testbed node.
    capacity = 2
    profile = current_profile()
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Prepared burst {uuid.uuid4().hex[:8]}",
        "model": profile["model"],
        "runtime_profile_name": environment_with_tenancy(e2e_client, "conversation"),
        "prepared_capacity": 1,
        "engine_options": canonical.get("engine_options") or {},
        **({"system": canonical["system"]} if canonical.get("system") else {}),
    }))
    agent_id = created["agent_id"]
    release_agent(agent_id)
    _wait_prepared_count(e2e_client, agent_id, 1, live_test_deadline)
    resized = data(e2e_client.put(f"/api/v1/agents/{agent_id}", json={
        "name": created["name"],
        "model": created["model"],
        "runtime_profile_name": created["runtime_profile_name"],
        "prepared_capacity": capacity,
        "version": created["version"],
    }))
    assert resized["prepared_capacity"] == capacity
    prepared = _wait_prepared_count(e2e_client, agent_id, capacity, live_test_deadline)
    assert prepared["target"] == capacity
    assert prepared["last_miss_at"] is None

    burst_sent_at = datetime.now().astimezone()

    def start() -> str:
        sid = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))["session_id"]
        release_session(sid)
        poll_until_agent_ready(e2e_client, sid)
        return sid

    with ThreadPoolExecutor(max_workers=capacity) as executor:
        sessions = list(executor.map(lambda _: start(), range(capacity)))

    sandbox_ids = [get_admin_session_detail(e2e_client, sid)["sandbox_id"] for sid in sessions]
    assert len(set(sandbox_ids)) == capacity, sandbox_ids
    for sandbox_id in sandbox_ids:
        box = data(e2e_client.get(f"/api/v1/admin/sandboxes/{sandbox_id}"))
        assert box["created_at"], box
        created_at = datetime.fromisoformat(str(box["created_at"]).replace("Z", "+00:00"))
        assert created_at < burst_sent_at, (
            f"sandbox {sandbox_id} was created at {created_at}, after the burst was "
            f"sent at {burst_sent_at}: that conversation started cold"
        )

    overflow_sent_at = datetime.now().astimezone()
    start()
    missed = data(e2e_client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
    assert missed["last_miss_at"], missed
    assert datetime.fromisoformat(missed["last_miss_at"]) >= overflow_sent_at, missed

    _wait_prepared_count(e2e_client, agent_id, capacity, live_test_deadline)


def test_a_shared_agent_burst_claims_a_prepared_seat_each(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Conversations sharing a box start a burst as large as the capacity warm.

    The seats wait in a pool box, several to a box, so the burst finds none
    in the Agent's own boxes and has to take the pool box while it runs. Every
    conversation must still claim a seat, including those that found the pool
    already empty because a sibling took the box: one that starts cold is
    recorded as a miss, and two conversations never share a seat's isolation
    session.
    """
    capacity = 3
    profile = current_profile()
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Shared burst {uuid.uuid4().hex[:8]}",
        "model": profile["model"],
        "runtime_profile_name": environment_with_tenancy(e2e_client, "agent"),
        "prepared_capacity": capacity,
        "engine_options": canonical.get("engine_options") or {},
        **({"system": canonical["system"]} if canonical.get("system") else {}),
    }))
    agent_id = created["agent_id"]
    release_agent(agent_id)
    prepared = _wait_prepared_count(e2e_client, agent_id, capacity, live_test_deadline)
    assert prepared["placement"] == "shared_slot", prepared
    assert prepared["last_miss_at"] is None, prepared

    def start() -> str:
        sid = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))["session_id"]
        release_session(sid)
        poll_until_agent_ready(e2e_client, sid)
        return sid

    with ThreadPoolExecutor(max_workers=capacity) as executor:
        sessions = list(executor.map(lambda _: start(), range(capacity)))

    after = data(e2e_client.get(f"/api/v1/agents/{agent_id}/prepared-runtime"))
    assert after["last_miss_at"] is None, f"a conversation of the burst started cold: {after}"
    seats = [
        get_admin_session_detail(e2e_client, sid)["runtime_identity"]["isolated_session_id"]
        for sid in sessions
    ]
    assert all(seats) and len(set(seats)) == capacity, seats


@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
def test_claim_activates_the_already_prepared_engine(
    e2e_client: httpx.Client, live_test_deadline: float, tenancy: str,
    request: pytest.FixtureRequest,
) -> None:
    """Reuse prepared suppliers; an Agent edit replaces only waiting capacity."""
    profile = current_profile()
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    created = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Prepared activation {tenancy} {uuid.uuid4().hex[:8]}",
        "model": profile["model"],
        "runtime_profile_name": environment_with_tenancy(e2e_client, tenancy),
        "prepared_capacity": 1,
        "engine_options": canonical.get("engine_options") or {},
        **({"system": canonical["system"]} if canonical.get("system") else {}),
    }))
    agent_id = created["agent_id"]
    release_agent(agent_id)
    prepared = _wait_prepared(e2e_client, agent_id, live_test_deadline)
    execute = _sandbox_exec(prepared["sandbox_id"])
    receipt = _prepared_identity(agent_id, tenancy, execute)
    assert receipt["sandbox_id"] == prepared["sandbox_id"]
    identity = receipt["runtime_identity"]
    before = _supplier_processes(execute, identity, profile["engine_kind"])

    started = time.monotonic()
    conversation = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
    sid = conversation["session_id"]
    release_session(sid)
    poll_until_agent_ready(e2e_client, sid)
    ready_seconds = time.monotonic() - started
    session = get_admin_session_detail(e2e_client, sid)
    assert session["sandbox_id"] == prepared["sandbox_id"]
    if profile["engine_kind"] == "deepseek_harness":
        # Its web server can host many native Sessions without changing PID.
        assert receipt["prepared_native_session"], "DSH has no prepared native Session"
        assert session.get("engine_session_key") == receipt["prepared_native_session"]
    claimed_identity = session.get("runtime_identity") or {}
    for key in ("home_dir", "linux_user", "isolated_session_id", "workspace_dir"):
        assert (claimed_identity.get(key) or "") == (identity.get(key) or ""), key
    assert _supplier_processes(execute, identity, profile["engine_kind"]) == before, (
        "Claim started a new supplier instead of activating the prepared process"
    )
    activation_evidence = json.dumps({
        "engine": profile["engine_kind"], "tenancy": tenancy,
        "session_id": sid, "sandbox_id": prepared["sandbox_id"],
        "create_to_ready_seconds": ready_seconds, "prepared_processes": before,
    })
    request.node.user_properties.append(("prepared_activation", activation_evidence))
    print(activation_evidence, flush=True)
    # The activated supplier answers the first message.
    _answered(e2e_client, sid, "用一句话解释什么是市盈率。")
    if profile["engine_kind"] == "deepseek_harness":
        assert get_admin_session_detail(e2e_client, sid).get("engine_session_key") == (
            receipt["prepared_native_session"]
        )
    assert _supplier_processes(execute, identity, profile["engine_kind"]) == before, (
        "The first message replaced the supposedly prepared supplier"
    )

    # Observe the refill before editing: the occupied box is not the waiting
    # inventory this change is allowed to retire. Shared boxes contain both.
    waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline)
    waiting_execute = _sandbox_exec(waiting["sandbox_id"])
    waiting_receipt = _prepared_identity(agent_id, tenancy, waiting_execute)
    assert waiting_receipt["slot_id"] != receipt["slot_id"]
    if tenancy == "conversation":
        assert waiting["sandbox_id"] != prepared["sandbox_id"]
    current_agent = data(e2e_client.get(f"/api/v1/agents/{agent_id}"))
    data(e2e_client.put(f"/api/v1/agents/{agent_id}", json={
        "name": current_agent["name"],
        "model": current_agent["model"],
        "runtime_profile_name": current_agent["runtime_profile_name"],
        "version": current_agent["version"],
        "system": (current_agent.get("system") or "") + "\n解释金融概念时优先使用简洁中文。",
    }))
    replacement = _wait_prepared(
        e2e_client, agent_id, live_test_deadline,
        previous_generation=waiting["runtime_generation"],
    )
    replacement_execute = _sandbox_exec(replacement["sandbox_id"])
    replacement_receipt = _prepared_identity(agent_id, tenancy, replacement_execute)
    assert replacement_receipt["slot_id"] != waiting_receipt["slot_id"]
    assert replacement_receipt["sandbox_id"] == replacement["sandbox_id"]
    if tenancy == "conversation":
        assert replacement["sandbox_id"] not in {
            waiting["sandbox_id"], prepared["sandbox_id"],
        }, "A prompt edit did not replace the unclaimed whole-box preparation"

    assert get_admin_session_detail(e2e_client, sid)["sandbox_id"] == prepared["sandbox_id"]
    # The existing Session answers after the Agent edit.
    _answered(e2e_client, sid, "再用一句话解释市净率。")
    continued_session = get_admin_session_detail(e2e_client, sid)
    assert continued_session["sandbox_id"] == prepared["sandbox_id"]

    replacement_identity = replacement_receipt["runtime_identity"]
    replacement_processes = _supplier_processes(
        replacement_execute, replacement_identity, profile["engine_kind"],
    )
    next_started = time.monotonic()
    next_conversation = data(e2e_client.post(
        f"/api/v1/agents/{agent_id}/conversations", json={},
    ))
    next_sid = next_conversation["session_id"]
    release_session(next_sid)
    poll_until_agent_ready(e2e_client, next_sid)
    next_ready_seconds = time.monotonic() - next_started
    next_session = get_admin_session_detail(e2e_client, next_sid)
    assert next_session["sandbox_id"] == replacement["sandbox_id"]
    next_identity = next_session.get("runtime_identity") or {}
    for key in ("home_dir", "linux_user", "isolated_session_id", "workspace_dir"):
        assert (next_identity.get(key) or "") == (replacement_identity.get(key) or ""), key
    if profile["engine_kind"] == "deepseek_harness":
        assert replacement_receipt["prepared_native_session"]
        assert next_session.get("engine_session_key") == replacement_receipt["prepared_native_session"]
    assert _supplier_processes(
        replacement_execute, replacement_identity, profile["engine_kind"],
    ) == replacement_processes
    replacement_evidence = json.dumps({
        "engine": profile["engine_kind"], "tenancy": tenancy,
        "existing_session_id": sid, "existing_sandbox_id": prepared["sandbox_id"],
        "retired_waiting_sandbox_id": waiting["sandbox_id"],
        "retired_waiting_slot_id": waiting_receipt["slot_id"],
        "replacement_slot_id": replacement_receipt["slot_id"],
        "replacement_session_id": next_sid, "replacement_sandbox_id": replacement["sandbox_id"],
        "replacement_create_to_ready_seconds": next_ready_seconds,
    })
    request.node.user_properties.append(("replacement_activation", replacement_evidence))
    print(replacement_evidence, flush=True)


@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
def test_prepared_runtime_resumes_native_history_without_disrupting_sibling(
    e2e_client: httpx.Client, live_test_deadline: float, tenancy: str,
    request: pytest.FixtureRequest,
) -> None:
    """Restore database history into waiting capacity, keeping the other owner live."""
    settings = _run_json([
        "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
        "python", "-c", "import json; from astrabox.common.utils.settings import "
        "load_astrabox_settings; print(json.dumps({'volume': "
        "load_astrabox_settings().sandbox_workspace_volume or ''}))",
    ])
    assert settings == {"volume": ""}, "This case must restore without a workspace volume"
    profile = current_profile()
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Warm resume {tenancy} {uuid.uuid4().hex[:8]}",
        "model": profile["model"],
        "runtime_profile_name": environment_with_tenancy(e2e_client, tenancy),
        "prepared_capacity": 2 if tenancy == "agent" else 1,
        "engine_options": canonical.get("engine_options") or {},
    }))
    agent_id = agent["agent_id"]
    release_agent(agent_id)

    def borrow() -> tuple[str, dict]:
        waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline)
        receipt = _prepared_identity(agent_id, tenancy, _sandbox_exec(waiting["sandbox_id"]))
        sid = data(e2e_client.post(
            f"/api/v1/agents/{agent_id}/conversations", json={},
        ))["session_id"]
        release_session(sid)
        poll_until_agent_ready(e2e_client, sid)
        assert get_admin_session_detail(e2e_client, sid)["sandbox_id"] == waiting["sandbox_id"]
        return sid, receipt

    def remember(sid: str, marker: str) -> None:
        result = _answered(e2e_client, sid, (
            f"Our hypothetical research project is labelled {marker}. "
            "Explain revenue growth in one sentence and include that project label. "
            "No tools or workspace files are needed."
        ))
        assert marker in result.text, result.text

    def recall(sid: str, marker: str, other: str) -> None:
        result = _answered(e2e_client, sid, (
            "What was the original project label in our revenue-growth discussion? "
            "Answer from this conversation, without tools."
        ))
        assert marker in result.text, result.text
        assert other not in result.text, "Another Session's history leaked into the answer"

    if tenancy == "agent":
        _wait_prepared_count(e2e_client, agent_id, 2, live_test_deadline)
    sibling, _ = borrow()
    sibling_marker = "NEIGHBOR_" + uuid.uuid4().hex
    remember(sibling, sibling_marker)
    sibling_before = get_admin_session_detail(e2e_client, sibling)
    sibling_identity = sibling_before["runtime_identity"]
    sibling_exec = _sandbox_exec(sibling_before["sandbox_id"])
    sibling_processes = _supplier_processes(sibling_exec, sibling_identity, profile["engine_kind"])

    sid, original = borrow()
    marker = "RESTORED_" + uuid.uuid4().hex
    remember(sid, marker)
    old = get_admin_session_detail(e2e_client, sid)
    if tenancy == "agent":
        assert old["sandbox_id"] == sibling_before["sandbox_id"]
    assert old.get("engine_session_key"), "A native conversation must exist before recovery"
    mirrored: list[dict] = []
    while time.monotonic() < live_test_deadline:
        mirrored = transcript_entries(sid)
        if marker in json.dumps(mirrored):
            break
        time.sleep(0.3)
    assert marker in json.dumps(mirrored), (
        "The platform's copy of the runtime's transcript does not hold the first message"
    )
    waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline)
    assert waiting["sandbox_id"] != old["sandbox_id"]
    execute = _sandbox_exec(waiting["sandbox_id"])
    receipt = _prepared_identity(agent_id, tenancy, execute)
    assert receipt["sandbox_id"] == waiting["sandbox_id"]
    assert receipt["slot_id"] != original["slot_id"]
    processes = _supplier_processes(execute, receipt["runtime_identity"], profile["engine_kind"])
    reclaimed = data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60))
    assert reclaimed["sandbox_id"] == old["sandbox_id"]
    assert reclaimed["killed"] is (tenancy == "conversation")
    data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60))
    poll_until_agent_ready(e2e_client, sid)
    resumed = get_admin_session_detail(e2e_client, sid)
    assert resumed["sandbox_id"] == waiting["sandbox_id"], "Recovery cold-created instead of claiming"
    assert resumed["engine_session_key"] == old["engine_session_key"]
    for field in ("home_dir", "linux_user", "isolated_session_id", "workspace_dir"):
        assert (resumed["runtime_identity"].get(field) or "") == (
            receipt["runtime_identity"].get(field) or ""
        ), f"Recovery did not adopt the waiting unit's {field}"
    if profile["engine_kind"] != "claude_code":
        assert _supplier_processes(execute, receipt["runtime_identity"], profile["engine_kind"]) == processes
    recall(sid, marker, sibling_marker)
    recall(sibling, sibling_marker, marker)
    sibling_after = get_admin_session_detail(e2e_client, sibling)
    assert sibling_after["sandbox_id"] == sibling_before["sandbox_id"]
    assert sibling_after["engine_session_key"] == sibling_before["engine_session_key"]
    assert sibling_after["runtime_identity"] == sibling_identity
    assert _supplier_processes(sibling_exec, sibling_identity, profile["engine_kind"]) == sibling_processes
    evidence = json.dumps({
        "engine": profile["engine_kind"], "tenancy": tenancy, "session_id": sid,
        "old_sandbox": old["sandbox_id"], "waiting_sandbox": waiting["sandbox_id"],
        "waiting_slot": receipt["slot_id"], "resumed_sandbox": resumed["sandbox_id"],
        "native_session": resumed["engine_session_key"], "sibling_session": sibling,
        "sibling_sandbox": sibling_after["sandbox_id"],
    })
    request.node.user_properties.append(("prepared_resume", evidence))
    print(evidence, flush=True)
