"""A prewarmed conversation adopts an already running supplier, in either tenancy."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile,
    data,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_agent,
    release_session,
    stream_turn,
    wait_until_settled,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.test_transcript_mirror import _query_mirror_docs

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


def _prepared_identity(agent_id: str, tenancy: str, execute: list[str]) -> dict:
    if tenancy == "agent":
        program = """
import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
ensure_database_wiring()
row = asyncio.run(AgentRepository().get_agent(sys.argv[1]))
receipt = (row or {}).get('_prepared_slot') or {}
""" + _PROJECT_RECEIPT
        command = [
            "docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
            "python", "-c", program, agent_id,
        ]
    else:
        program = """
import json
from pathlib import Path
path = Path('/home/agent/.astrabox-prepared-runtime.json')
assert path.is_file(), 'prewarm READY has no prepared engine receipt'
receipt = json.loads(path.read_text())
""" + _PROJECT_RECEIPT
        command = [*execute, "python3", "-c", program]
    receipt = _run_json(command)
    assert isinstance(receipt, dict)
    assert receipt["slot_id"], "Prewarm READY has no initialized engine slot"
    identity = receipt["runtime_identity"]
    assert identity["home_dir"] and identity["linux_user"], receipt
    return receipt


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
        "environment_name": environment_with_tenancy(e2e_client, tenancy),
        "prewarm_enabled": True,
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
    result = stream_turn(e2e_client, sid, content="用一句话解释什么是市盈率。")
    assert result.error is None, result.error
    assert result.text.strip(), "The activated supplier produced no first reply"
    wait_until_settled(e2e_client, sid)
    if profile["engine_kind"] == "deepseek_harness":
        assert get_admin_session_detail(e2e_client, sid).get("engine_session_key") == (
            receipt["prepared_native_session"]
        )
    assert _supplier_processes(execute, identity, profile["engine_kind"]) == before, (
        "The first user turn replaced the supposedly prepared supplier"
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
        "environment_name": current_agent["environment_name"],
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
    continuation = stream_turn(e2e_client, sid, content="再用一句话解释市净率。")
    assert continuation.error is None, continuation.error
    assert continuation.text.strip(), "The existing Session could not answer after the Agent edit"
    wait_until_settled(e2e_client, sid)
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
        "environment_name": environment_with_tenancy(e2e_client, tenancy),
        "prewarm_enabled": True,
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
        result = stream_turn(e2e_client, sid, content=(
            f"Our hypothetical research project is labelled {marker}. "
            "Explain revenue growth in one sentence and include that project label. "
            "No tools or workspace files are needed."
        ))
        assert result.error is None, result.error
        assert marker in result.text, result.text
        wait_until_settled(e2e_client, sid)

    def recall(sid: str, marker: str, other: str) -> None:
        result = stream_turn(e2e_client, sid, content=(
            "What was the original project label in our revenue-growth discussion? "
            "Answer from this conversation, without tools."
        ))
        assert result.error is None, result.error
        assert marker in result.text, result.text
        assert other not in result.text, "Another Session's history leaked into the answer"
        wait_until_settled(e2e_client, sid)

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
    assert old.get("engine_session_key"), "A native conversation must exist before recovery"
    mirrored: list[dict] = []
    while time.monotonic() < live_test_deadline:
        mirrored = _query_mirror_docs(sid)
        if marker in json.dumps(mirrored):
            break
        time.sleep(0.3)
    assert marker in json.dumps(mirrored), "The database has not received the actual first turn"
    waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline)
    execute = _sandbox_exec(waiting["sandbox_id"])
    receipt = _prepared_identity(agent_id, tenancy, execute)
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
